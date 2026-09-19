#!/usr/bin/env python3
"""Fire the canonical demo query at a running service and print the panel.

    python scripts/demo_request.py [file] [start] [end]

Defaults to the demo selection: the retry loop in webhooks/delivery.py, whose
blame resolves to PR #4821.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT / "seed" / "repo"
SERVICE = "http://127.0.0.1:8000"

DEMO_FILE, DEMO_START, DEMO_END = "webhooks/delivery.py", 23, 68


def main() -> None:
    file_path = sys.argv[1] if len(sys.argv) > 1 else DEMO_FILE
    start = int(sys.argv[2]) if len(sys.argv) > 2 else DEMO_START
    end = int(sys.argv[3]) if len(sys.argv) > 3 else DEMO_END

    lines = (REPO / file_path).read_text().splitlines()
    code = "\n".join(lines[start - 1 : end])

    resp = httpx.post(
        f"{SERVICE}/context",
        json={
            "code": code,
            "file_path": file_path,
            "repo_root": str(REPO),
            "line_start": start,
            "line_end": end,
            "language": "python",
        },
        timeout=60,
    )
    resp.raise_for_status()
    d = resp.json()

    b = d["blame"]
    print("=" * 74)
    if b.get("dominant_sha"):
        print(
            f"Last touched by {', '.join(b['authors']) or 'unknown'}, "
            f"{b.get('commit_date')}, "
            f"{'PR #' + str(b['pr_number']) if b.get('pr_number') else 'no PR'} "
            f"({b['dominant_sha']})"
        )
    else:
        print("No git history for this selection")
    print("=" * 74)

    if d.get("message"):
        print(f"\n  {d['message']}\n")

    if d.get("synthesis"):
        print("\n" + d["synthesis"] + "\n")

    for i, r in enumerate(d["results"], 1):
        badge = "  [EXACT MATCH]" if r["match_type"] == "exact" else ""
        print(f"[{i}] #{r['channel_name']}  {r['date']}  score={r['score']}{badge}")
        print(f"    who: {', '.join(r['participants'][:5])}")
        if r["why"]:
            print(f"    why: {r['why']}")
        print(f"    {r['summary'][:150]}")
        print(f"    {r['permalink']}")
        print()

    print("timing_ms:", json.dumps(d["timing_ms"]))


if __name__ == "__main__":
    main()
