#!/usr/bin/env python3
"""Eval harness against a running Provenance service.

    python evals/run_eval.py                # full suite over HTTP
    python evals/run_eval.py --calibrate    # suggest a NULL_THRESHOLD

Checks, per 16:
  (a) expected evidence appears, and at what rank;
  (b) the null case returns zero results and a `message`;
  (c) the conflict case has a non-empty `conflicts` list and at least one
      CONFLICTS_WITH / SUPERSEDES edge in the graph.

`--calibrate` runs in-process rather than over HTTP: the absolute dense score the
threshold compares against is an internal retrieval value and is deliberately not
part of the /context contract, so calibration calls retrieve.retrieve() directly.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals.queries import QUERIES                       # noqa: E402
from provenance import config                            # noqa: E402

DEFAULT_REPO = config.REPO_ROOT / "seed" / "repo"
TIMEOUT = 120

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"


def _read_range(repo: Path, file_path: str, start: int, end: int) -> str:
    path = repo / file_path
    if not path.is_file():
        sys.exit(f"eval fixture missing: {path} (run `make seed`)")
    return "".join(path.read_text().splitlines(keepends=True)[start - 1:end])


def _request(repo: Path, file_path: str, start: int, end: int) -> dict:
    return {
        "code": _read_range(repo, file_path, start, end),
        "file_path": file_path,
        "repo_root": str(repo),
        "line_start": start,
        "line_end": end,
    }


def _check(label: str, ok: bool, detail: str, failures: list[str]) -> None:
    status = PASS if ok else FAIL
    print(f"    [{status}] {label}: {detail}")
    if not ok:
        failures.append(f"{label}: {detail}")


def run_suite(repo: Path, service_url: str) -> int:
    failures: list[str] = []

    for (label, file_path, start, end, expected, expect_null, expect_conflict) in QUERIES:
        print(f"\n  {label}\n    {file_path}:{start}-{end}")
        try:
            resp = httpx.post(
                f"{service_url}/context",
                json=_request(repo, file_path, start, end), timeout=TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            _check("request", False, str(exc), failures)
            continue

        results = data.get("results") or []
        found = [(r.get("channel_name"), r.get("date")) for r in results]

        if expect_null:
            _check("returns no results", not results, f"got {len(results)}", failures)
            _check("returns a message", bool(data.get("message")),
                   repr(data.get("message")), failures)
        else:
            for channel, marker in expected:
                rank = None
                for i, r in enumerate(results, 1):
                    if r.get("channel_name") != channel:
                        continue
                    hay = f"{r.get('summary', '')} {r.get('raw_text', '')}".lower()
                    if marker.lower() in hay:
                        rank = i
                        break
                if rank is not None:
                    _check(f"evidence #{channel} ~ {marker!r}", True, f"rank {rank}", failures)
                else:
                    _check(f"evidence #{channel} ~ {marker!r}", False,
                           f"absent (got {found})", failures)

            _check("synthesis present", bool(data.get("synthesis")),
                   (data.get("synthesis") or "")[:80] + "...", failures)

            exact = [r for r in results if r.get("match_type") == "exact"]
            print(f"    [{PASS if exact else WARN}] exact-tier hits: {len(exact)}")

        if expect_conflict:
            conflicts = data.get("conflicts") or []
            _check("conflicts reported", bool(conflicts), str(conflicts), failures)
            edges = [
                e for e in (data.get("graph") or {}).get("edges", [])
                if e.get("type") in ("CONFLICTS_WITH", "SUPERSEDES")
            ]
            _check("conflict edge in graph", bool(edges),
                   str([e.get("type") for e in edges]), failures)

        timings = data.get("timing_ms") or {}
        if timings:
            print(f"    timings: {'  '.join(f'{k}={v}ms' for k, v in timings.items())}")

    print()
    if failures:
        print(f"{len(failures)} check(s) failed:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("all checks passed")
    return 0


async def _best_dense(repo: Path, file_path: str, start: int, end: int) -> float:
    from provenance.ingest import load
    from provenance.service import gitctx, query_build, retrieve

    code = _read_range(repo, file_path, start, end)
    # with_history, to match exactly what POST /context feeds retrieval -- a threshold
    # calibrated against a narrower blame than production uses is not a calibration.
    blame = gitctx.blame(str(repo), file_path, start, end, with_history=True)
    queries = await query_build.build_queries(code, file_path, None)
    found = await retrieve.retrieve(load.client(), queries, blame, file_path)
    return found["best_dense"]


def run_calibrate(repo: Path) -> int:
    """Set the threshold to the midpoint between the best null score and the worst
    still-relevant score (5)."""
    null_scores: list[tuple[str, float]] = []
    hit_scores: list[tuple[str, float]] = []

    for (label, file_path, start, end, _expected, expect_null, _c) in QUERIES:
        score = asyncio.run(_best_dense(repo, file_path, start, end))
        bucket = null_scores if expect_null else hit_scores
        bucket.append((label, score))
        kind = "null" if expect_null else "hit "
        print(f"  {kind}  dense={score:.4f}  {label}")

    if not null_scores or not hit_scores:
        print("\nneed at least one null case and one hit case to calibrate")
        return 1

    best_null = max(s for _, s in null_scores)
    worst_hit = min(s for _, s in hit_scores)
    print(f"\n  best null score:       {best_null:.4f}")
    print(f"  worst relevant score:  {worst_hit:.4f}")
    print(f"  current NULL_THRESHOLD: {config.NULL_THRESHOLD}")

    if worst_hit <= best_null:
        print("\n  ! these overlap -- no threshold separates them. The corpus, the")
        print("    code-to-prose prompt, or the summary prompt needs work before")
        print("    NULL_THRESHOLD can mean anything.")
        return 1

    suggested = (best_null + worst_hit) / 2
    print(f"\n  suggested NULL_THRESHOLD = {suggested:.4f}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=str(DEFAULT_REPO))
    parser.add_argument("--service", default=config.SERVICE_URL)
    parser.add_argument("--calibrate", action="store_true")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    if args.calibrate:
        print(f"calibrating against {repo}\n")
        return run_calibrate(repo)

    print(f"eval suite -> {args.service}  (repo: {repo})")
    return run_suite(repo, args.service)


if __name__ == "__main__":
    raise SystemExit(main())
