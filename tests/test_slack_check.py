from __future__ import annotations

import pytest

from provenance import config
from provenance.ingest import slack_check
from provenance.ingest.slack_check import check_access, verdict_lines

TEAM = "T0C34UQUW68"
CHANNEL = "C0C34DY037B"

AUTH_OK = {"ok": True, "user": "ada", "team": "Acme", "team_id": TEAM,
           "url": "https://acme.slack.com/"}
INFO_OK = {"ok": True, "channel": {"id": CHANNEL, "name": "engineering"}}
HISTORY_OK = {"ok": True, "messages": []}


def _run(make_client, routes):
    client, fake = make_client(routes)
    lines: list[str] = []
    report = check_access(client, TEAM, [CHANNEL], say=lines.append)
    return report, lines, fake


def test_access_ok(make_client):
    report, lines, _ = _run(make_client, {
        "auth.test": AUTH_OK, "conversations.info": INFO_OK, "conversations.history": HISTORY_OK,
    })

    assert report.ok
    assert report.workspace_url == "https://acme.slack.com/"
    assert verdict_lines(report) == ["ACCESS OK: #engineering"]
    assert any("signed in as @ada" in line for line in lines)


def test_invalid_token_stops_before_touching_channels(make_client):
    report, _, fake = _run(make_client, {"auth.test": {"ok": False, "error": "invalid_auth"}})

    assert not report.ok
    assert "Slack rejected your token" in verdict_lines(report)[0]
    assert [m for m, _ in fake.calls] == ["auth.test"]


def test_wrong_workspace(make_client):
    report, _, fake = _run(make_client, {"auth.test": {**AUTH_OK, "team_id": "TOTHER"}})

    assert not report.ok
    assert "TOTHER" in verdict_lines(report)[0] and TEAM in verdict_lines(report)[0]
    assert [m for m, _ in fake.calls] == ["auth.test"]


def test_private_channel_you_are_not_in(make_client):
    report, _, _ = _run(make_client, {
        "auth.test": AUTH_OK, "conversations.info": {"ok": False, "error": "channel_not_found"},
    })

    verdict = verdict_lines(report)[0]
    assert not report.ok
    assert verdict.startswith(f"NO ACCESS: #{CHANNEL}")
    assert "ask a member to add you" in verdict


def test_missing_scope_names_the_scope(make_client):
    report, _, _ = _run(make_client, {
        "auth.test": AUTH_OK, "conversations.info": INFO_OK,
        "conversations.history": {"ok": False, "error": "missing_scope", "needed": "channels:history"},
    })

    verdict = verdict_lines(report)[0]
    assert not report.ok
    assert "channels:history" in verdict and "reinstall" in verdict


def test_visible_but_not_readable_is_no_access(make_client):
    report, _, _ = _run(make_client, {
        "auth.test": AUTH_OK, "conversations.info": INFO_OK,
        "conversations.history": {"ok": False, "error": "not_in_channel"},
    })

    assert not report.ok
    assert "join the channel" in verdict_lines(report)[0]


def test_no_channels_configured_is_no_access(make_client):
    client, _ = make_client({"auth.test": AUTH_OK})

    report = check_access(client, TEAM, [], say=lambda _: None)

    assert not report.ok
    assert "SLACK_CHANNEL_IDS" in verdict_lines(report)[0]


def test_main_without_token_prints_setup_and_exits_1(monkeypatch, capsys):
    monkeypatch.setattr(config, "SLACK_USER_TOKEN", "")

    with pytest.raises(SystemExit) as exc:
        slack_check.main()

    assert exc.value.code == 1
    assert "SLACK_USER_TOKEN is not set" in capsys.readouterr().out
