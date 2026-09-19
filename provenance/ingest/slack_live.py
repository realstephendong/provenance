"""Live Slack reader: the API counterpart of `slack_source.load_export`.

Returns the same `Message` objects, so segment -> extract -> summarize -> embed -> load
are unchanged. The per-message filtering and text cleaning is `slack_source.to_message`,
shared with the export reader, so both paths index identical text.

Threads: `conversations.history` returns only top-level messages, so every message with
replies is followed up with `conversations.replies`. A broadcast reply shows up in both,
hence the de-duplication by timestamp.
"""

from __future__ import annotations

from urllib.parse import urlparse

from .. import config
from .slack_check import AccessReport
from .slack_client import SlackClient, SlackError
from .slack_source import _MENTION, Message, to_message

_HUMAN_ID_PREFIXES = ("U", "W")      # bot ids (B...) are not resolvable with users.info


def _apply_workspace(workspace_url: str) -> None:
    """Point permalinks at the real workspace (`Unit.permalink` reads config)."""
    host = urlparse(workspace_url).hostname or ""
    if host.endswith(".slack.com"):
        config.SLACK_WORKSPACE = host[: -len(".slack.com")]


def _fetch_channel(client: SlackClient, channel_id: str, oldest: float) -> list[dict]:
    """Every raw message in the channel newer than `oldest`, with full threads."""
    params: dict = {"channel": channel_id, "limit": config.SLACK_PAGE_SIZE}
    if oldest > 0:
        params["oldest"] = f"{oldest:.6f}"

    by_ts: dict[str, dict] = {}
    parents: list[str] = []
    for m in client.paginate("conversations.history", "messages", **params):
        by_ts.setdefault(m["ts"], m)
        if m.get("reply_count", 0) > 0 and m.get("thread_ts", m["ts"]) == m["ts"]:
            parents.append(m["ts"])

    for i, thread_ts in enumerate(parents, 1):
        for r in client.paginate(
            "conversations.replies", "messages",
            channel=channel_id, ts=thread_ts, limit=config.SLACK_PAGE_SIZE,
        ):
            by_ts.setdefault(r["ts"], r)
        if i % 50 == 0:
            print(f"    ...{i}/{len(parents)} threads")
    return list(by_ts.values())


def _resolve_names(client: SlackClient, raw_messages: list[dict]) -> dict[str, str]:
    """user/bot id -> display name, using users.info once per distinct human id."""
    names: dict[str, str] = {}
    wanted: set[str] = set()
    for m in raw_messages:
        if m.get("bot_id") and (m.get("bot_profile") or {}).get("name"):
            names[m["bot_id"]] = m["bot_profile"]["name"]
        if m.get("user"):
            wanted.add(m["user"])
        wanted.update(_MENTION.findall(m.get("text", "")))

    for uid in sorted(u for u in wanted if u.startswith(_HUMAN_ID_PREFIXES)):
        try:
            user = client.call("users.info", user=uid).get("user", {})
        except SlackError as exc:
            if exc.code == "missing_scope":
                print("  ! token lacks users:read -- showing user ids instead of names")
                break
            continue          # user_not_found etc.: keep the id as the name
        prof = user.get("profile", {})
        names[uid] = prof.get("display_name") or prof.get("real_name") or user.get("name", uid)
    return names


def load_slack(client: SlackClient, report: AccessReport, oldest: float = 0.0) -> list[Message]:
    """Read every channel `report` says is accessible. `oldest=0` is the entire history."""
    _apply_workspace(report.workspace_url)

    out: list[Message] = []
    for channel in report.channels:
        raw = _fetch_channel(client, channel.channel_id, oldest)
        names = _resolve_names(client, raw)
        kept = [
            msg for m in raw
            if (msg := to_message(m, channel.channel_id, channel.name,
                                  config.slack_tier_for(channel.name), names)) is not None
        ]
        print(f"  #{channel.name}: {len(raw)} messages fetched, {len(kept)} indexable")
        out.extend(kept)

    out.sort(key=lambda m: (m.channel_name, m.ts))
    return out
