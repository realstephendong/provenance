"""Indexing private Slack channels into the on-device store.

The other half of the Backfill button. `ingest.sync` reads the public channels into
the company's Elasticsearch; this reads the private ones into an encrypted store on
this machine, using this person's own Slack token. Same discovery, same segmentation,
same summaries and embeddings -- the only thing that differs is where the result
lands and who can read it afterwards.

It is the same code path deliberately: a private result and a shared result are built
identically, which is what lets the editor rank them in one list and mean it.

Two things guard the boundary:

  * `report.readable(private=True)` -- this side sees private channels and nothing
    else, exactly as the shared side sees public ones and nothing else;
  * a consent record in the person's own store, checked before any private text is
    sent to a model provider to be summarized or embedded.
"""

from __future__ import annotations

import time

from .. import config
from ..ingest import load, slack_check, summarize, sync
from ..ingest.embed import embed_summaries
from ..ingest.segment import Unit, segment
from ..ingest.slack_client import SlackClient, SlackError
from ..ingest.slack_live import load_slack
from .store import LocalDoc, LocalStore

CONSENT_REMOTE_PROCESSING = "remote_processing"

CONSENT_TEXT = (
    "Indexing a private conversation sends its text to the configured model provider "
    "to be summarized and embedded. The text and the resulting vectors are stored "
    "only on this device, encrypted; they are never sent to the shared Provenance "
    "service. Revoking this consent stops all future private indexing immediately."
)


class ConsentRequired(RuntimeError):
    """Indexing was asked for before the person agreed to remote processing."""


class NotIndexable(RuntimeError):
    """Slack could not be read with this account's token."""


class LocalIngest:
    def __init__(self, store: LocalStore, auth):
        self.store = store
        self.auth = auth
        self._client: SlackClient | None = None

    # --- consent ----------------------------------------------------------------

    def consent(self) -> dict | None:
        record = self.store.consent(CONSENT_REMOTE_PROCESSING)
        if record and record.get("version") != config.LOCAL_CONSENT_VERSION:
            # The terms changed since they agreed. Ask again rather than assume.
            return None
        return record

    def grant_consent(self) -> dict:
        return self.store.grant_consent(
            CONSENT_REMOTE_PROCESSING, config.LOCAL_CONSENT_VERSION,
            {"model": config.DENSE_MODEL, "summarizer": config.SUMMARY_MODEL,
             "text": CONSENT_TEXT},
        )

    def revoke_consent(self) -> None:
        self.store.revoke_consent(CONSENT_REMOTE_PROCESSING)

    def require_consent(self) -> None:
        if self.consent() is None:
            raise ConsentRequired(CONSENT_TEXT)

    # --- Slack ---------------------------------------------------------------------

    def client(self) -> SlackClient:
        if self._client is None:
            self._client = SlackClient(self.auth.require_token())
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def access(self, say=print) -> slack_check.AccessReport:
        """Discover every channel this account can read.

        Always `*`, never the configured list: the configured list is the *company's*
        ingest scope, and this side is answering a different question -- which private
        conversations can the person at this keyboard see.
        """
        report = slack_check.check_access(
            self.client(), config.SLACK_TEAM_ID, ["*"], say=say
        )
        if not report.ok:
            raise NotIndexable("\n".join(slack_check.verdict_lines(report)))
        return report

    # --- writing -----------------------------------------------------------------------

    async def _store_units(self, units: list[Unit], team_id: str) -> int:
        """summarize -> embed -> encrypt -> write.

        The consent gate is the first line because everything after it is a remote
        call with private text in it.
        """
        if not units:
            return 0
        self.require_consent()

        summarized = await summarize.summarize_units(units)
        payloads = [
            load.build_payload(u, s, syms)
            for u, (s, syms) in zip(units, summarized)
        ]
        vectors = await embed_summaries([p["summary"] for p in payloads])

        indexed_at = time.time()
        docs = [
            LocalDoc(
                channel_id=payload["channel_id"],
                channel_name=payload["channel_name"],
                thread_ts=payload["thread_id"], team_id=team_id,
                payload={**payload, "indexed_at": indexed_at},
                vector=vector, ts_start=payload["ts_start"], ts_end=payload["ts_end"],
                content_hash=load.content_hash(payload["raw_text"]),
            )
            for payload, vector in zip(payloads, vectors)
        ]
        return self.store.upsert(docs)

    # --- the entry point -----------------------------------------------------------------

    async def backfill(self, say=print) -> dict:
        """Index everything posted in this account's private channels since last time.

        Incremental by the same rule the shared side uses: read back further than the
        checkpoint, because a thread can gain a reply weeks after its parent, but only
        messages newer than the checkpoint count as new -- and then rebuild the whole
        unit each one belongs to, not just its tail. With no checkpoint every message
        is new, so the first press is a full backfill and every press after it is the
        delta, on one code path.
        """
        self.require_consent()
        report = self.access(say)
        channels = report.readable(private=True)
        if not channels:
            say("  no private channels this account can read")
            return {"indexed": 0, "units": 0, "messages": 0, "channels": 0,
                    "note": "no private channels"}

        say(f"  {len(channels)} private channel(s), indexed on this machine only")
        state = {c.name: self.store.checkpoint(c.channel_id) for c in channels}
        try:
            pool = await _to_thread(
                load_slack, self.client(), report,
                sync.incremental_oldest({k: {"last_ts": v} for k, v in state.items()}),
                private=True, say=say,
            )
        except SlackError as exc:
            raise NotIndexable(f"Slack read failed: {exc}") from None

        fresh = [m for m in pool if m.ts > state.get(m.channel_name, 0.0)]
        if not fresh:
            say("  nothing new in your private channels")
            return {"indexed": 0, "units": 0, "messages": 0,
                    "channels": len(channels), "note": "nothing new"}

        units = segment(sync.affected_messages(pool, fresh))
        say(f"  {len(fresh)} new message(s) -> {len(units)} conversation(s) to rebuild")
        written = await self._store_units(units, report.team_id)

        # Checkpoint only after the write Elasticsearch -- here, the local store --
        # has accepted it. Advancing first and failing loses those messages silently.
        for channel in channels:
            in_channel = [u for u in units if u.channel_id == channel.channel_id]
            if in_channel:
                self.store.set_checkpoint(channel.channel_id, channel.name,
                                          max(u.ts_end for u in in_channel))
        say(f"  indexed {written} private conversation(s), encrypted on this machine")
        return {"indexed": written, "units": len(units), "messages": len(fresh),
                "channels": len(channels)}


async def _to_thread(fn, *args, **kwargs):
    """Slack reads are blocking; the connector serves other requests meanwhile."""
    import asyncio

    return await asyncio.to_thread(lambda: fn(*args, **kwargs))
