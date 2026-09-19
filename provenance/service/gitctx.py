"""git blame and PR resolution -- the structural half of the join (2.3).

Nothing here ever raises past this module. Every failure mode in 18 rows 13-15
(no repo, no git binary, uncommitted file, partially uncommitted range) resolves to a
safe `BlameInfo`, because a missing commit anchor degrades retrieval quality but must
never fail the request.
"""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from datetime import datetime, timezone

from ..models import BlameInfo

UNCOMMITTED_SHA = "0" * 40

_BLAME_HEADER = re.compile(r"^([0-9a-f]{40}) (\d+) (\d+)(?: (\d+))?$")
_SQUASH_SUBJECT = re.compile(r"\(#(\d+)\)\s*$")
_MERGE_SUBJECT = re.compile(r"Merge pull request #(\d+)")

_TIMEOUT = 15


def _git(repo_root: str, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", repo_root, *args],
        capture_output=True, text=True, check=True, timeout=_TIMEOUT,
    )
    return result.stdout


def _parse_porcelain(output: str) -> tuple[Counter, dict[str, dict]]:
    """-> (lines-per-sha, {sha: {"author": str, "author_time": float}})."""
    counts: Counter = Counter()
    meta: dict[str, dict] = {}
    current: str | None = None

    for line in output.splitlines():
        header = _BLAME_HEADER.match(line)
        if header:
            # One header line per blamed line, so counting headers counts lines.
            current = header.group(1)
            counts[current] += 1
            meta.setdefault(current, {})
            continue
        if current is None:
            continue
        if line.startswith("author "):
            meta[current].setdefault("author", line[len("author "):].strip())
        elif line.startswith("author-time "):
            try:
                meta[current]["author_time"] = float(line[len("author-time "):].strip())
            except ValueError:
                pass
    return counts, meta


def sha_to_pr(repo_root: str, sha: str) -> int | None:
    """Resolve a commit to its pull request.

    Two strategies, in order: the squash-merge subject convention (`... (#4821)`),
    then merge-commit ancestry (`Merge pull request #4821 ...`). Returns None when
    neither applies -- plenty of commits genuinely have no PR.
    """
    try:
        subject = _git(repo_root, "log", "-1", "--format=%s", sha).strip()
    except Exception:
        return None
    match = _SQUASH_SUBJECT.search(subject)
    if match:
        return int(match.group(1))

    try:
        ancestry = _git(
            repo_root, "log", "--merges", "--ancestry-path", "--reverse",
            "--format=%H %s", f"{sha}..HEAD",
        )
    except Exception:
        return None
    for line in ancestry.splitlines():
        match = _MERGE_SUBJECT.search(line)
        if match:
            return int(match.group(1))
    return None


def blame(repo_root: str, file_path: str, line_start: int, line_end: int) -> BlameInfo:
    if not repo_root or not file_path:
        return BlameInfo()

    try:
        output = _git(
            repo_root, "blame", f"-L{line_start},{line_end}", "--porcelain", "--", file_path
        )
    except Exception:
        # No repo, not a git dir, file never committed, git binary missing, timeout.
        # 18 rows 13-14.
        return BlameInfo()

    counts, meta = _parse_porcelain(output)
    if not counts:
        return BlameInfo()

    # 18 row 15: uncommitted lines carry the all-zero sha and must not win the vote.
    committed = Counter({sha: n for sha, n in counts.items() if sha != UNCOMMITTED_SHA})
    if not committed:
        return BlameInfo(uncommitted=True)

    dominant_full, _ = committed.most_common(1)[0]
    ordered_shas = [sha for sha, _ in committed.most_common()]

    authors: list[str] = []
    for sha in ordered_shas:
        author = meta.get(sha, {}).get("author")
        if author and author not in authors:
            authors.append(author)

    pr_numbers: list[int] = []
    for sha in ordered_shas:
        pr = sha_to_pr(repo_root, sha)
        if pr is not None and pr not in pr_numbers:
            pr_numbers.append(pr)

    dominant_short = dominant_full[:7]
    commit_ts = meta.get(dominant_full, {}).get("author_time")
    commit_date = (
        datetime.fromtimestamp(commit_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        if commit_ts else None
    )

    return BlameInfo(
        authors=authors,
        dominant_sha=dominant_short,
        all_shas=[s[:7] for s in ordered_shas],
        pr_number=sha_to_pr(repo_root, dominant_full),
        pr_numbers=pr_numbers,
        commit_date=commit_date,
        commit_ts=commit_ts,
        uncommitted=UNCOMMITTED_SHA in counts,
    )
