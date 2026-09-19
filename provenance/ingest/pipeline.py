"""summarize -> embed -> upsert: the tail every ingest path shares.

Batch ingest (`provenance.ingest.__main__`) feeds it whole channels; the Slack bot
(`provenance.slackbot`) feeds it one conversation at a time. Keeping the tail in one
place is what stops those two drifting into two subtly different indexers.
"""

from __future__ import annotations

from collections.abc import Callable

from .. import config
from . import load, summarize
from .embed import embed_summaries
from .segment import Unit


async def build_and_upsert(
    es,
    units: list[Unit],
    *,
    bookmark: bool = False,
    say: Callable[[str], None] = lambda _: None,
) -> tuple[list[dict], int]:
    """-> (the documents written, how many Elasticsearch accepted).

    `bookmark` marks every unit as human-flagged; see `load.build_payload`.
    """
    if not units:
        return [], 0

    say(f"  summarizing {len(units)} units ({config.SUMMARY_MODEL})...")
    summarized = await summarize.summarize_units(units)
    payloads = [
        load.build_payload(u, s, syms, bookmark=bookmark)
        for u, (s, syms) in zip(units, summarized)
    ]

    say(f"  embedding {len(payloads)} summaries ({config.DENSE_MODEL})...")
    dense = await embed_summaries([p["summary"] for p in payloads])

    written = load.upsert(es, payloads, dense)
    return payloads, written
