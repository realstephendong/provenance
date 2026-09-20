"""Ingest as a library: the three modes, callable by the CLI and by the service.

`provenance.ingest.__main__` is the command line around this; `/ingest/sync` in
`provenance.service.main` is the Provenance panel's Backfill button around it. Both
drive the same functions, so there is one implementation of "what has already been
indexed" rather than two that can disagree.

Every mode takes a `say` callback instead of printing: the CLI passes `print`, the
service collects the lines and hands them back to the caller.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

from dataclasses import dataclass, field

from .. import config
from . import checkpoint as checkpoint_store
from . import load, pipeline, slack_check, slack_live
from .segment import Unit, gap_report, gap_report_lines, group_by_channel, segment
from .slack_client import SlackClient
from .slack_source import Message, load_export

# Where a mode's progress goes. `print` for the CLI; a list's `append` for the service.
Say = Callable[[str], None]


async def _read(source: Loader, oldest: float) -> list[Message]:
    """Run the loader off the event loop.

    Both loaders are blocking -- `slack_live` drives an httpx.Client, the export reader
    walks a directory -- and under the service that read is the long pole of a sync.
    The CLI cannot tell the difference; the service would otherwise answer nothing
    else, including /health, until Slack came back.
    """
    return await asyncio.to_thread(source, oldest)

# A message source: `oldest` (unix ts) -> messages. The export reader ignores `oldest`
# and always returns everything; the live reader uses it to bound the API calls.
Loader = Callable[[float], list[Message]]


def report_gaps(messages: list[Message], say: Say = print) -> None:
    """Say so when a channel's loose messages are packed too tightly for rule 2's
    gap threshold to separate anything. Silent when there is nothing to report."""
    for line in gap_report_lines(gap_report(messages)):
        say(line)


async def process_units(es, units: list[Unit], say: Say = print) -> int:
    """The shared tail of every mode: summarize, embed, and write `units`.

    The work itself is `pipeline.build_and_upsert`, which the Slack bot's on-demand
    path also calls; this is the batch path's progress reporting around it.
    """
    if not units:
        say("  nothing to process")
        return 0

    _, written = await pipeline.build_and_upsert(es, units, say=say)
    say(f"  indexed {written} documents")
    return written


# --- mode: backfill -----------------------------------------------------------


async def run_backfill(source: Loader, checkpoint_path: Path, recreate: bool,
                       source_label: str = 'unknown', source_detail: dict | None = None,
                       say: Say = print) -> dict:
    # Read first: with --recreate the index is dropped below, and a failed source read
    # (a Slack outage, a rate limit) must not leave the person with an empty index.
    messages = await _read(source, 0.0)

    es = load.client()
    load.ensure_index(es, recreate=recreate)

    report_gaps(messages, say)
    units = segment(messages)
    say(f"backfill: {len(messages)} messages -> {len(units)} units")
    written = await process_units(es, units, say)

    state: dict = {}
    for unit in units:
        entry = state.setdefault(unit.channel_name, {"last_ts": 0.0})
        entry["last_ts"] = max(entry["last_ts"], unit.ts_end)
    checkpoint_store.save(checkpoint_path, state)
    load.stamp_embedder(es)
    # Re-read the workspace here, not at dispatch: slack_live rewrites
    # config.SLACK_WORKSPACE from auth.test once the source has actually run,
    # so capturing it earlier stamps the "acme" placeholder.
    detail = dict(source_detail or {})
    if source_label == "slack":
        detail["workspace"] = config.SLACK_WORKSPACE
    load.stamp_source(es, source_label, detail)
    say(f"  checkpoint -> {checkpoint_path}")
    say(f"  embedder stamped: {config.EMBEDDER_ID}")
    return {"mode": "backfill", "messages": len(messages), "units": len(units),
            "indexed": written, "checkpoint": state}


# --- mode: incremental ---------------------------------------------------------


def _dedupe_by_identity(messages: list[Message]) -> list[Message]:
    seen, out = set(), []
    for m in messages:
        key = (m.channel_id, m.ts)
        if key not in seen:
            seen.add(key)
            out.append(m)
    return out


def _burst_reachable(pool: list[Message], seeds: list[Message], gap: float) -> list[Message]:
    """Every loose message transitively within `gap` of a seed message.

    A burst is a chain, so reachability has to run to a fixpoint: a new message can
    pull in a message that pulls in another. Forward and backward passes over a
    ts-sorted list converge in one round each.
    """
    ordered = sorted(pool, key=lambda m: m.ts)
    seed_ts = {m.ts for m in seeds}
    marked = [m.ts in seed_ts for m in ordered]
    if not any(marked):
        return []

    changed = True
    while changed:
        changed = False
        for i in range(1, len(ordered)):
            if marked[i - 1] and not marked[i] and (ordered[i].ts - ordered[i - 1].ts) <= gap:
                marked[i] = changed = True
        for i in range(len(ordered) - 2, -1, -1):
            if marked[i + 1] and not marked[i] and (ordered[i + 1].ts - ordered[i].ts) <= gap:
                marked[i] = changed = True
    return [m for m, hit in zip(ordered, marked) if hit]


def affected_messages(all_messages: list[Message], new_messages: list[Message]) -> list[Message]:
    """Expand new messages to every message whose *unit* they change.

    Any thread a new message belongs to must be reconstructed in full (old + new),
    not just its new tail, so the resummarized thread is complete and re-embeds
    correctly. Same for a burst that a new message continues within
    SEGMENT_GAP_SECONDS of an already-indexed burst's last message.

    Filtering by timestamp alone -- the obvious implementation -- would segment a
    three-week-old thread's new reply in isolation and overwrite the real, larger
    thread with a truncated summary. That is the whole point of this function
    (11.7, 18 row 27).
    """
    affected_thread_ts = {m.thread_ts for m in new_messages if m.thread_ts is not None}
    by_channel_all = group_by_channel(all_messages)

    affected: list[Message] = []
    for cname, channel_new in group_by_channel(new_messages).items():
        pool = by_channel_all.get(cname, [])
        affected.extend(
            m for m in pool if m.thread_ts is not None and m.thread_ts in affected_thread_ts
        )
        loose_seeds = [m for m in channel_new if m.thread_ts is None]
        if not config.SEGMENT_TRUST_TIME:
            # No bursts exist to continue: `segment` gives every loose message its own
            # unit, so a new one affects itself and nothing around it.
            affected.extend(loose_seeds)
            continue
        loose_pool = [m for m in pool if m.thread_ts is None]
        affected.extend(_burst_reachable(loose_pool, loose_seeds, config.SEGMENT_GAP_SECONDS))
    return _dedupe_by_identity(affected)


def incremental_oldest(state: dict) -> float:
    """How far back a live source must read for an incremental run.

    `affected_messages` needs the surrounding pool, not just new messages: a burst can
    continue from up to SEGMENT_GAP_SECONDS before the checkpoint, and recent threads
    can have gained replies. Threads whose parent is older than the lookback are left
    to `--mode reconcile`, as is a channel newly added to SLACK_BOT_CHANNEL_IDS.
    """
    last = [entry.get("last_ts", 0.0) for entry in state.values()]
    if not last:
        return 0.0
    margin = config.SEGMENT_GAP_SECONDS + config.SLACK_THREAD_LOOKBACK_DAYS * 86400
    return max(0.0, min(last) - margin)


async def run_incremental(source: Loader, checkpoint_path: Path, say: Say = print,
                          source_label: str = "unknown",
                          source_detail: dict | None = None) -> dict:
    es = load.client()
    load.ensure_index(es)
    # An incremental run against an index nobody has backfilled yet *is* the backfill:
    # with an empty checkpoint every message is new. It then has to leave the index as
    # queryable as `run_backfill` would, or `retrieve.check_embedder` rejects every
    # search against it for want of a stamp. Only on a fresh index -- re-stamping an
    # existing one would let a sync silently relabel which corpus is being served.
    unstamped = load.read_embedder_id(es) is None

    state = checkpoint_store.load(checkpoint_path)
    all_messages = await _read(source, incremental_oldest(state))

    new_messages = [
        m for m in all_messages
        if m.ts > state.get(m.channel_name, {}).get("last_ts", 0.0)
    ]
    if not new_messages:
        say("nothing new")
        return {"mode": "incremental", "new_messages": 0, "affected": 0, "units": 0,
                "indexed": 0, "checkpoint": state}
    say(f"incremental: {len(new_messages)} new messages")

    report_gaps(all_messages, say)
    affected = affected_messages(all_messages, new_messages)
    units = segment(affected)
    say(f"  {len(affected)} affected messages -> {len(units)} units to rebuild")
    written = await process_units(es, units, say)

    for cname, channel_new in group_by_channel(new_messages).items():
        state.setdefault(cname, {})["last_ts"] = max(m.ts for m in channel_new)
    checkpoint_store.save(checkpoint_path, state)
    say(f"  checkpoint -> {checkpoint_path}")
    if unstamped:
        detail = dict(source_detail or {})
        if source_label == "slack":
            detail["workspace"] = config.SLACK_WORKSPACE
        load.stamp_embedder(es)
        load.stamp_source(es, source_label, detail)
        say(f"  embedder stamped: {config.EMBEDDER_ID}")
    return {"mode": "incremental", "new_messages": len(new_messages),
            "affected": len(affected), "units": len(units), "indexed": written,
            "checkpoint": state}


# --- mode: reconcile -----------------------------------------------------------


async def run_reconcile(source: Loader, say: Say = print) -> dict:
    """Drift detection between the source and the index.

    Stands in for "webhooks got missed" in a system with no live webhooks to miss, and
    is what catches edits, deletions, replies to old threads and newly added channels.
    Known scaling limit, stated rather than solved: this re-segments the entire
    source on every run. That is cheap (no LLM calls) but O(total messages), and with
    `--source slack` it re-reads the whole channel through the API; at real scale you
    would shard by channel and date with per-channel cursors.
    """
    es = load.client()
    load.ensure_index(es)

    all_messages = await _read(source, 0.0)
    report_gaps(all_messages, say)
    units = segment(all_messages)
    current_by_id = {
        load.point_id(u.channel_id, u.thread_id): load.content_hash(u.raw_text) for u in units
    }
    indexed = load.scroll_all(es, fields=["content_hash"])

    missing = [i for i in current_by_id if i not in indexed]
    changed = [
        i for i in current_by_id
        if i in indexed and indexed[i].get("content_hash") != current_by_id[i]
    ]
    stale = [i for i in indexed if i not in current_by_id]
    say(f"reconcile: missing={len(missing)} changed={len(changed)} stale={len(stale)}")

    to_reprocess = set(missing) | set(changed)
    units_to_reprocess = [u for u in units if load.point_id(u.channel_id, u.thread_id) in to_reprocess]

    written = await process_units(es, units_to_reprocess, say)

    for doc_id in stale:
        es.delete(index=config.INDEX, id=doc_id, ignore_status=404)
    if stale:
        say(f"  deleted {len(stale)} stale documents")
    return {"mode": "reconcile", "missing": len(missing), "changed": len(changed),
            "stale": len(stale), "indexed": written}


# --- where the messages come from -------------------------------------------------


class SourceUnavailable(RuntimeError):
    """The configured source cannot be read, with the plain-language reason why.

    The CLI turns this into an exit with the verdict printed; the service turns it
    into a 503 the panel shows. Neither ever falls back to seed data -- silently
    indexing the demo corpus into someone's real index is worse than refusing.
    """


@dataclass
class Source:
    """A ready-to-read message source, plus what to stamp it as and how to let go."""
    load: Loader
    label: str
    detail: dict = field(default_factory=dict)
    close: Callable[[], None] = lambda: None


def slack_source(say: Say = print) -> Source:
    """Verify the token can read the channels, then return a live loader.

    Raises `SourceUnavailable` with the verdict if it cannot -- before any
    Elasticsearch or OpenAI work, so a person without access is told so rather than
    billed for it.
    """
    if not config.SLACK_BOT_TOKEN:
        raise SourceUnavailable(slack_check.NO_TOKEN_HELP)

    say("checking Slack access...")
    client = SlackClient(config.SLACK_BOT_TOKEN)
    report = slack_check.check_access(
        client, config.SLACK_TEAM_ID, config.SLACK_BOT_CHANNEL_IDS, say=say,
        private=False, join_public=True,
    )
    if not report.ok:
        client.close()
        raise SourceUnavailable("\n".join(slack_check.verdict_lines(report)))
    # `private=False` is the whole shared/private boundary, in one argument. This
    # source feeds the company Elasticsearch, so it reads public channels and no
    # others -- a private channel this token can see goes to the person's own
    # machine instead (`provenance.local_agent`). The filter lives in `load_slack`
    # rather than here so that every caller gets it, not just this one.
    shared = report.readable(private=False)
    say(f"  [ok] shared index: {len(shared)} public channel(s)")
    return Source(
        load=lambda oldest: slack_live.load_slack(client, report, oldest,
                                                  private=False, say=say),
        label="slack",
        detail={"workspace": config.SLACK_WORKSPACE, "team_id": config.SLACK_TEAM_ID,
                "channels": [c.channel_id for c in shared], "scope": "public"},
        close=client.close,
    )


def export_source(export_dir: str | Path) -> Source:
    path = Path(export_dir)
    return Source(
        load=lambda _oldest: load_export(path),   # an export ignores `oldest`
        label="export",
        detail={"export_dir": str(path.resolve())},
    )


def default_source(say: Say = print) -> Source:
    """The source `USE_MOCK_DATA` selects -- what the service's /ingest/sync uses.

    The flag stays the single switch between the seed demo and a real workspace, so
    the Backfill button reads whatever the rest of the process is already serving.
    """
    if config.USE_MOCK_DATA:
        return export_source(config.SEED_SLACK)
    return slack_source(say)


def corpus_conflict(es, intended_label: str) -> str | None:
    """Why `intended_label` must not be read into this index, or None if it may be.

    Nothing *collides* when two corpora share an index -- ids are per-conversation, so
    the seed fixture and a real workspace simply both end up in there, answering
    queries together while the source stamp and /health still name one of them. That
    is worse than a collision, because nothing reports it. Learned the hard way: a
    stray `--source export` run against a live index put nine seed threads into a real
    workspace's evidence, and the only clue was a channel count that had grown.

    A `backfill --recreate` is exempt -- dropping the index is an explicit request to
    replace the corpus, which is the supported way to switch.
    """
    try:
        stamped = load.read_source(es)
    except Exception:
        return None                      # unreachable index is not this check's business
    if not stamped or stamped.get("source") in (None, intended_label):
        return None
    return (
        f"this index was built from {stamped['source']!r}, but this run would read "
        f"{intended_label!r} into it. Rebuild from the source you want "
        f"(`make ingest` or `make ingest-slack`), or pass --recreate to replace it."
    )


def coverage(checkpoint_path: Path) -> dict:
    """What the last sync covered: per channel, and the oldest of those.

    The floor rather than the newest: a sync is only complete through the point
    *every* channel has reached, and reporting the leading channel would claim
    coverage the laggard does not have.
    """
    state = checkpoint_store.load(checkpoint_path)
    channels = {
        name: float(entry.get("last_ts", 0.0))
        for name, entry in state.items()
        if isinstance(entry, dict)
    }
    return {
        "channels": channels,
        "covered_through": min(channels.values()) if channels else None,
        "never_run": not channels,
    }
