from __future__ import annotations

from provenance import config
from provenance.ingest.__main__ import incremental_oldest
from provenance.ingest.slack_check import AccessReport, ChannelAccess
from provenance.ingest.slack_live import load_slack
from provenance.ingest.segment import segment

CHANNEL = "C0C34DY037B"
REPORT = AccessReport(
    ok=True, user="ada", team_id="T0C34UQUW68", workspace_url="https://acme.slack.com/",
    channels=[ChannelAccess(channel_id=CHANNEL, name="engineering", ok=True)],
)

PARENT = {"type": "message", "user": "U1", "ts": "1700000000.000100", "thread_ts": "1700000000.000100",
          "reply_count": 2, "text": "retry storm on <@U2> settlement job, see PR #4821",
          "reactions": [{"name": "warning", "count": 1}]}
REPLY_1 = {"type": "message", "user": "U2", "ts": "1700000060.000200", "thread_ts": "1700000000.000100",
           "text": "looking &amp; checking"}
REPLY_2 = {"type": "message", "user": "U1", "ts": "1700000120.000300", "thread_ts": "1700000000.000100",
           "text": "fixed with backoff"}
JOIN = {"type": "message", "subtype": "channel_join", "user": "U3", "ts": "1700000500.000400",
        "text": "<@U3> has joined the channel"}
EMPTY = {"type": "message", "user": "U1", "ts": "1700000600.000500", "text": ""}
LOOSE = {"type": "message", "user": "U2", "ts": "1700009000.000600", "text": "unrelated standalone"}
BOT = {"type": "message", "subtype": "bot_message", "bot_id": "B9", "ts": "1700009100.000700",
       "text": "deploy finished", "bot_profile": {"name": "Deployer"}}


def _users_info(params):
    people = {"U1": "Ada", "U2": "Grace"}
    uid = params["user"]
    if uid not in people:
        return {"ok": False, "error": "user_not_found"}
    return {"ok": True, "user": {"id": uid, "name": uid.lower(), "profile": {"display_name": people[uid]}}}


def _routes(history_pages=None):
    return {
        "conversations.history": history_pages or (lambda p: {
            "ok": True, "messages": [PARENT, JOIN, EMPTY, LOOSE, BOT],
        }),
        "conversations.replies": {"ok": True, "messages": [PARENT, REPLY_1, REPLY_2]},
        "users.info": _users_info,
    }


def test_threads_are_stitched_and_noise_dropped(make_client):
    client, _ = make_client(_routes())

    messages = load_slack(client, REPORT)

    texts = [m.text for m in messages]
    assert len(messages) == 5                              # parent + 2 replies + loose + bot
    assert not any("has joined" in t for t in texts)       # channel_join dropped
    assert "looking & checking" in texts                   # entities cleaned
    assert "retry storm on @Grace settlement job, see PR #4821" in texts   # mention resolved
    thread = [m for m in messages if m.thread_ts == 1700000000.0001]
    assert len(thread) == 3
    assert {m.user_name for m in thread} == {"Ada", "Grace"}
    assert next(m for m in messages if m.text == "deploy finished").user_name == "Deployer"
    assert next(m for m in messages if m.thread_ts == m.ts).reactions == ["warning"]


def test_broadcast_reply_in_history_and_replies_is_not_duplicated(make_client):
    routes = _routes(lambda p: {"ok": True, "messages": [PARENT, REPLY_2]})   # REPLY_2 broadcast
    client, _ = make_client(routes)

    messages = load_slack(client, REPORT)

    assert sorted(m.ts for m in messages) == sorted({m.ts for m in messages})
    assert len(messages) == 3


def test_output_feeds_the_existing_segmenter(make_client):
    client, _ = make_client(_routes())
    messages = load_slack(client, REPORT)

    units = segment(messages)

    thread_unit = next(u for u in units if len(u.messages) == 3)
    assert thread_unit.channel_id == CHANNEL and thread_unit.channel_name == "engineering"
    assert thread_unit.is_bookmarked                      # warning is a trigger emoji
    assert thread_unit.permalink == f"https://acme.slack.com/archives/{CHANNEL}/p1700000000000100"


def test_oldest_bounds_the_history_call(make_client):
    client, fake = make_client(_routes())

    load_slack(client, REPORT, oldest=1699990000.5)
    load_slack(client, REPORT, oldest=0.0)

    history = [p for m, p in fake.calls if m == "conversations.history"]
    assert history[0]["oldest"] == "1699990000.500000"
    assert "oldest" not in history[1]                     # 0 means the entire history


def test_missing_users_read_scope_degrades_to_ids(make_client):
    routes = _routes()
    routes["users.info"] = {"ok": False, "error": "missing_scope", "needed": "users:read"}
    client, _ = make_client(routes)

    messages = load_slack(client, REPORT)

    assert {m.user_name for m in messages if m.user_id == "U1"} == {"U1"}


def test_unknown_user_keeps_id_as_name(make_client):
    routes = _routes()
    routes["conversations.history"] = {
        "ok": True, "messages": [{"type": "message", "user": "U404", "ts": "1700000000.000100", "text": "hi"}],
    }
    client, _ = make_client(routes)

    (message,) = load_slack(client, REPORT)

    assert message.user_name == "U404"


def test_incremental_oldest_covers_gap_and_thread_lookback():
    assert incremental_oldest({}) == 0.0

    state = {"engineering": {"last_ts": 1_800_000_000.0}, "other": {"last_ts": 1_900_000_000.0}}
    margin = config.SEGMENT_GAP_SECONDS + config.SLACK_THREAD_LOOKBACK_DAYS * 86400

    assert incremental_oldest(state) == 1_800_000_000.0 - margin
    assert incremental_oldest({"c": {"last_ts": 10.0}}) == 0.0          # never negative
