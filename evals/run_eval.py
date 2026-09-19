#!/usr/bin/env python3
"""Check every tuning change in ten seconds.

    python evals/run_eval.py            # needs the service running

Prints the rank each expected thread landed at. A scoring change that helps
one query and quietly breaks another shows up here immediately, which is the
entire point: no tuning by vibes.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from queries import QUERIES  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT / "seed" / "repo"
SERVICE = "http://127.0.0.1:8000"

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def run_one(label, file_path, start, end, expected, expect_null):
    lines = (REPO / file_path).read_text().splitlines()
    code = "\n".join(lines[start - 1 : end])

    t0 = time.perf_counter()
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
        timeout=90,
    )
    resp.raise_for_status()
    data = resp.json()
    elapsed = time.perf_counter() - t0

    got = [(r["channel_name"], r["date"], r["match_type"]) for r in data["results"]]
    print(f"\n{label}  {DIM}({file_path}:{start}-{end}, {elapsed:.1f}s){RESET}")

    if expect_null:
        if not got:
            print(f"  {GREEN}PASS{RESET}  returned nothing: {data.get('message')}")
            return True
        print(f"  {RED}FAIL{RESET}  expected no results, got {len(got)}:")
        for c, d, m in got:
            print(f"          #{c} {d} [{m}]")
        return False

    positions = {}
    for rank, (c, d, _m) in enumerate(got, 1):
        positions.setdefault((c, d), rank)

    ok = True
    for channel, date in expected:
        rank = positions.get((channel, date))
        if rank is None:
            ok = False
            print(f"  {RED}MISS{RESET}  #{channel} {date}  {DIM}not returned{RESET}")
        else:
            mark = GREEN if rank <= len(expected) else YELLOW
            print(f"  {mark}  #{rank}{RESET}  #{channel} {date}")

    extras = [g for g in got if (g[0], g[1]) not in set(expected)]
    for c, d, m in extras:
        print(f"  {DIM}  --   #{c} {d} [{m}]  (not expected){RESET}")
    return ok


def main() -> None:
    try:
        health = httpx.get(f"{SERVICE}/health", timeout=5).json()
    except Exception as exc:
        sys.exit(f"service not reachable at {SERVICE}: {exc}\nstart it with: make serve")

    print(f"{DIM}service: points={health.get('points')} "
          f"embedder={health.get('embedder_id')}{RESET}")
    if not health.get("api_key_present"):
        sys.exit("service is running without OPENAI_API_KEY; results would be meaningless")
    if not health.get("ok"):
        print(f"{YELLOW}warning: /health is not ok: "
              f"{health.get('error') or 'collection missing or embedder mismatch'}{RESET}")

    t0 = time.perf_counter()
    passed = sum(bool(run_one(*q)) for q in QUERIES)
    total = len(QUERIES)

    colour = GREEN if passed == total else RED
    print(f"\n{colour}{passed}/{total} queries passed{RESET}  "
          f"{DIM}({time.perf_counter() - t0:.1f}s total){RESET}")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
