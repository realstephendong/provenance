"""Ingest entrypoint: Slack -> Elasticsearch, in three modes.

    python -m provenance.ingest --export seed/slack --mode backfill [--recreate]
    python -m provenance.ingest --export seed/slack --mode incremental
    python -m provenance.ingest --export seed/slack --mode reconcile

    python -m provenance.ingest --source slack --mode backfill [--recreate]   # live Slack

`--source export` (default) reads a Slack export directory; `--source slack` reads the
live channel with the token in `.env` and refuses to start unless that token can read it.

All modes share one pipeline (segment -> extract -> summarize -> embed -> load) and
differ only in which units they feed it. Because `load.point_id` is deterministic,
every mode is idempotent: re-processing a unit overwrites its document in place.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable
from pathlib import Path

from .. import config
from . import checkpoint as checkpoint_store
from . import load, slack_check, slack_live, summarize
from .embed import embed_summaries
from .extract import extract_refs
from .segment import Unit, group_by_channel, segment
from .slack_client import SlackClient, SlackError
from .slack_source import Message, load_export

# A message source: `oldest` (unix ts) -> messages. The export reader ignores `oldest`
# and always returns everything; the live reader uses it to bound the API calls.
Loader = Callable[[float], list[Message]]


def _payload(unit: Unit, summary: str, llm_symbols: list[str]) -> dict:
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
        "is_bookmarked": unit.is_bookmarked,
    }


async def process_units(es, units: list[Unit]) -> int:
    """The shared tail of every mode: summarize, embed, and write `units`."""
    if not units:
        print("  nothing to process")
        return 0

    print(f"  summarizing {len(units)} units ({config.SUMMARY_MODEL})...")
    summarized = await summarize.summarize_units(units)
    payloads = [_payload(u, s, syms) for u, (s, syms) in zip(units, summarized)]

    print(f"  embedding {len(payloads)} summaries ({config.DENSE_MODEL})...")
    dense = await embed_summaries([p["summary"] for p in payloads])

    written = load.upsert(es, payloads, dense)
    print(f"  indexed {written} documents")
    return written


# --- mode: backfill -----------------------------------------------------------


async def run_backfill(source: Loader, checkpoint_path: Path, recreate: bool) -> None:
    # Read first: with --recreate the index is dropped below, and a failed source read
    # (a Slack outage, a rate limit) must not leave the person with an empty index.
    messages = source(0.0)

    es = load.client()
    load.ensure_index(es, recreate=recreate)

    units = segment(messages)
    print(f"backfill: {len(messages)} messages -> {len(units)} units")
    await process_units(es, units)

    state: dict = {}
    for unit in units:
        entry = state.setdefault(unit.channel_name, {"last_ts": 0.0})
        entry["last_ts"] = max(entry["last_ts"], unit.ts_end)
    checkpoint_store.save(checkpoint_path, state)
    load.stamp_embedder(es)
    print(f"  checkpoint -> {checkpoint_path}")
    print(f"  embedder stamped: {config.EMBEDDER_ID}")


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
        loose_pool = [m for m in pool if m.thread_ts is None]
        loose_seeds = [m for m in channel_new if m.thread_ts is None]
        affected.extend(_burst_reachable(loose_pool, loose_seeds, config.SEGMENT_GAP_SECONDS))
    return _dedupe_by_identity(affected)


def incremental_oldest(state: dict) -> float:
    """How far back a live source must read for an incremental run.

    `affected_messages` needs the surrounding pool, not just new messages: a burst can
    continue from up to SEGMENT_GAP_SECONDS before the checkpoint, and recent threads
    can have gained replies. Threads whose parent is older than the lookback are left
    to `--mode reconcile`, as is a channel newly added to SLACK_CHANNEL_IDS.
    """
    last = [entry.get("last_ts", 0.0) for entry in state.values()]
    if not last:
        return 0.0
    margin = config.SEGMENT_GAP_SECONDS + config.SLACK_THREAD_LOOKBACK_DAYS * 86400
    return max(0.0, min(last) - margin)


async def run_incremental(source: Loader, checkpoint_path: Path) -> None:
    es = load.client()
    load.ensure_index(es)

    state = checkpoint_store.load(checkpoint_path)
    all_messages = source(incremental_oldest(state))

    new_messages = [
        m for m in all_messages
        if m.ts > state.get(m.channel_name, {}).get("last_ts", 0.0)
    ]
    if not new_messages:
        print("nothing new")
        return
    print(f"incremental: {len(new_messages)} new messages")

    affected = affected_messages(all_messages, new_messages)
    units = segment(affected)
    print(f"  {len(affected)} affected messages -> {len(units)} units to rebuild")
    await process_units(es, units)

    for cname, channel_new in group_by_channel(new_messages).items():
        state.setdefault(cname, {})["last_ts"] = max(m.ts for m in channel_new)
    checkpoint_store.save(checkpoint_path, state)
    print(f"  checkpoint -> {checkpoint_path}")


# --- mode: reconcile -----------------------------------------------------------


async def run_reconcile(source: Loader) -> None:
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

    all_messages = source(0.0)
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
    print(f"reconcile: missing={len(missing)} changed={len(changed)} stale={len(stale)}")

    to_reprocess = set(missing) | set(changed)
    units_to_reprocess = [u for u in units if load.point_id(u.channel_id, u.thread_id) in to_reprocess]
    await process_units(es, units_to_reprocess)

    for doc_id in stale:
        es.delete(index=config.INDEX, id=doc_id, ignore_status=404)
    if stale:
        print(f"  deleted {len(stale)} stale documents")


# --- entrypoint -----------------------------------------------------------------


def _slack_source() -> Loader:
    """Verify the person's token can read the channel, then return a live loader.

    Exits with the plain-language verdict if it can't -- before any Elasticsearch or
    OpenAI work, and without ever falling back to seed data.
    """
    if not config.SLACK_USER_TOKEN:
        sys.exit(slack_check.NO_TOKEN_HELP)

    print("checking Slack access...")
    client = SlackClient(config.SLACK_USER_TOKEN)
    report = slack_check.check_access(client, config.SLACK_TEAM_ID, config.SLACK_CHANNEL_IDS)
    if not report.ok:
        client.close()
        sys.exit("\n".join(slack_check.verdict_lines(report)))
    return lambda oldest: slack_live.load_slack(client, report, oldest)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m provenance.ingest", description=__doc__)
    parser.add_argument("--source", default="export", choices=["export", "slack"],
                        help="export = a Slack export directory; slack = the live channel "
                             "(needs SLACK_USER_TOKEN in .env)")
    parser.add_argument("--export", help="path to the Slack export directory (--source export)")
    parser.add_argument("--mode", default="backfill", choices=["backfill", "incremental", "reconcile"])
    parser.add_argument("--recreate", action="store_true",
                        help="drop and rebuild the index first (backfill only)")
    parser.add_argument("--checkpoint", default=config.INGEST_CHECKPOINT_FILE)
    args = parser.parse_args()

    if args.recreate and args.mode != "backfill":
        parser.error("--recreate is only valid with --mode backfill")
    if args.source == "export" and not args.export:
        parser.error("--export is required with --source export")
    if args.source == "slack" and args.export:
        parser.error("--export can't be combined with --source slack")

    # 18 row 1: refuse to boot without a key, rather than fail on the first call
    # after an expensive segmentation pass.
    try:
        config.require_api_key()
    except config.MissingAPIKey as exc:
        sys.exit(str(exc))

    if args.source == "slack":
        source = _slack_source()
    else:
        export_dir = Path(args.export)
        source = lambda oldest: load_export(export_dir)  # noqa: E731 -- export ignores `oldest`
    checkpoint_path = Path(args.checkpoint)

    try:
        if args.mode == "backfill":
            asyncio.run(run_backfill(source, checkpoint_path, args.recreate))
        elif args.mode == "incremental":
            asyncio.run(run_incremental(source, checkpoint_path))
        else:
            asyncio.run(run_reconcile(source))
    except SlackError as exc:
        # Every mode reads the source before it writes, deletes or checkpoints anything,
        # so a failed read leaves the index and checkpoint as they were.
        sys.exit(f"Slack read failed: {exc}")


if __name__ == "__main__":
    main()
