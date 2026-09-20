"""The encrypted on-device store for one person's private index.

Not Elasticsearch, deliberately. Running an ES node on every developer laptop means
a JVM, a multi-gigabyte heap, a VSIX that cannot ship it, and a support burden the
moment a version drifts. This is SQLite from the standard library plus AES-GCM from
`cryptography` (already a dependency, via PyJWT), and the storage interface is narrow
enough that a LanceDB or local-ES backend could replace it without touching
`retrieve.py`.

**What is encrypted: everything that describes a conversation.** Message text, the
summary, the channel name, the permalink, the participants and the embedding vector
are all sealed with a key held in the OS credential store. What stays in plaintext is
only what the store must query on without the key being an index: row ids, timestamps
and content hashes.

Lookups that would normally need plaintext use keyed HMACs instead. The channel a row
belongs to is stored as `HMAC(key, channel_id)`, and every exact-match term -- file
path, PR number, commit sha, identifier -- as `HMAC(key, kind:value)`. Equality still
works, so the exact tier is exact; the values themselves are not recoverable from the
file. A deterministic HMAC does leak *equality* (two rows about the same file are
visibly about the same something), which is the honest trade for keeping exact match
on an encrypted store.

Dense search decrypts every vector on every query. At laptop scale -- a person's own
private threads, thousands not millions -- that is milliseconds to a few hundred
milliseconds, and it is why `LOCAL_RETRIEVE_LIMIT` exists rather than an ANN index:
an ANN structure over ciphertext would either leak the geometry or need the whole set
decrypted anyway.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import secrets
import sqlite3
import stat
import struct
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .. import config
from . import keychain

KEY_NAME = "store_key"
SCHEMA_VERSION = 1

TERM_KINDS = ("pr", "sha", "path", "basename", "symbol")
# Exact-tier strengths, identical to the shared plane's so a merged result list
# ranks like one list rather than two.
TERM_STRENGTH = {"pr": 5, "sha": 4, "path": 3, "basename": 2, "symbol": 1}


class StoreLocked(RuntimeError):
    """The encryption key is missing or does not match this store."""


class CorruptStore(RuntimeError):
    """The database exists but cannot be read as a Provenance local store."""


@dataclass
class LocalDoc:
    """One private conversation, as it goes in and comes out."""

    channel_id: str
    channel_name: str
    thread_ts: str
    team_id: str
    payload: dict = field(default_factory=dict)
    vector: list[float] = field(default_factory=list)
    ts_start: float = 0.0
    ts_end: float = 0.0
    content_hash: str = ""


def doc_id_for(profile_id: str, team_id: str, channel_id: str, thread_ts: str) -> str:
    """The namespaced local id the plan specifies.

    Not keyed: it has to stay stable across a key rotation for deletion and
    checkpointing to keep working, and a SHA-256 of four identifiers reveals nothing
    to someone who does not already hold them.
    """
    raw = f"{profile_id}|{team_id}|{channel_id}|{thread_ts}".encode()
    return hashlib.sha256(raw).hexdigest()[:40]


def _pack(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    return [v / norm for v in vector] if norm else list(vector)


try:                                    # numpy is not a declared dependency
    import numpy as _np
except ImportError:                     # pragma: no cover - depends on the install
    _np = None


def _dot(a: list[float], b: list[float]) -> float:
    if _np is not None:
        return float(_np.dot(_np.asarray(a, dtype=_np.float32),
                             _np.asarray(b, dtype=_np.float32)))
    return sum(x * y for x, y in zip(a, b))


SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS documents (
        doc_id       TEXT PRIMARY KEY,
        channel_key  TEXT NOT NULL,
        ts_start     REAL NOT NULL DEFAULT 0,
        ts_end       REAL NOT NULL DEFAULT 0,
        indexed_at   REAL NOT NULL DEFAULT 0,
        content_hash TEXT NOT NULL DEFAULT '',
        embedder_id  TEXT NOT NULL DEFAULT '',
        payload      BLOB NOT NULL,
        vector       BLOB NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS documents_channel ON documents (channel_key)",
    """
    CREATE TABLE IF NOT EXISTS doc_terms (
        doc_id   TEXT NOT NULL,
        kind     TEXT NOT NULL,
        term_key TEXT NOT NULL,
        PRIMARY KEY (doc_id, kind, term_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS doc_terms_lookup ON doc_terms (kind, term_key)",
    """
    CREATE TABLE IF NOT EXISTS checkpoints (
        channel_key  TEXT PRIMARY KEY,
        channel_meta BLOB NOT NULL,
        last_ts      REAL NOT NULL DEFAULT 0,
        updated_at   REAL NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS consents (
        name       TEXT PRIMARY KEY,
        version    TEXT NOT NULL DEFAULT '',
        granted_at REAL NOT NULL DEFAULT 0,
        detail     TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL DEFAULT ''
    )
    """,
]


class LocalStore:
    def __init__(self, profile_id: str = config.LOCAL_DEFAULT_PROFILE,
                 root: Path | None = None):
        self.profile_id = profile_id
        self.root = Path(root or config.LOCAL_PROFILE_DIR) / profile_id
        self._key: bytes | None = None
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    # --- lifecycle -------------------------------------------------------------

    @property
    def path(self) -> Path:
        return self.root / "local.db"

    def key(self) -> bytes:
        """The store key, created on first use and kept in the OS credential store."""
        if self._key is None:
            raw = keychain.get(self.root, KEY_NAME)
            if not raw:
                self.root.mkdir(parents=True, exist_ok=True)
                raw = secrets.token_hex(32)
                keychain.put(self.root, KEY_NAME, raw)
            try:
                self._key = bytes.fromhex(raw)
            except ValueError as exc:
                raise StoreLocked("the stored key is not usable") from exc
            if len(self._key) != 32:
                raise StoreLocked("the stored key is the wrong length")
        return self._key

    def open(self) -> "LocalStore":
        with self._lock:
            if self._conn is not None:
                return self
            self.root.mkdir(parents=True, exist_ok=True)
            # 0700: the directory holds ciphertext, but its file names and sizes are
            # still information about what a person has indexed.
            os.chmod(self.root, stat.S_IRWXU)
            first = not self.path.exists()
            conn = sqlite3.connect(self.path, check_same_thread=False,
                                   isolation_level=None)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                for statement in SCHEMA:
                    conn.execute(statement)
            except sqlite3.DatabaseError as exc:
                conn.close()
                raise CorruptStore(
                    f"{self.path} is not readable as a Provenance store: {exc}. "
                    "Delete the profile directory to start clean; nothing in it is "
                    "recoverable without the key."
                ) from exc
            if first:
                os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
            self._conn = conn
            self.key()
            self._write_meta("schema_version", str(SCHEMA_VERSION))
            self._write_meta("profile_id", self.profile_id)
            return self

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            self.open()
        return self._conn                      # type: ignore[return-value]

    # --- crypto ------------------------------------------------------------------

    def _seal(self, data: bytes, aad: bytes) -> bytes:
        nonce = os.urandom(12)
        return nonce + AESGCM(self.key()).encrypt(nonce, data, aad)

    def _open_sealed(self, blob: bytes, aad: bytes) -> bytes:
        try:
            return AESGCM(self.key()).decrypt(bytes(blob[:12]), bytes(blob[12:]), aad)
        except InvalidTag as exc:
            # Either the key changed or the row was tampered with. Both mean this row
            # is not trustworthy, and returning it anyway would be worse than failing.
            raise StoreLocked(
                "a stored record could not be decrypted with the current key"
            ) from exc

    def _mac(self, kind: str, value: str) -> str:
        return hmac.new(self.key(), f"{kind}:{value}".encode(), hashlib.sha256).hexdigest()

    def channel_key(self, channel_id: str) -> str:
        return self._mac("channel", channel_id)

    def term_key(self, kind: str, value: str) -> str:
        return self._mac(f"term.{kind}", value.strip().lower())

    # --- meta and consent -----------------------------------------------------------

    def _write_meta(self, key: str, value: str) -> None:
        self._db().execute(
            "INSERT INTO meta (key, value) VALUES (?,?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value),
        )

    def meta(self, key: str) -> str:
        row = self._db().execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else ""

    def grant_consent(self, name: str, version: str, detail: dict | None = None) -> dict:
        record = {"name": name, "version": version, "granted_at": time.time(),
                  "detail": detail or {}}
        self._db().execute(
            "INSERT INTO consents (name, version, granted_at, detail) VALUES (?,?,?,?) "
            "ON CONFLICT (name) DO UPDATE SET version = excluded.version, "
            "granted_at = excluded.granted_at, detail = excluded.detail",
            (name, version, record["granted_at"], json.dumps(record["detail"])),
        )
        return record

    def revoke_consent(self, name: str) -> None:
        self._db().execute("DELETE FROM consents WHERE name = ?", (name,))

    def consent(self, name: str) -> dict | None:
        row = self._db().execute(
            "SELECT * FROM consents WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            return None
        return {"name": row["name"], "version": row["version"],
                "granted_at": row["granted_at"],
                "detail": json.loads(row["detail"] or "{}")}

    # --- writes ----------------------------------------------------------------------

    def upsert(self, docs: list[LocalDoc]) -> int:
        """Write conversations, replacing any earlier version of the same one.

        Idempotent through `doc_id_for`, exactly as the shared plane is through
        `load.point_id`: re-indexing a thread that gained a reply overwrites it
        rather than leaving two partial copies.
        """
        if not docs:
            return 0
        conn = self._db()
        written = 0
        now = time.time()
        with self._lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for doc in docs:
                    doc_id = doc_id_for(self.profile_id, doc.team_id, doc.channel_id,
                                        doc.thread_ts)
                    body = dict(doc.payload)
                    body.setdefault("channel_id", doc.channel_id)
                    body.setdefault("channel_name", doc.channel_name)
                    body.setdefault("thread_id", doc.thread_ts)
                    body["scope"] = config.SCOPE_PRIVATE
                    sealed = self._seal(json.dumps(body).encode(), doc_id.encode())
                    vector = self._seal(_pack(_normalize(doc.vector)),
                                        (doc_id + ":vec").encode())
                    conn.execute(
                        "INSERT INTO documents (doc_id, channel_key, ts_start, ts_end, "
                        "indexed_at, content_hash, embedder_id, payload, vector) "
                        "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT (doc_id) DO UPDATE SET "
                        "channel_key = excluded.channel_key, ts_start = excluded.ts_start, "
                        "ts_end = excluded.ts_end, indexed_at = excluded.indexed_at, "
                        "content_hash = excluded.content_hash, "
                        "embedder_id = excluded.embedder_id, payload = excluded.payload, "
                        "vector = excluded.vector",
                        (doc_id, self.channel_key(doc.channel_id), doc.ts_start,
                         doc.ts_end, now, doc.content_hash, config.EMBEDDER_ID,
                         sealed, vector),
                    )
                    conn.execute("DELETE FROM doc_terms WHERE doc_id = ?", (doc_id,))
                    for kind, value in self._terms(body):
                        conn.execute(
                            "INSERT OR IGNORE INTO doc_terms (doc_id, kind, term_key) "
                            "VALUES (?,?,?)", (doc_id, kind, self.term_key(kind, value)),
                        )
                    written += 1
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return written

    @staticmethod
    def _terms(payload: dict) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        out.extend(("pr", str(n)) for n in payload.get("pr_refs", []))
        out.extend(("sha", str(s)) for s in payload.get("commit_shas", []))
        out.extend(("path", str(p)) for p in payload.get("file_paths", []))
        out.extend(("basename", str(b)) for b in payload.get("file_basenames", []))
        out.extend(
            ("symbol", str(s)) for s in payload.get("symbols", [])
            if len(str(s)) >= config.EXACT_SYMBOL_MIN_CHARS
        )
        return out

    # --- reads -------------------------------------------------------------------------

    def _payload_of(self, row: sqlite3.Row) -> dict:
        return json.loads(self._open_sealed(row["payload"], row["doc_id"].encode()))

    def get(self, doc_id: str) -> dict | None:
        row = self._db().execute(
            "SELECT * FROM documents WHERE doc_id = ?", (doc_id,)
        ).fetchone()
        return self._payload_of(row) if row else None

    def search_dense(self, vector: list[float], limit: int) -> list[dict]:
        """Cosine similarity over every stored vector.

        Scores are rescaled to (1 + cosine) / 2 to match what Elasticsearch reports
        for a `dense_vector` cosine field. Without that the shared plane's
        NULL_THRESHOLD and SEMANTIC_FLOOR_RATIO would mean something different on
        each side, and the merged list would be ranked on two incompatible scales.
        """
        query = _normalize(vector)
        hits = []
        for row in self._db().execute(
            "SELECT doc_id, vector, ts_start, ts_end FROM documents"
        ):
            stored = _unpack(self._open_sealed(row["vector"], (row["doc_id"] + ":vec").encode()))
            if len(stored) != len(query):
                continue                # a vector from a different embedding model
            hits.append({"doc_id": row["doc_id"],
                         "score": (1.0 + _dot(query, stored)) / 2.0})
        hits.sort(key=lambda h: -h["score"])
        return self._hydrate(hits[:limit])

    def search_exact(self, terms: dict[str, list[str]]) -> list[dict]:
        """Structural matches, by keyed term. Returns the strongest kind per document."""
        pairs: list[tuple[str, str]] = [
            (kind, self.term_key(kind, value))
            for kind, values in terms.items() if kind in TERM_KINDS
            for value in values if str(value).strip()
        ]
        if not pairs:
            return []
        best: dict[str, int] = {}
        for kind, key in pairs:
            for row in self._db().execute(
                "SELECT doc_id FROM doc_terms WHERE kind = ? AND term_key = ?", (kind, key)
            ):
                strength = TERM_STRENGTH[kind]
                if strength > best.get(row["doc_id"], 0):
                    best[row["doc_id"]] = strength
        ordered = sorted(best.items(), key=lambda kv: -kv[1])
        return self._hydrate([{"doc_id": d, "exact_strength": s} for d, s in ordered])

    def _hydrate(self, hits: list[dict]) -> list[dict]:
        if not hits:
            return []
        placeholders = ",".join("?" for _ in hits)
        rows = {
            row["doc_id"]: row for row in self._db().execute(
                f"SELECT * FROM documents WHERE doc_id IN ({placeholders})",
                tuple(h["doc_id"] for h in hits),
            )
        }
        out = []
        for hit in hits:
            row = rows.get(hit["doc_id"])
            if row is None:
                continue
            out.append({**hit, "payload": self._payload_of(row),
                        "content_hash": row["content_hash"],
                        "indexed_at": row["indexed_at"]})
        return out

    # --- checkpoints ---------------------------------------------------------------------

    def set_checkpoint(self, channel_id: str, channel_name: str, last_ts: float) -> None:
        key = self.channel_key(channel_id)
        meta = self._seal(
            json.dumps({"channel_id": channel_id, "channel_name": channel_name}).encode(),
            key.encode(),
        )
        self._db().execute(
            "INSERT INTO checkpoints (channel_key, channel_meta, last_ts, updated_at) "
            "VALUES (?,?,?,?) ON CONFLICT (channel_key) DO UPDATE SET "
            "channel_meta = excluded.channel_meta, "
            "last_ts = CASE WHEN checkpoints.last_ts > excluded.last_ts "
            "               THEN checkpoints.last_ts ELSE excluded.last_ts END, "
            "updated_at = excluded.updated_at",
            (key, meta, float(last_ts), time.time()),
        )

    def checkpoint(self, channel_id: str) -> float:
        row = self._db().execute(
            "SELECT last_ts FROM checkpoints WHERE channel_key = ?",
            (self.channel_key(channel_id),),
        ).fetchone()
        return float(row["last_ts"]) if row else 0.0

    def channels(self) -> list[dict]:
        out = []
        for row in self._db().execute("SELECT * FROM checkpoints ORDER BY updated_at DESC"):
            meta = json.loads(self._open_sealed(row["channel_meta"], row["channel_key"].encode()))
            count = self._db().execute(
                "SELECT COUNT(*) AS n FROM documents WHERE channel_key = ?",
                (row["channel_key"],),
            ).fetchone()["n"]
            out.append({**meta, "last_ts": row["last_ts"],
                        "updated_at": row["updated_at"], "documents": int(count)})
        return out

    # --- deletion ------------------------------------------------------------------------

    def delete_channel(self, channel_id: str) -> int:
        key = self.channel_key(channel_id)
        conn = self._db()
        with self._lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                doc_ids = [r["doc_id"] for r in conn.execute(
                    "SELECT doc_id FROM documents WHERE channel_key = ?", (key,)
                )]
                for doc_id in doc_ids:
                    conn.execute("DELETE FROM doc_terms WHERE doc_id = ?", (doc_id,))
                conn.execute("DELETE FROM documents WHERE channel_key = ?", (key,))
                conn.execute("DELETE FROM checkpoints WHERE channel_key = ?", (key,))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return len(doc_ids)

    def documents_in(self, channel_id: str, since: float = 0.0) -> list[dict]:
        """Every stored conversation for a channel that *starts* at or after `since`.

        The window bound is on `ts_start` for the same reason the shared plane's is:
        a conversation that began before the window would be refetched truncated and
        compare as changed on every single reconciliation pass.
        """
        rows = self._db().execute(
            "SELECT * FROM documents WHERE channel_key = ? AND ts_start >= ?",
            (self.channel_key(channel_id), float(since)),
        ).fetchall()
        return [
            {"doc_id": r["doc_id"], "content_hash": r["content_hash"],
             "ts_start": r["ts_start"], "payload": self._payload_of(r)}
            for r in rows
        ]

    def delete_document(self, doc_id: str) -> None:
        conn = self._db()
        with self._lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("DELETE FROM doc_terms WHERE doc_id = ?", (doc_id,))
                conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def stats(self) -> dict:
        conn = self._db()
        documents = int(conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"])
        channels = int(conn.execute("SELECT COUNT(*) AS n FROM checkpoints").fetchone()["n"])
        size = self.path.stat().st_size if self.path.exists() else 0
        return {"documents": documents, "channels": channels, "bytes": size,
                "profile_id": self.profile_id, "path": str(self.path),
                "key_backend": keychain.backend_name()}

    def purge(self) -> dict:
        """Delete everything: documents, checkpoints, consents, the key, the token.

        A deletion command that leaves the key behind has not deleted anything --
        and one that leaves the database behind leaves ciphertext someone could
        decrypt later if the key ever leaks. Both go.
        """
        # A store that cannot be opened must still be deletable -- that is precisely
        # the case where someone most wants it gone. Counting what was in it is a
        # courtesy, not a precondition.
        try:
            stats = self.stats()
        except Exception:
            stats = {"documents": 0, "channels": 0}
        self.close()
        for name in ("local.db", "local.db-wal", "local.db-shm"):
            try:
                (self.root / name).unlink()
            except OSError:
                pass
        keychain.delete(self.root, KEY_NAME)
        from .auth import TOKEN_NAME             # circular only at call time

        keychain.delete(self.root, TOKEN_NAME)
        secrets_dir = self.root / "secrets"
        try:
            for leftover in secrets_dir.iterdir():
                leftover.unlink()
            secrets_dir.rmdir()
        except OSError:
            pass
        self._key = None
        return {"deleted_documents": stats["documents"],
                "deleted_channels": stats["channels"]}
