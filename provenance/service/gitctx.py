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

from .. import config
from ..integrations import github
from ..models import BlameInfo, CommitInfo

UNCOMMITTED_SHA = "0" * 40

_BLAME_HEADER = re.compile(r"^([0-9a-f]{40}) (\d+) (\d+)(?: (\d+))?$")
_SQUASH_SUBJECT = re.compile(r"\(#(\d+)\)\s*$")
_MERGE_SUBJECT = re.compile(r"Merge pull request #(\d+)")

_TIMEOUT = 15

# (repo_root, sha) -> PR number. A commit's subject never changes and a merge it is
# already an ancestor of never un-merges, so a *found* answer is immutable and safe
# to keep. A miss is not cached: a commit on an unmerged branch legitimately becomes
# part of a PR later, and history now asks this question once per commit rather than
# once per selection.
_PR_CACHE: dict[tuple[str, str], int] = {}


def _git(repo_root: str, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", repo_root, *args],
        capture_output=True, text=True, check=True, timeout=_TIMEOUT,
    )
    return result.stdout


def _git_ok(repo_root: str, *args: str) -> bool:
    """Run git for its exit status alone (`merge-base --is-ancestor` answers that way).

    False on any error as well as on a plain "no", which keeps a broken git from
    silently rewriting an attribution -- the caller treats False as "not already on
    main", the same answer it assumed before this check existed.
    """
    try:
        return subprocess.run(
            ["git", "-C", repo_root, *args], capture_output=True, timeout=_TIMEOUT,
        ).returncode == 0
    except Exception:
        return False


def _parse_porcelain(output: str) -> tuple[Counter, dict[str, dict]]:
    """-> (lines-per-sha, {sha: {"author": str, "author_time": float, "summary": str}})."""
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
        elif line.startswith("summary "):
            # Porcelain already carries the subject line, which is where the squash
            # convention hides the PR number -- reading it here saves `sha_to_pr` a
            # subprocess per commit.
            meta[current].setdefault("summary", line[len("summary "):].strip())
    return counts, meta


