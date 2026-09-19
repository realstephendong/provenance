"""git blame, and the commit -> PR join.

This module is the single highest-value output of the query pipeline. The
exact-match tier runs on the PR numbers it returns, and the exact-match tier
is the thing that makes this more than a RAG demo.
"""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from datetime import datetime, timezone

from ..models import BlameInfo

ZERO_SHA = "0" * 40

_SQUASH_PR = re.compile(r"\(#(\d+)\)\s*$")
_MERGE_PR = re.compile(r"Merge pull request #(\d+)")
_BLAME_HEADER = re.compile(r"^([0-9a-f]{40}) \d+ \d+(?: \d+)?$")


def _git(repo_root: str, *args: str, timeout: float = 5.0) -> str:
    out = subprocess.run(
        ["git", "-C", repo_root, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or f"git {' '.join(args)} failed")
    return out.stdout


def blame(repo_root: str, file_path: str, line_start: int, line_end: int) -> BlameInfo:
    """Aggregate blame over the selected range.

    Trap #4: blame returns all-zero SHAs for unstaged work. That is detected
    and reported rather than crashing; retrieval then runs purely semantic
    with no time anchor.
    """
    try:
        raw = _git(
            repo_root,
            "blame",
            f"-L{line_start},{line_end}",
            "--porcelain",
            "--",
            file_path,
        )
    except Exception:
        return BlameInfo()

    counts: Counter[str] = Counter()
    meta: dict[str, dict] = {}
    current: str | None = None

    for line in raw.splitlines():
        header = _BLAME_HEADER.match(line)
        if header:
            current = header.group(1)
            counts[current] += 1
            meta.setdefault(current, {})
            continue
        if current is None:
            continue
        if line.startswith("author "):
            meta[current].setdefault("author", line[len("author ") :].strip())
        elif line.startswith("author-time "):
            meta[current].setdefault("author_time", float(line[len("author-time ") :].strip()))

    if not counts:
        return BlameInfo()

    uncommitted = ZERO_SHA in counts
    committed = {s: c for s, c in counts.items() if s != ZERO_SHA}
    if not committed:
        return BlameInfo(uncommitted=True)

    dominant = max(committed, key=lambda s: committed[s])
    commit_ts = meta.get(dominant, {}).get("author_time")

    authors = []
    for sha in committed:
        a = meta.get(sha, {}).get("author")
        if a and a not in authors:
            authors.append(a)

    all_shas = [s[:7] for s in sorted(committed, key=lambda s: -committed[s])]
    pr_numbers = []
    for sha in committed:
        pr = sha_to_pr(repo_root, sha)
        if pr and pr not in pr_numbers:
            pr_numbers.append(pr)

    dominant_pr = sha_to_pr(repo_root, dominant)
    # The dominant commit's PR ranks first; it is the strongest signal.
    if dominant_pr and pr_numbers and pr_numbers[0] != dominant_pr:
        pr_numbers.remove(dominant_pr)
        pr_numbers.insert(0, dominant_pr)

    return BlameInfo(
        authors=authors,
        dominant_sha=dominant[:7],
        all_shas=all_shas,
        pr_number=dominant_pr,
        pr_numbers=pr_numbers,
        commit_date=(
            datetime.fromtimestamp(commit_ts, tz=timezone.utc).strftime("%Y-%m-%d")
            if commit_ts
            else None
        ),
        commit_ts=commit_ts,
        uncommitted=uncommitted,
    )


def sha_to_pr(repo_root: str, sha: str) -> int | None:
    """Two strategies, first hit wins."""
    # 1. Squash merges: the PR number is in the commit subject.
    try:
        subject = _git(repo_root, "log", "-1", "--format=%s", sha).strip()
        m = _SQUASH_PR.search(subject)
        if m:
            return int(m.group(1))
    except Exception:
        return None

    # 2. Merge commits: the first merge that can reach this commit.
    try:
        out = _git(
            repo_root,
            "log",
            "--merges",
            "--ancestry-path",
            "--reverse",
            "--format=%H %s",
            f"{sha}..HEAD",
        )
        for line in out.splitlines():
            m = _MERGE_PR.search(line)
            if m:
                return int(m.group(1))
    except Exception:
        pass

    return None
