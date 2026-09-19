"""Mocked ticket-tracker adapter.

`lookup_by_pr` returns a *list*: nothing in the real world guarantees a 1:1
PR-to-ticket mapping -- a PR can close two tickets. Callers handle zero, one, or many.
"""

from __future__ import annotations

import json

from .. import config

_CACHE: list[dict] | None = None


def _load() -> list[dict]:
    global _CACHE
    if _CACHE is None:
        path = config.SEED_DIR / "mock_integrations" / "tickets.json"
        try:
            _CACHE = json.loads(path.read_text()) if path.exists() else []
        except (json.JSONDecodeError, OSError):
            _CACHE = []
    return _CACHE


def lookup_by_pr(pr_number: int) -> list[dict]:
    return [t for t in _load() if t.get("pr_number") == pr_number]


def lookup_by_key(key: str) -> dict | None:
    for t in _load():
        if t.get("key") == key:
            return t
    return None
