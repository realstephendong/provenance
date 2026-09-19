"""Reads a Slack workspace export directory (the offline/seed path), and holds the
`Message` model and per-message cleaning that the live reader (`slack_live`) shares."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_TIER = 2


@dataclass
class Message:
    channel_id: str
    channel_name: str
    channel_tier: int
    user_id: str
    user_name: str
    text: str
    ts: float
    thread_ts: float | None
    reactions: list[str] = field(default_factory=list)


_MENTION = re.compile(r"<@([UWB][A-Z0-9]+)(?:\|[^>]*)?>")
_LINK = re.compile(r"<(https?://[^|>]+)(?:\|([^>]*))?>")
_CHANNEL_REF = re.compile(r"<#[A-Z0-9]+\|([^>]*)>")


def _clean(text: str, names: dict[str, str]) -> str:
    text = _MENTION.sub(lambda m: "@" + names.get(m.group(1), "someone"), text)
    text = _LINK.sub(lambda m: m.group(1), text)   # keep the URL, drop the label
    text = _CHANNEL_REF.sub(lambda m: "#" + m.group(1), text)
    return text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").strip()


def to_message(
    raw: dict, channel_id: str, channel_name: str, tier: int, names: dict[str, str]
) -> Message | None:
    """One raw Slack message object -> `Message`, or None if it isn't indexable.

    Shared by the export reader and the live API reader (`slack_live`) so both apply
    the same filtering and text cleaning.
    """
    if raw.get("type") != "message" or raw.get("subtype") in {"channel_join", "channel_leave"}:
        return None
    text = _clean(raw.get("text", ""), names)
    if not text:
        return None
    uid = raw.get("user") or raw.get("bot_id") or "unknown"
    return Message(
        channel_id=channel_id, channel_name=channel_name, channel_tier=tier,
        user_id=uid, user_name=names.get(uid, raw.get("username", uid)),
        text=text, ts=float(raw["ts"]),
        thread_ts=float(raw["thread_ts"]) if raw.get("thread_ts") else None,
        reactions=[r["name"] for r in raw.get("reactions", [])],
    )


def load_export(export_dir: str | Path) -> list[Message]:
    root = Path(export_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"no export directory at {root}")

    names: dict[str, str] = {}
    users_file = root / "users.json"
    if users_file.exists():
        for u in json.loads(users_file.read_text()):
            prof = u.get("profile", {})
            names[u["id"]] = prof.get("display_name") or prof.get("real_name") or u.get("name", u["id"])

    tiers = json.loads((root / "channel_tiers.json").read_text()) if (root / "channel_tiers.json").exists() else {}
    ids = (
        {c["name"]: c["id"] for c in json.loads((root / "channels.json").read_text())}
        if (root / "channels.json").exists() else {}
    )

    out: list[Message] = []
    for cdir in sorted(p for p in root.iterdir() if p.is_dir()):
        cname = cdir.name
        cid = ids.get(cname, cname)
        tier = int(tiers.get(cname, DEFAULT_TIER))

        for day_file in sorted(cdir.glob("*.json")):
            try:
                raw_messages = json.loads(day_file.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                # A malformed day-file must not abort the whole export read -- skip
                # it, log it, keep going. Partial data beats a crashed ingest.
                print(f"  ! skipping malformed export file {day_file}: {exc}")
                continue
            for m in raw_messages:
                msg = to_message(m, cid, cname, tier, names)
                if msg is not None:
                    out.append(msg)
    out.sort(key=lambda m: (m.channel_name, m.ts))
    return out
