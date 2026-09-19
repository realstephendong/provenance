"""Shared plumbing for the live (non-fixture) integration backends.

Every adapter in this package has two backends, and `config.USE_MOCK_DATA` picks
between them for all of them at once:

  * the JSON fixtures under `seed/mock_integrations` -- the default. Offline,
    deterministic, no credentials, single-digit-millisecond.
  * a real HTTP API, which additionally needs that adapter's credentials in `.env`.

The backends never mix. A live call that fails returns None, which every caller reads
as "no node for this one" -- not as "use the fixture", because answering a real
repository's PR with seed data would fabricate the one thing this tool exists to
establish. Failing soft still matters, though: these lookups run inside
`resolve_graph`, on the request path, and an answer missing its ticket is still a
useful answer whereas a 500 from Jira that propagates is a broken feature. Nothing in
this module raises.

The linkage each adapter needs -- "which incidents relate to PR #4821?" -- is not a
first-class concept in Sentry or Jira the way it is in our fixtures. Where the live
implementation has to guess, it says so at the call site rather than pretending the
join is exact.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from .. import config

# url+params -> (fetched_at, payload). Successes only.
_cache: dict[tuple, tuple[float, Any]] = {}
# host -> monotonic time of the last failure, to avoid re-dialling a dead API once
# per node on every single request.
_cooldown: dict[str, float] = {}


def _host(url: str) -> str:
    return url.split("/", 3)[2] if "://" in url else url


def get_json(
    url: str,
    *,
    headers: dict | None = None,
    params: dict | None = None,
    auth: tuple[str, str] | None = None,
) -> Any | None:
    """GET and decode JSON. Returns None on *any* failure, which every caller reads as
    "this lookup contributes no node". Only ever reached with USE_MOCK_DATA off -- the
    adapters answer from fixtures before dialling anything."""
    key = (url, tuple(sorted((params or {}).items())))
    now = time.monotonic()

    hit = _cache.get(key)
    if hit and now - hit[0] < config.INTEGRATION_CACHE_TTL_SECONDS:
        return hit[1]

    host = _host(url)
    last_failure = _cooldown.get(host)
    if last_failure and now - last_failure < config.INTEGRATION_FAILURE_COOLDOWN_SECONDS:
        return None

    try:
        response = httpx.get(
            url,
            headers={"Accept": "application/json", **(headers or {})},
            params=params,
            auth=auth,
            timeout=config.INTEGRATION_TIMEOUT_SECONDS,
            follow_redirects=True,
        )
    except Exception as exc:
        _cooldown[host] = now
        print(f"  ! {host} unreachable, skipping this lookup: {exc}")
        return None

    # A 404 is a legitimate miss (that PR has no ticket), not an outage -- it must not
    # put the whole host into cooldown.
    if response.status_code == 404:
        _cache[key] = (now, None)
        return None
    if response.status_code >= 400:
        _cooldown[host] = now
        print(f"  ! {host} returned {response.status_code}, skipping this lookup")
        return None

    try:
        payload = response.json()
    except Exception:
        _cooldown[host] = now
        print(f"  ! {host} returned a non-JSON body, skipping this lookup")
        return None

    _cache[key] = (now, payload)
    return payload


def reset_cache() -> None:
    """Test hook. Nothing in the request path calls this."""
    _cache.clear()
    _cooldown.clear()
