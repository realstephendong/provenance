"""Mocked error-tracker adapter.

`lookup_by_pr` returns a *list*: a PR can relate to more than one incident.
"""

from __future__ import annotations

import json

from .. import config

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


def lookup_by_pr(pr_number: int) -> list[dict]:
    return [i for i in _load() if i.get("pr_number") == pr_number]


def lookup_by_id(issue_id: str) -> dict | None:
    for i in _load():
        if i.get("id") == issue_id:
            return i
    return None
