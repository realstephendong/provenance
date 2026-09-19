"""Ingest entrypoint: Slack export -> Elasticsearch, in three modes.

    python -m provenance.ingest --export seed/slack --mode backfill [--recreate]
    python -m provenance.ingest --export seed/slack --mode incremental
    python -m provenance.ingest --export seed/slack --mode reconcile

All three share one pipeline (segment -> extract -> summarize -> embed -> load) and
differ only in which units they feed it. Because `load.point_id` is deterministic,
every mode is idempotent: re-processing a unit overwrites its document in place.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .. import config
from . import checkpoint as checkpoint_store
from . import load, summarize
from .embed import embed_summaries
from .extract import extract_refs
from .segment import Unit, group_by_channel, segment
from .slack_source import Message, load_export


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


async def run_backfill(export_dir: Path, checkpoint_path: Path, recreate: bool) -> None:
    es = load.client()
    load.ensure_index(es, recreate=recreate)

    messages = load_export(export_dir)
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


async def run_incremental(export_dir: Path, checkpoint_path: Path) -> None:
    es = load.client()
    load.ensure_index(es)

    all_messages = load_export(export_dir)
    state = checkpoint_store.load(checkpoint_path)

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


async def run_reconcile(export_dir: Path) -> None:
    """Drift detection between the export and the index.

    Stands in for "webhooks got missed" in a system with no live webhooks to miss.
    Known scaling limit, stated rather than solved: this re-segments the entire
    export on every run. That is cheap (no LLM calls) but O(total messages); at real
    scale you would shard by channel and date with per-channel cursors.
    """
    es = load.client()
    load.ensure_index(es)

    all_messages = load_export(export_dir)
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


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m provenance.ingest", description=__doc__)
    parser.add_argument("--export", required=True, help="path to the Slack export directory")
    parser.add_argument("--mode", default="backfill", choices=["backfill", "incremental", "reconcile"])
    parser.add_argument("--recreate", action="store_true",
                        help="drop and rebuild the index first (backfill only)")
    parser.add_argument("--checkpoint", default=config.INGEST_CHECKPOINT_FILE)
    args = parser.parse_args()

    if args.recreate and args.mode != "backfill":
        parser.error("--recreate is only valid with --mode backfill")

    # 18 row 1: refuse to boot without a key, rather than fail on the first call
    # after an expensive segmentation pass.
    try:
        config.require_api_key()
    except config.MissingAPIKey as exc:
        sys.exit(str(exc))

    export_dir = Path(args.export)
    checkpoint_path = Path(args.checkpoint)

    if args.mode == "backfill":
        asyncio.run(run_backfill(export_dir, checkpoint_path, args.recreate))
    elif args.mode == "incremental":
        asyncio.run(run_incremental(export_dir, checkpoint_path))
    else:
        asyncio.run(run_reconcile(export_dir))


if __name__ == "__main__":
    main()
