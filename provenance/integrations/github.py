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
import time
from contextvars import ContextVar, Token
from pathlib import Path

import httpx
import jwt

from .. import config
from . import _live

_CACHE: list[dict] | None = None
_repository: ContextVar[str] = ContextVar("github_repository", default="")
_app_tokens: dict[str, tuple[float, str]] = {}


def set_repository(repository: str) -> Token:
    """Bind this request to an `owner/repo` without global cross-request state."""
    return _repository.set(repository.strip().strip("/"))


def reset_repository(token: Token) -> None:
    _repository.reset(token)


def _repo() -> str:
    return _repository.get() or config.GITHUB_REPO


def pull_request_url(pr_number: int) -> str | None:
    repository = _repo()
    return f"{config.GITHUB_WEB_URL}/{repository}/pull/{pr_number}" if repository else None


def commit_url(sha: str) -> str | None:
    """Return the forge URL for a commit in this request's repository.

    The extension binds the workspace's origin remote with ``set_repository`` before
    resolving a graph, so this remains correct when one shared API serves many repos.
    GitHub accepts the abbreviated SHAs produced by git blame as well as full SHAs.
    """
    repository = _repo()
    return f"{config.GITHUB_WEB_URL}/{repository}/commit/{sha}" if repository and sha else None


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
    return bool(
        (config.GITHUB_TOKEN and config.GITHUB_REPO)
        or (config.GITHUB_APP_ID and config.GITHUB_APP_PRIVATE_KEY_PATH)
    )


def _headers(repository: str) -> dict:
    if config.GITHUB_APP_ID and config.GITHUB_APP_PRIVATE_KEY_PATH:
        token = _installation_token(repository)
        if token:
            return {
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            }
    return {
        "Authorization": f"Bearer {config.GITHUB_TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _app_jwt() -> str | None:
    """Short-lived JWT used only to exchange for an installation token."""
    try:
        key = Path(config.GITHUB_APP_PRIVATE_KEY_PATH).read_text()
        now = int(time.time())
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": config.GITHUB_APP_ID}, key, algorithm="RS256")
    except (OSError, ValueError, jwt.PyJWTError):
        return None


def _installation_token(repository: str) -> str | None:
    if not repository or "/" not in repository:
        return None
    cached = _app_tokens.get(repository)
    if cached and cached[0] > time.monotonic() + 60:
        return cached[1]
    app_jwt = _app_jwt()
    if not app_jwt:
        return None
    headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {app_jwt}", "X-GitHub-Api-Version": "2022-11-28"}
    try:
        installation = httpx.get(f"{config.GITHUB_API}/repos/{repository}/installation", headers=headers, timeout=config.INTEGRATION_TIMEOUT_SECONDS)
        if installation.status_code >= 400:
            return None
        installation_id = installation.json().get("id")
        minted = httpx.post(f"{config.GITHUB_API}/app/installations/{installation_id}/access_tokens", headers=headers, timeout=config.INTEGRATION_TIMEOUT_SECONDS)
        if minted.status_code >= 400:
            return None
        token = minted.json().get("token")
        if not isinstance(token, str) or not token:
            return None
    except (httpx.HTTPError, ValueError, TypeError):
        return None
    # Installation tokens last one hour. Cache conservatively; the next request
    # simply mints another if GitHub expires it sooner.
    _app_tokens[repository] = (time.monotonic() + 50 * 60, token)
    return token


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
    repository = _repo()
    if not repository:
        return None
    data = _live.get_json(
        f"{config.GITHUB_API}/repos/{repository}/pulls/{pr_number}",
        headers=_headers(repository),
    )
    return _normalize(data) if isinstance(data, dict) else None


def _live_by_sha(sha: str) -> dict | None:
    # This is an exact join, not a guess: GitHub itself knows which PRs contain a
    # given commit. It is also the only one of the three trackers that does.
    repository = _repo()
    if not repository:
        return None
    data = _live.get_json(
        f"{config.GITHUB_API}/repos/{repository}/commits/{sha}/pulls",
        headers=_headers(repository),
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
