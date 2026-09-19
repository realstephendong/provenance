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

from ..integrations import github
from ..models import BlameInfo, CommitInfo

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

    Three strategies, in order: the squash-merge subject convention (`... (#4821)`),
    then merge-commit ancestry (`Merge pull request #4821 ...`), then the GitHub
    adapter's own sha->PR index. The first two read only local git, so they work with
    no network and no credentials; the third catches the case both conventions miss --
    a rebase-merged commit, whose subject keeps no PR marker and which no merge commit
    is an ancestor of. Returns None when all three fail; plenty of commits genuinely
    have no PR.
    """
    try:
        subject = _git(repo_root, "log", "-1", "--format=%s", sha).strip()
    except Exception:
        subject = ""
    match = _SQUASH_SUBJECT.search(subject)
    if match:
        return int(match.group(1))

    try:
        ancestry = _git(
            repo_root, "log", "--merges", "--ancestry-path", "--reverse",
            "--format=%H %s", f"{sha}..HEAD",
        )
    except Exception:
        ancestry = ""
    for line in ancestry.splitlines():
        match = _MERGE_SUBJECT.search(line)
        if match:
            return int(match.group(1))

    # Last resort: ask the forge. Never raises past here -- the adapter returns None
    # on a miss and swallows its own transport errors.
    try:
        pr = github.lookup_by_sha(sha)
    except Exception:
        return None
    return pr.get("number") if pr else None


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

    # most_common() orders by lines owned, so the dominant commit is simply the first.
    ordered = committed.most_common()
    dominant_full = ordered[0][0]

    def _date(ts: float | None) -> str | None:
        return (
            datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            if ts else None
        )

    commits: list[CommitInfo] = []
    for full_sha, lines in ordered:
        info = meta.get(full_sha, {})
        ts = info.get("author_time")
        commits.append(CommitInfo(
            sha=full_sha[:7],
            author=info.get("author"),
            date=_date(ts),
            ts=ts,
            lines=lines,
            # One resolution per commit, reused below -- sha_to_pr shells out to git,
            # so the old code's separate pass for the dominant sha was a wasted call.
            pr_number=sha_to_pr(repo_root, full_sha),
            dominant=full_sha == dominant_full,
        ))

    authors: list[str] = []
    for c in commits:
        if c.author and c.author not in authors:
            authors.append(c.author)

    pr_numbers: list[int] = []
    for c in commits:
        if c.pr_number is not None and c.pr_number not in pr_numbers:
            pr_numbers.append(c.pr_number)

    return BlameInfo(
        authors=authors,
        dominant_sha=commits[0].sha,
        all_shas=[c.sha for c in commits],
        pr_number=commits[0].pr_number,
        pr_numbers=pr_numbers,
        commit_date=commits[0].date,
        commit_ts=commits[0].ts,
        uncommitted=UNCOMMITTED_SHA in counts,
        commits=commits,
    )
