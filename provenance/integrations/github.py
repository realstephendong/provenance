"""GitHub adapter. Fixtures while USE_MOCK_DATA is on, the real API once it is off
and GITHUB_TOKEN + GITHUB_REPO are set.

Callers only ever use `lookup_by_pr` / `lookup_by_sha` and only ever read the keys
`_normalize` guarantees, so which backend answered is invisible to them.

The two backends never mix. With the flag off and no credentials these return
nothing, which the graph already renders as "that PR is not on the map" -- far better
than a real repository's PR silently answered with a seed fixture's title.
"""

from __future__ import annotations

import json

from .. import config
from . import _live

_CACHE: list[dict] | None = None


def _load() -> list[dict]:
    global _CACHE
    if _CACHE is None:
        path = config.SEED_DIR / "mock_integrations" / "github_prs.json"
        try:
            _CACHE = json.loads(path.read_text()) if path.exists() else []
        except (json.JSONDecodeError, OSError):
            _CACHE = []
    return _CACHE


# --- live backend -------------------------------------------------------------

def live_enabled() -> bool:
    return bool(config.GITHUB_TOKEN and config.GITHUB_REPO)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {config.GITHUB_TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _normalize(pr: dict) -> dict:
    """REST shape -> fixture shape.

    `files` and `commit_shas` are left empty: each needs its own extra request, and
    no caller reads them off a live result -- the sha->PR direction is answered by
    the `/commits/{sha}/pulls` endpoint below rather than by scanning a sha list.

    `merge_commit_sha` is the exception, because it arrives free on this payload and
    `sentry_issues` needs it: it is the identifier the deployed code was released
    under, and therefore the only thing that ties a PR to the incidents it caused.
    None on an unmerged PR and absent from the fixtures, both of which that adapter
    handles as "no incidents".
    """
    return {
        "number": pr.get("number"),
        "title": pr.get("title"),
        "author": (pr.get("user") or {}).get("login"),
        "merged_at": pr.get("merged_at"),
        "merge_commit_sha": pr.get("merge_commit_sha"),
        "files": [],
        "commit_shas": [],
    }


def _live_by_pr(pr_number: int) -> dict | None:
    data = _live.get_json(
        f"{config.GITHUB_API}/repos/{config.GITHUB_REPO}/pulls/{pr_number}",
        headers=_headers(),
    )
    return _normalize(data) if isinstance(data, dict) else None


def _live_by_sha(sha: str) -> dict | None:
    # This is an exact join, not a guess: GitHub itself knows which PRs contain a
    # given commit. It is also the only one of the three trackers that does.
    data = _live.get_json(
        f"{config.GITHUB_API}/repos/{config.GITHUB_REPO}/commits/{sha}/pulls",
        headers=_headers(),
    )
    if isinstance(data, list) and data:
        merged = [pr for pr in data if pr.get("merged_at")]
        return _normalize(merged[0] if merged else data[0])
    return None


# --- public interface ---------------------------------------------------------

def lookup_by_pr(pr_number: int) -> dict | None:
    if not config.USE_MOCK_DATA:
        return _live_by_pr(pr_number) if live_enabled() else None
    for pr in _load():
        if pr["number"] == pr_number:
            return pr
    return None


def lookup_by_sha(sha: str) -> dict | None:
    if not sha:
        return None
    if not config.USE_MOCK_DATA:
        return _live_by_sha(sha) if live_enabled() else None
    for pr in _load():
        if any(s.startswith(sha) or sha.startswith(s) for s in pr.get("commit_shas", [])):
            return pr
    return None
