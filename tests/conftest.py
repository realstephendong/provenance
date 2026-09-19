"""Shared fakes for the Slack tests. Nothing here touches the network."""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from provenance.ingest.slack_client import SlackClient  # noqa: E402

TOKEN = "xoxp-SECRET-TOKEN-VALUE"


class FakeSlack:
    """A scripted Slack. `routes` maps method -> a body dict, or a callable(params) ->
    a body dict or an `httpx.Response`. Every call is recorded in `calls`."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        params = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.calls.append((method, params))
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        route = self.routes[method]
        result = route(params) if callable(route) else route
        return result if isinstance(result, httpx.Response) else httpx.Response(200, json=result)


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def make_client(sleeps):
    def _make(routes: dict) -> tuple[SlackClient, FakeSlack]:
        fake = FakeSlack(routes)
        client = SlackClient(
            TOKEN, base_url="https://slack.test/api",
            transport=httpx.MockTransport(fake), sleep=sleeps.append,
        )
        return client, fake
    return _make
