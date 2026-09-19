"""Messages -> conversation units.

No topic-shift detector. Four rules, all of them cheap, tuned once against
the seed corpus and then left alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .. import config
from .slack_source import Message


@dataclass
class Unit:
    """One conversation: a Slack thread, or a burst of top-level messages."""

    thread_id: str
    channel_id: str
    channel_name: str
    channel_tier: int
    messages: list[Message] = field(default_factory=list)

    @property
    def ts_start(self) -> float:
        return self.messages[0].ts

    @property
    def ts_end(self) -> float:
        return self.messages[-1].ts

    @property
    def participants(self) -> list[str]:
        seen, out = set(), []
        for m in self.messages:
            if m.user_name not in seen:
                seen.add(m.user_name)
                out.append(m.user_name)
        return out

    @property
    def reactions(self) -> list[str]:
        return sorted({r for m in self.messages for r in m.reactions})

    @property
    def is_bookmarked(self) -> bool:
        return bool(set(self.reactions) & config.TRIGGER_EMOJI)

    @property
    def raw_text(self) -> str:
        return "\n".join(f"{m.user_name}: {m.text}" for m in self.messages)


def _split_oversized(unit: Unit) -> list[Unit]:
    """Rule 4: cap at MAX_MESSAGES_PER_UNIT, splitting at the largest gap."""
    if len(unit.messages) <= config.MAX_MESSAGES_PER_UNIT:
        return [unit]

    msgs = unit.messages
    # Only consider split points that leave both halves non-trivial.
    lo, hi = 1, len(msgs) - 1
    idx = max(range(lo, hi), key=lambda i: msgs[i].ts - msgs[i - 1].ts)
    left = Unit(f"{unit.thread_id}-a", unit.channel_id, unit.channel_name, unit.channel_tier, msgs[:idx])
    right = Unit(f"{unit.thread_id}-b", unit.channel_id, unit.channel_name, unit.channel_tier, msgs[idx:])
    return _split_oversized(left) + _split_oversized(right)


def segment(messages: list[Message]) -> list[Unit]:
    by_channel: dict[str, list[Message]] = {}
    for m in messages:
        by_channel.setdefault(m.channel_name, []).append(m)

    units: list[Unit] = []

    for cname, msgs in by_channel.items():
        msgs = sorted(msgs, key=lambda m: m.ts)
        cid = msgs[0].channel_id
        tier = msgs[0].channel_tier

        # Rule 1: anything with a thread_ts groups by thread_ts, for free.
        threads: dict[float, list[Message]] = {}
        toplevel: list[Message] = []
        for m in msgs:
            if m.thread_ts is not None:
                threads.setdefault(m.thread_ts, []).append(m)
            else:
                toplevel.append(m)

        for tts, group in threads.items():
            group.sort(key=lambda m: m.ts)
            units.append(Unit(f"{tts:.6f}", cid, cname, tier, group))

        # Rule 2: remaining top-level messages split on a gap > threshold.
        run: list[Message] = []
        for m in toplevel:
            if run and m.ts - run[-1].ts > config.SEGMENT_GAP_SECONDS:
                units.append(Unit(f"burst-{run[0].ts:.6f}", cid, cname, tier, run))
                run = []
            run.append(m)
        if run:
            units.append(Unit(f"burst-{run[0].ts:.6f}", cid, cname, tier, run))

    # Rule 3: drop thin units unless a trigger emoji marks them as worth keeping.
    kept = [
        u
        for u in units
        if len(u.messages) >= config.MIN_MESSAGES_PER_UNIT or u.is_bookmarked
    ]

    out: list[Unit] = []
    for u in kept:
        out.extend(_split_oversized(u))
    out.sort(key=lambda u: (u.channel_name, u.ts_start))
    return out
