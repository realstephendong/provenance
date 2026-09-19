"""Mocked GitHub adapter. Real adapter interface, fake data behind it.

Swapping to a real API later: replace `_load()`'s body with an authenticated HTTP
call and add response caching. Every caller only ever uses `lookup_by_pr` /
`lookup_by_sha`, so no call site changes.
"""

from __future__ import annotations

import json

from .. import config

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


def lookup_by_pr(pr_number: int) -> dict | None:
    for pr in _load():
        if pr["number"] == pr_number:
            return pr
    return None


def lookup_by_sha(sha: str) -> dict | None:
    if not sha:
        return None
    for pr in _load():
        if any(s.startswith(sha) or sha.startswith(s) for s in pr.get("commit_shas", [])):
            return pr
    return None
