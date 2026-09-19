"""Elasticsearch writes: the document body, index creation, the embedder stamp, and
idempotent upsert."""

from __future__ import annotations

import hashlib
import time
import uuid

from elasticsearch import Elasticsearch

from .. import config
from .extract import extract_refs
from .segment import Unit

# 9.2. `thread_id` is mapped explicitly rather than left to dynamic mapping: it is a
# dotted timestamp string and must stay an exact-match keyword.
INDEX_SETTINGS = {"number_of_shards": 1, "number_of_replicas": 0}

INDEX_MAPPING = {
    "properties": {
        "dense_vec":     {"type": "dense_vector", "dims": config.DENSE_DIM,
                          "index": True, "similarity": "cosine"},
        "summary":       {"type": "text"},
        "raw_text":      {"type": "text"},
        "symbols":       {"type": "text"},
        "thread_id":     {"type": "keyword"},
        "pr_refs":       {"type": "integer"},
        "commit_shas":   {"type": "keyword"},
        "ticket_refs":   {"type": "keyword"},
        "file_paths":    {"type": "keyword"},
        "participants":  {"type": "keyword"},
        "channel_name":  {"type": "keyword"},
        "channel_id":    {"type": "keyword"},
        "channel_tier":  {"type": "integer"},
        "permalink":     {"type": "keyword"},
        "ts_start":      {"type": "date", "format": "epoch_second"},
        "ts_end":        {"type": "date", "format": "epoch_second"},
        "message_count": {"type": "integer"},
        "reactions":     {"type": "keyword"},
        "is_bookmarked": {"type": "boolean"},
        "content_hash":  {"type": "keyword"},
    }
}


def build_payload(
    unit: Unit, summary: str, llm_symbols: list[str], *, bookmark: bool = False
) -> dict:
    """One `Unit` -> the document body `upsert` writes (everything but the vector).

    `bookmark=True` asserts that a human explicitly asked for this unit. Only the
    Slack bot sets it: a batch scan can only read intent off a trigger reaction, an
    on-demand request *is* the intent. It feeds `REACTION_BOOST` at query time.
    """
    refs = extract_refs(unit.raw_text)
    symbols, seen = [], set()
    for s in list(refs["symbols"]) + list(llm_symbols):
        key = s.lower()
        if key not in seen:
            seen.add(key)
            symbols.append(s)
    return {
        "thread_id": unit.thread_id,
        "channel_id": unit.channel_id,
        "channel_name": unit.channel_name,
        "channel_tier": unit.channel_tier,
        "permalink": unit.permalink,
        "ts_start": unit.ts_start,
        "ts_end": unit.ts_end,
        "participants": unit.participants,
        "message_count": len(unit.messages),
        "summary": summary,
        "raw_text": unit.raw_text,
        "pr_refs": refs["pr_refs"],
        "commit_shas": refs["commit_shas"],
        "ticket_refs": refs["ticket_refs"],
        "file_paths": refs["file_paths"],
        "symbols": symbols,
        "reactions": unit.reactions,
        "is_bookmarked": unit.is_bookmarked or bookmark,
    }


def client() -> Elasticsearch:
    return Elasticsearch(config.ES_URL, request_timeout=30)


def point_id(channel_id: str, thread_id: str) -> str:
    """Deterministic document id. This is what makes every ingest mode idempotent:
    re-indexing the same thread overwrites in place rather than duplicating."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{channel_id}/{thread_id}"))


def content_hash(raw_text: str) -> str:
    return hashlib.sha256(raw_text.encode("utf-8")).hexdigest()


def ensure_index(es: Elasticsearch, recreate: bool = False) -> None:
    if es.indices.exists(index=config.INDEX):
        if recreate:
            es.indices.delete(index=config.INDEX)
        else:
            return
    es.indices.create(index=config.INDEX, settings=INDEX_SETTINGS, mappings=INDEX_MAPPING)


def stamp_source(es: Elasticsearch, source: str, detail: dict | None = None) -> None:
    """Record where this index came from.

    Without this, "am I querying the real workspace or the seed fixture?" can only
    be answered by eyeballing permalinks. /health reads it back.
    """
    es.index(index=config.META_INDEX, id="source", document={
        "source": source, "built_at": time.time(), **(detail or {}),
    }, refresh=True)


def read_source(es: Elasticsearch) -> dict | None:
    try:
        return es.get(index=config.META_INDEX, id="source")["_source"]
    except Exception:
        return None


def stamp_embedder(es: Elasticsearch) -> None:
    es.index(index=config.META_INDEX, id="embedder", document={
        "embedder_id": config.EMBEDDER_ID, "dense_dim": config.DENSE_DIM, "built_at": time.time(),
    })


def read_embedder_id(es: Elasticsearch) -> str | None:
    try:
        return es.get(index=config.META_INDEX, id="embedder")["_source"]["embedder_id"]
    except Exception:
        return None


def upsert(es: Elasticsearch, payloads: list[dict], dense: list[list[float]]) -> int:
    from elasticsearch.helpers import bulk

    actions = []
    for payload, vec in zip(payloads, dense):
        _id = point_id(payload["channel_id"], payload["thread_id"])
        doc = {**payload, "dense_vec": vec, "content_hash": content_hash(payload["raw_text"])}
        # `epoch_second` will not accept a fractional value; the sub-second part is
        # meaningless against a 60-day decay scale anyway.
        doc["ts_start"] = int(payload["ts_start"])
        doc["ts_end"] = int(payload["ts_end"])
        actions.append({"_index": config.INDEX, "_id": _id, "_source": doc})
    ok, errors = bulk(es, actions, chunk_size=config.UPSERT_BATCH, raise_on_error=False)
    if errors:
        print(f"  ! {len(errors)} documents failed to index: {errors[:3]}")
    return ok


def scroll_all(es: Elasticsearch, fields: list[str]) -> dict[str, dict]:
    """Used by --mode reconcile. Returns {_id: {field: value, ...}}."""
    out: dict[str, dict] = {}
    if not es.indices.exists(index=config.INDEX):
        return out
    resp = es.search(index=config.INDEX, query={"match_all": {}}, source=fields, scroll="2m", size=500)
    scroll_id = resp.get("_scroll_id")
    try:
        while resp["hits"]["hits"]:
            for h in resp["hits"]["hits"]:
                out[h["_id"]] = h["_source"]
            resp = es.scroll(scroll_id=scroll_id, scroll="2m")
            scroll_id = resp.get("_scroll_id")
    finally:
        if scroll_id:
            try:
                es.clear_scroll(scroll_id=scroll_id)
            except Exception:
                pass
    return out
