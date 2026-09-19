"""Minimal Slack Web API client for the live ingest path.

Deliberately not built on `integrations/_live.get_json`: that helper swallows every
failure and returns None so an adapter can fall back to a fixture. Here a failed read
must be loud -- silently ingesting nothing (or seed data) after an auth or rate-limit
failure would corrupt the index without anyone noticing.

The token is only ever sent in the Authorization header. It is never logged, and no
exception message contains it; errors carry the Slack error code only.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx

from .. import config

_MAX_RETRY_AFTER_SECONDS = 300.0


class SlackError(RuntimeError):
    """A Slack call failed. `code` is Slack's error string (e.g. `channel_not_found`,
    `missing_scope`, `invalid_auth`) or a `network_error:*` / `ratelimited` marker."""

    def __init__(self, method: str, code: str, needed: str | None = None):
        self.method = method
        self.code = code
        self.needed = needed          # for missing_scope: the scope Slack says it wanted
        detail = f" (needs scope: {needed})" if needed else ""
        super().__init__(f"slack {method}: {code}{detail}")


class SlackClient:
    def __init__(
        self,
        token: str,
        *,
        base_url: str | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._base = (base_url or config.SLACK_API).rstrip("/")
        self._sleep = sleep
        self._http = httpx.Client(
            headers={"Authorization": f"Bearer {token}"},
            timeout=config.SLACK_TIMEOUT_SECONDS,
            transport=transport,
        )

    def __enter__(self) -> "SlackClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _backoff(self, attempt: int) -> float:
        return config.LLM_RETRY_BASE_SECONDS * (2 ** attempt)

    def call(self, method: str, **params: Any) -> dict:
        """POST one Web API method and return the decoded body. Retries HTTP 429
        (honouring Retry-After), 5xx and transport errors; raises `SlackError`
        otherwise, including when Slack answers `ok: false`."""
        url = f"{self._base}/{method}"
        last = config.SLACK_MAX_RETRIES

        for attempt in range(last + 1):
            try:
                response = self._http.post(url, data=params)
            except httpx.TransportError as exc:
                if attempt == last:
                    raise SlackError(method, f"network_error:{type(exc).__name__}") from None
                self._sleep(self._backoff(attempt))
                continue

            if response.status_code == 429:
                if attempt == last:
                    raise SlackError(method, "ratelimited")
                try:
                    wait = float(response.headers.get("Retry-After", ""))
                except ValueError:
                    wait = self._backoff(attempt)
                self._sleep(min(max(wait, 0.0), _MAX_RETRY_AFTER_SECONDS))
                continue

            if response.status_code >= 500:
                if attempt == last:
                    raise SlackError(method, f"http_{response.status_code}")
                self._sleep(self._backoff(attempt))
                continue

            try:
                body = response.json()
            except ValueError:
                raise SlackError(method, "invalid_response") from None
            if not isinstance(body, dict) or not body.get("ok"):
                error = body.get("error", "unknown_error") if isinstance(body, dict) else "unknown_error"
                needed = body.get("needed") if isinstance(body, dict) else None
                raise SlackError(method, str(error), needed)
            return body

        raise SlackError(method, "unreachable")  # pragma: no cover

    def paginate(self, method: str, key: str, **params: Any) -> Iterator[dict]:
        """Yield every item under `key`, following `response_metadata.next_cursor`."""
        cursor = ""
        while True:
            body = self.call(method, **params, **({"cursor": cursor} if cursor else {}))
            yield from body.get(key, [])
            cursor = (body.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                return
