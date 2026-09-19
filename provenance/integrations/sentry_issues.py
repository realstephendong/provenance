"""Error-tracker adapter. Fixtures by default, Sentry's API when SENTRY_API_TOKEN,
SENTRY_ORG and SENTRY_PROJECT are all set.

`lookup_by_pr` returns a *list*: a PR can relate to more than one incident.

A caveat the fixtures hide: "which incidents relate to PR #4821" is not a relation
Sentry models. The fixture stores `pr_number` on the issue because we control it; the
live path can only *search* for the PR number in issue text, which is a heuristic and
will both miss and over-match depending on how your team writes issue titles. If you
link incidents to PRs some other way (a release tag, a custom tag, a Linear/Jira
bridge), replace `_live_by_pr` -- that is the one function that has to know.
`lookup_by_id` has no such problem: a short id is an exact key on both backends.
"""

from __future__ import annotations

import json

from .. import config
from . import _live

_CACHE: list[dict] | None = None


def _load() -> list[dict]:
    global _CACHE
    if _CACHE is None:
        path = config.SEED_DIR / "mock_integrations" / "sentry_issues.json"
        try:
            _CACHE = json.loads(path.read_text()) if path.exists() else []
        except (json.JSONDecodeError, OSError):
            _CACHE = []
    return _CACHE


# --- live backend -------------------------------------------------------------

def live_enabled() -> bool:
    return bool(config.SENTRY_API_TOKEN and config.SENTRY_ORG and config.SENTRY_PROJECT)


def _headers() -> dict:
    return {"Authorization": f"Bearer {config.SENTRY_API_TOKEN}"}


def _normalize(issue: dict, pr_number: int | None = None) -> dict:
    return {
        "id": issue.get("shortId") or issue.get("id"),
        "title": issue.get("title"),
        "first_seen": issue.get("firstSeen"),
        "status": issue.get("status"),
        "pr_number": pr_number,
    }


def _live_by_pr(pr_number: int) -> list[dict]:
    # Heuristic, as the module docstring explains: full-text search for the PR number.
    data = _live.get_json(
        f"{config.SENTRY_API}/projects/{config.SENTRY_ORG}/{config.SENTRY_PROJECT}/issues/",
        headers=_headers(),
        params={"query": f"#{pr_number}", "statsPeriod": "90d"},
    )
    if not isinstance(data, list):
        return []
    return [_normalize(i, pr_number) for i in data if isinstance(i, dict)]


def _live_by_id(issue_id: str) -> dict | None:
    data = _live.get_json(
        f"{config.SENTRY_API}/organizations/{config.SENTRY_ORG}/shortids/{issue_id}/",
        headers=_headers(),
    )
    if isinstance(data, dict):
        group = data.get("group")
        if isinstance(group, dict):
            # The shortids endpoint nests the issue and reports the canonical short id
            # alongside it; prefer that over the one we were asked about.
            return _normalize({**group, "shortId": data.get("shortId") or issue_id})
    return None


# --- public interface ---------------------------------------------------------

def lookup_by_pr(pr_number: int) -> list[dict]:
    if live_enabled():
        found = _live_by_pr(pr_number)
        if found:
            return found
    return [i for i in _load() if i.get("pr_number") == pr_number]


def lookup_by_id(issue_id: str) -> dict | None:
    if live_enabled():
        found = _live_by_id(issue_id)
        if found:
            return found
    for i in _load():
        if i.get("id") == issue_id:
            return i
    return None
