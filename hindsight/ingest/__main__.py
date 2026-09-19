"""Ingest CLI.

    python -m hindsight.ingest --export seed/slack --recreate

Offline. Target: the full seed workspace indexed in under 3 minutes.
"""

from __future__ import annotations

import argparse
import asyncio
import time

from .. import config
from ..models import ThreadPayload
from . import embed, load, summarize
from .extract import extract_refs
from .segment import segment
from .slack_source import load_export


def permalink(channel_id: str, thread_id: str) -> str:
    return f"https://acme.slack.com/archives/{channel_id}/p{thread_id.replace('.', '')}"


async def run(export: str, recreate: bool, dry_run: bool) -> None:
    t0 = time.time()

    messages = load_export(export)
    units = segment(messages)
    print(f"[1/5] {len(messages)} messages -> {len(units)} conversation units")

    refs = [extract_refs(u.raw_text) for u in units]
    n_pr = sum(len(r["pr_refs"]) for r in refs)
    print(f"[2/5] extracted refs: {n_pr} PR references, "
          f"{sum(len(r['file_paths']) for r in refs)} file paths")

    summaries = await summarize.summarize_units(units)
    print(f"[3/5] summarized {len(summaries)} threads ({config.SUMMARY_MODEL})")

    payloads = []
    for unit, ref, (summary, llm_symbols) in zip(units, refs, summaries):
        symbols = list(dict.fromkeys(ref["symbols"] + llm_symbols))
        payloads.append(
            ThreadPayload(
                thread_id=unit.thread_id,
                channel_id=unit.channel_id,
                channel_name=unit.channel_name,
                channel_tier=unit.channel_tier,
                permalink=permalink(unit.channel_id, unit.thread_id),
                ts_start=unit.ts_start,
                ts_end=unit.ts_end,
                participants=unit.participants,
                message_count=len(unit.messages),
                summary=summary,
                raw_text=unit.raw_text,
                pr_refs=ref["pr_refs"],
                commit_shas=ref["commit_shas"],
                ticket_refs=ref["ticket_refs"],
                file_paths=ref["file_paths"],
                symbols=symbols,
                reactions=unit.reactions,
                is_bookmarked=unit.is_bookmarked,
            ).model_dump()
        )

    dense, sparse = await embed.embed_units(
        [p["summary"] for p in payloads],
        [p["raw_text"] for p in payloads],
        [p["symbols"] for p in payloads],
    )
    print(f"[4/5] embedded {len(dense)} threads (dense {config.DENSE_DIM}-d + BM25 sparse)")

    if dry_run:
        print("[5/5] dry run, nothing written")
        for p in payloads:
            print(f"  #{p['channel_name']:15} PR{p['pr_refs']} {p['summary'][:80]}")
        return

    qc = load.client()
    load.ensure_collection(qc, recreate=recreate)
    n = load.upsert(qc, payloads, dense, sparse)
    print(f"[5/5] upserted {n} points into '{config.COLLECTION}' "
          f"(embedder {config.EMBEDDER_ID})")
    print(f"done in {time.time() - t0:.1f}s")


def main() -> None:
    ap = argparse.ArgumentParser(prog="hindsight.ingest")
    ap.add_argument("--export", default=str(config.SEED_SLACK), help="Slack export directory")
    ap.add_argument("--recreate", action="store_true", help="drop and rebuild the collection")
    ap.add_argument("--dry-run", action="store_true", help="do everything but write to Qdrant")
    args = ap.parse_args()

    try:
        config.require_api_key()
    except config.MissingAPIKey as exc:
        raise SystemExit(f"\n{exc}\n")

    asyncio.run(run(args.export, args.recreate, args.dry_run))


if __name__ == "__main__":
    main()
