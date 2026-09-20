"""Ingest entrypoint: Slack -> Elasticsearch, in three modes.

    python -m provenance.ingest --export seed/slack --mode backfill [--recreate]
    python -m provenance.ingest --export seed/slack --mode incremental
    python -m provenance.ingest --export seed/slack --mode reconcile

    python -m provenance.ingest --source slack --mode backfill [--recreate]   # live Slack

`--source export` reads a Slack export directory; `--source slack` reads the live
channel with the token in `.env` and refuses to start unless that token can read it.
Which one is the default follows `USE_MOCK_DATA`, so flipping that one flag switches
the whole system between the seed demo and a real workspace; `--source` still wins
when passed explicitly.

All modes share one pipeline (segment -> extract -> summarize -> embed -> load) and
differ only in which units they feed it. Because `load.point_id` is deterministic,
every mode is idempotent: re-processing a unit overwrites its document in place.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .. import config
from . import sync
from .slack_client import SlackError

# The modes themselves live in `sync`, so the Provenance panel's Backfill button
# (`/ingest/sync` in the service) drives exactly the same code this does.


def _source(args: argparse.Namespace) -> sync.Source:
    """The CLI's source: `sync`'s, but exiting with the verdict instead of raising."""
    if args.source == "export":
        return sync.export_source(args.export)
    try:
        return sync.slack_source()
    except sync.SourceUnavailable as exc:
        sys.exit(str(exc))


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m provenance.ingest", description=__doc__)
    default_source = "export" if config.USE_MOCK_DATA else "slack"
    parser.add_argument("--source", default=default_source, choices=["export", "slack"],
                        help="export = a Slack export directory; slack = the live channel "
                             "(needs SLACK_BOT_TOKEN in .env). "
                             f"Default follows USE_MOCK_DATA, currently {default_source!r}.")
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

    source = _source(args)
    checkpoint_path = Path(args.checkpoint)

    # Reading one corpus into an index built from the other is silent, not loud: see
    # `sync.corpus_conflict`. `--recreate` is the supported way to switch.
    if not (args.mode == "backfill" and args.recreate):
        from . import load
        conflict = sync.corpus_conflict(load.client(), source.label)
        if conflict:
            source.close()
            sys.exit(conflict)

    try:
        if args.mode == "backfill":
            asyncio.run(sync.run_backfill(source.load, checkpoint_path, args.recreate,
                                          source.label, source.detail))
        elif args.mode == "incremental":
            asyncio.run(sync.run_incremental(source.load, checkpoint_path,
                                             source_label=source.label,
                                             source_detail=source.detail))
        else:
            asyncio.run(sync.run_reconcile(source.load))
    except SlackError as exc:
        # Every mode reads the source before it writes, deletes or checkpoints anything,
        # so a failed read leaves the index and checkpoint as they were.
        sys.exit(f"Slack read failed: {exc}")
    finally:
        source.close()


if __name__ == "__main__":
    main()
