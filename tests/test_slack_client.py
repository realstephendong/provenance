from __future__ import annotations

import httpx
import pytest

from provenance import config
from provenance.ingest.slack_client import SlackClient, SlackError

from conftest import TOKEN


def test_429_is_retried_after_retry_after(make_client, sleeps):
    responses = iter([
        httpx.Response(429, headers={"Retry-After": "7"}),
        {"ok": True, "user": "ada"},
    ])
    client, fake = make_client({"auth.test": lambda p: next(responses)})

    assert client.call("auth.test")["user"] == "ada"
    assert sleeps == [7.0]
    assert len(fake.calls) == 2


def test_gives_up_on_persistent_429(make_client, sleeps):
    client, fake = make_client({"auth.test": lambda p: httpx.Response(429, headers={"Retry-After": "1"})})

    with pytest.raises(SlackError) as exc:
        client.call("auth.test")

    assert exc.value.code == "ratelimited"
    assert len(fake.calls) == config.SLACK_MAX_RETRIES + 1


def test_5xx_is_retried_with_backoff(make_client, sleeps):
    responses = iter([httpx.Response(503), httpx.Response(502), {"ok": True}])
    client, _ = make_client({"auth.test": lambda p: next(responses)})

    client.call("auth.test")

    assert sleeps == [config.LLM_RETRY_BASE_SECONDS, config.LLM_RETRY_BASE_SECONDS * 2]


def test_ok_false_raises_with_code_and_needed_scope(make_client):
    client, _ = make_client({
        "conversations.history": {"ok": False, "error": "missing_scope", "needed": "channels:history"},
    })

    with pytest.raises(SlackError) as exc:
        client.call("conversations.history", channel="C1")

    assert exc.value.code == "missing_scope"
    assert exc.value.needed == "channels:history"
    assert "channels:history" in str(exc.value)


def test_pagination_follows_cursor(make_client):
    pages = {
        None: {"ok": True, "messages": [{"ts": "1"}, {"ts": "2"}],
               "response_metadata": {"next_cursor": "abc"}},
        "abc": {"ok": True, "messages": [{"ts": "3"}], "response_metadata": {"next_cursor": ""}},
    }
    client, fake = make_client({"conversations.history": lambda p: pages[p.get("cursor")]})

    got = [m["ts"] for m in client.paginate("conversations.history", "messages", channel="C1")]

    assert got == ["1", "2", "3"]
    assert [c[1].get("cursor") for c in fake.calls] == [None, "abc"]


def test_token_never_appears_in_errors(sleeps):
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect, auth was {TOKEN}")

    client = SlackClient(TOKEN, base_url="https://slack.test/api",
                         transport=httpx.MockTransport(boom), sleep=sleeps.append)

    with pytest.raises(SlackError) as exc:
        client.call("auth.test")

    assert TOKEN not in str(exc.value)
    assert TOKEN not in repr(exc.value)
    assert exc.value.code == "network_error:ConnectError"
    assert exc.value.__cause__ is None      # `from None`: the original message isn't chained
