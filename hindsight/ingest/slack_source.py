"""Read a Slack workspace export.

The export is the primary path and the demo path. An API reader is a
deliberate non-goal until everything else works, and if someone does build
one: Slack cut conversations.history and conversations.replies to Tier 1 for
non-Marketplace apps -- 1 request/minute, 15 objects/response. Internal
non-distributed apps are supposed to keep the old limits, but that must be
verified with one real call before any code is written against it. The export
path carries no such risk.
"""

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
    """Slack markup -> plain text, so regexes and the LLM see real words."""
    text = _MENTION.sub(lambda m: "@" + names.get(m.group(1), "someone"), text)
    # Keep the URL, not the label: PR_URL extraction runs on this.
    text = _LINK.sub(lambda m: m.group(1), text)
    text = _CHANNEL_REF.sub(lambda m: "#" + m.group(1), text)
    return text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").strip()


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

    tiers: dict[str, int] = {}
    tiers_file = root / "channel_tiers.json"
    if tiers_file.exists():
        tiers = json.loads(tiers_file.read_text())

    ids: dict[str, str] = {}
    channels_file = root / "channels.json"
    if channels_file.exists():
        for c in json.loads(channels_file.read_text()):
            ids[c["name"]] = c["id"]

    out: list[Message] = []
    for cdir in sorted(p for p in root.iterdir() if p.is_dir()):
        cname = cdir.name
        cid = ids.get(cname, cname)
        tier = int(tiers.get(cname, DEFAULT_TIER))

        for day_file in sorted(cdir.glob("*.json")):
            for m in json.loads(day_file.read_text()):
                if m.get("type") != "message" or m.get("subtype") in {"channel_join", "channel_leave"}:
                    continue
                text = _clean(m.get("text", ""), names)
                if not text:
                    continue
                uid = m.get("user") or m.get("bot_id") or "unknown"
                out.append(
                    Message(
                        channel_id=cid,
                        channel_name=cname,
                        channel_tier=tier,
                        user_id=uid,
                        user_name=names.get(uid, m.get("username", uid)),
                        text=text,
                        ts=float(m["ts"]),
                        thread_ts=float(m["thread_ts"]) if m.get("thread_ts") else None,
                        reactions=[r["name"] for r in m.get("reactions", [])],
                    )
                )

    out.sort(key=lambda m: (m.channel_name, m.ts))
    return out