def _date_str(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d") if ts else None


def sha_to_pr(
    repo_root: str, sha: str, subject: str | None = None, allow_forge: bool = True,
) -> int | None:
    """Resolve a commit to its pull request.

    Three strategies, in order: the squash-merge subject convention (`... (#4821)`),
    then merge-commit ancestry (`Merge pull request #4821 ...`), then the GitHub
    adapter's own sha->PR index. The first two read only local git, so they work with
    no network and no credentials; the third catches the case both conventions miss --
    a rebase-merged commit, whose subject keeps no PR marker and which no merge commit
    is an ancestor of. Returns None when all three fail; plenty of commits genuinely
    have no PR.

    Pass `subject` when the caller already has it (blame's porcelain and the history
    walk both do) to skip the first subprocess entirely. `allow_forge=False` drops the
    third strategy, which is the only one that touches the network.
    """
    cached = _PR_CACHE.get((repo_root, sha))
    if cached is not None:
        return cached

    if subject is None:
        try:
            subject = _git(repo_root, "log", "-1", "--format=%s", sha).strip()
        except Exception:
            subject = ""
    match = _SQUASH_SUBJECT.search(subject)
    if match:
        return _remember(repo_root, sha, int(match.group(1)))

    # A merge commit belongs to its own pull request. Blame attributes a line to a
    # merge only where that line came from conflict resolution, which is rare -- but
    # the walk below starts at `sha..HEAD` and so can never find the commit itself,
    # so those lines lost their PR entirely.
    own = _MERGE_SUBJECT.search(subject)
    if own:
        return _remember(repo_root, sha, int(own.group(1)))

    try:
        ancestry = _git(
            repo_root, "log", "--merges", "--ancestry-path", "--reverse",
            "--format=%H %s", f"{sha}..HEAD",
        )
    except Exception:
        ancestry = ""
    for line in ancestry.splitlines():
        match = _MERGE_SUBJECT.search(line)
        if not match:
            continue
        # `--ancestry-path` proves the commit is an ancestor of this merge, which is
        # not the same as having arrived through it -- everything already on main is
        # an ancestor of every later merge too. A commit reachable from the merge's
        # first parent was on main before that PR branched, so the PR did not
        # introduce it, and no later merge can have introduced it either. Without
        # this, a repository's initial commit is attributed to whichever PR merged
        # first, and history surfaces exactly those old base commits.
        merge_sha = line.split(" ", 1)[0]
        if _git_ok(repo_root, "merge-base", "--is-ancestor", sha, f"{merge_sha}^1"):
            break
        return _remember(repo_root, sha, int(match.group(1)))

    # Last resort: ask the forge. Never raises past here -- the adapter returns None
    # on a miss and swallows its own transport errors.
    if not allow_forge:
        return None
    try:
        pr = github.lookup_by_sha(sha)
    except Exception:
        return None
    if pr and pr.get("number") is not None:
        return _remember(repo_root, sha, int(pr["number"]))
    return None


def _remember(repo_root: str, sha: str, pr_number: int) -> int:
    _PR_CACHE[(repo_root, sha)] = pr_number
    return pr_number


def history(repo_root: str, file_path: str, line_start: int, line_end: int) -> list[CommitInfo]:
    """Every commit that ever touched the selected lines, newest first.

    `git blame` answers "who owns these lines now". This answers "what did these lines
    used to be" -- a range rewritten twice has superseded commits that blame cannot
    see at all, and each one may carry the PR and the Slack thread explaining a
    constraint the current code dropped. Without them, a thread arguing for the *old*
    value is retrieved with nothing in the graph to mark it as settled history.

    Every commit comes back `current=False`; `blame` promotes the ones still owning
    lines. `-s` suppresses the diff body, but not on every git version that accepts
    `-L`, so the parse keys on a NUL-prefixed header line rather than on position --
    no line of a diff can begin with NUL.
    """
    try:
        output = _git(
            repo_root, "log",
            f"-L{line_start},{line_end}:{file_path}",
            f"-n{config.GIT_HISTORY_MAX_COMMITS}",
            "-s", "--format=%x00%H%x00%at%x00%an%x00%s",
        )
    except Exception:
        # No repo, path absent from HEAD, a range past the end of the file, timeout.
        return []

    out: list[CommitInfo] = []
    for line in output.splitlines():
        if not line.startswith("\0"):
            continue
        parts = line.split("\0")
        if len(parts) < 5:
            continue
        _, full_sha, author_time, author, subject = parts[:5]
        try:
            ts = float(author_time)
        except ValueError:
            ts = None
        out.append(CommitInfo(
            sha=full_sha[:7],
            author=author or None,
            date=_date_str(ts),
            ts=ts,
            lines=0,
            # Local git only. The forge fallback exists for rebase-merged commits,
            # and paying a network round-trip for each of up to
            # GIT_HISTORY_MAX_COMMITS of them would put the slowest, least
            # predictable call in the pipeline on the blame stage. A superseded
            # commit whose PR neither convention records simply has no PR node --
            # the same outcome as any other commit git cannot tie to one.
            pr_number=sha_to_pr(repo_root, full_sha, subject, allow_forge=False),
            dominant=False,
            current=False,
        ))
    return out


def blame(
    repo_root: str, file_path: str, line_start: int, line_end: int,
    with_history: bool = False,
) -> BlameInfo:
    """Who owns the selected lines, and -- with `with_history` -- who used to.

    History is opt-in because `/context/count` blames once per top-level symbol per
    file open for the CodeLens gutter, and a `git log -L` walk per lens would make
    opening a file cost a subprocess per symbol.
    """
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

    commits: list[CommitInfo] = []
    for full_sha, lines in ordered:
        info = meta.get(full_sha, {})
        ts = info.get("author_time")
        commits.append(CommitInfo(
            sha=full_sha[:7],
            author=info.get("author"),
            date=_date_str(ts),
            ts=ts,
            lines=lines,
            # One resolution per commit, reused below -- sha_to_pr shells out to git,
            # so the old code's separate pass for the dominant sha was a wasted call.
            pr_number=sha_to_pr(repo_root, full_sha, info.get("summary")),
            dominant=full_sha == dominant_full,
            current=True,
        ))

    # Authors stay the *current* owners only. They anchor the "Origin" line every
    # surface prints and the author-match boost at query time, and neither should
    # start naming someone whose version of these lines was replaced years ago.
    authors: list[str] = []
    for c in commits:
        if c.author and c.author not in authors:
            authors.append(c.author)

    dominant = next(c for c in commits if c.dominant)

    if with_history:
        known = {c.sha for c in commits}
        commits += [h for h in history(repo_root, file_path, line_start, line_end)
                    if h.sha not in known]

    # Chronological, oldest first: the order the range was actually built in, and the
    # order the graph, the CLI and the timeline all render. Undated commits sort last
    # rather than colonising the head of the story.
    commits.sort(key=lambda c: (c.ts is None, c.ts or 0.0))

    pr_numbers: list[int] = []
    for c in commits:
        if c.pr_number is not None and c.pr_number not in pr_numbers:
            pr_numbers.append(c.pr_number)

    # `all_shas` and `pr_numbers` now carry superseded commits too. Retrieval reads
    # both as sets (an ES `terms` filter and a set intersection), so the wider net is
    # the point and the new ordering costs nothing: a thread naming the PR that wrote
    # the *previous* version becomes structural evidence instead of a semantic guess.
    return BlameInfo(
        authors=authors,
        dominant_sha=dominant.sha,
        all_shas=[c.sha for c in commits],
        pr_number=dominant.pr_number,
        pr_numbers=pr_numbers,
        commit_date=dominant.date,
        commit_ts=dominant.ts,
        uncommitted=UNCOMMITTED_SHA in counts,
        commits=commits,
    )
