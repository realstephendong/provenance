"""Ticket-tracker adapter. Fixtures by default, Jira's REST API when JIRA_BASE_URL,
JIRA_EMAIL and JIRA_TOKEN are all set.

`lookup_by_pr` returns a *list*: nothing in the real world guarantees a 1:1
PR-to-ticket mapping -- a PR can close two tickets. Callers handle zero, one, or many.

Same caveat as the error tracker: PR-to-ticket is not a plain field in Jira. The
proper source is the dev-status API, which is undocumented, needs the GitHub-for-Jira
app installed, and keys on Jira's internal issue id rather than the key -- so the
default live implementation uses a JQL text search for the PR number instead, and is
a heuristic. `lookup_by_key` is exact on both backends.
"""

from __future__ import annotations

import json

from .. import config
from . import _live

_CACHE: list[dict] | None = None
_FIELDS = "summary,status,assignee"


def _load() -> list[dict]:
    global _CACHE
    if _CACHE is None:
        path = config.SEED_DIR / "mock_integrations" / "tickets.json"
        try:
            _CACHE = json.loads(path.read_text()) if path.exists() else []
        except (json.JSONDecodeError, OSError):
            _CACHE = []
    return _CACHE


# --- live backend -------------------------------------------------------------

def live_enabled() -> bool:
    return bool(config.JIRA_BASE_URL and config.JIRA_EMAIL and config.JIRA_TOKEN)


def _auth() -> tuple[str, str]:
    return (config.JIRA_EMAIL, config.JIRA_TOKEN)


def _normalize(issue: dict, pr_number: int | None = None) -> dict:
    fields = issue.get("fields") or {}
    status = fields.get("status") or {}
    assignee = fields.get("assignee") or {}
    return {
        "key": issue.get("key"),
        "title": fields.get("summary"),
        "status": status.get("name"),
        "assignee": assignee.get("displayName"),
        "pr_number": pr_number,
    }


def _live_by_pr(pr_number: int) -> list[dict]:
    data = _live.get_json(
        f"{config.JIRA_BASE_URL}/rest/api/3/search",
        auth=_auth(),
        params={"jql": f'text ~ "#{pr_number}"', "fields": _FIELDS, "maxResults": 5},
    )
    if not isinstance(data, dict):
        return []
    issues = data.get("issues")
    if not isinstance(issues, list):
        return []
    return [_normalize(i, pr_number) for i in issues if isinstance(i, dict)]


def _live_by_key(key: str) -> dict | None:
    data = _live.get_json(
        f"{config.JIRA_BASE_URL}/rest/api/3/issue/{key}",
        auth=_auth(),
        params={"fields": _FIELDS},
    )
    return _normalize(data) if isinstance(data, dict) and data.get("key") else None


# --- public interface ---------------------------------------------------------

def lookup_by_pr(pr_number: int) -> list[dict]:
    if live_enabled():
        found = _live_by_pr(pr_number)
        if found:
            return found
    return [t for t in _load() if t.get("pr_number") == pr_number]


def lookup_by_key(key: str) -> dict | None:
    if live_enabled():
        found = _live_by_key(key)
        if found:
            return found
    for t in _load():
        if t.get("key") == key:
            return t
    return None
