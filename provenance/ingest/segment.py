"""Messages -> conversation units.

Four fixed rules, deliberately cheap and un-ML (20 lists embedding-based topic-shift
segmentation as a non-goal):

  1. anything carrying `thread_ts` groups by thread;
  2. remaining top-level messages split into bursts on any gap > SEGMENT_GAP_SECONDS;
  3. units under MIN_MESSAGES_PER_UNIT are dropped unless bookmark-reacted;
  4. units over MAX_MESSAGES_PER_UNIT split recursively at their largest internal gap.

`Unit.thread_id` is derived from the unit's first message timestamp, which makes it
stable across runs: a thread that gains a reply keeps the same id, so `load.point_id`
resolves to the same Elasticsearch `_id` and the re-ingest is an overwrite rather
than a duplicate. That property is what 11.7's incremental mode depends on.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .. import config
from .slack_source import Message


@dataclass
class Unit:
    channel_id: str
    channel_name: str
    channel_tier: int
    messages: list[Message] = field(default_factory=list)

    @property
    def thread_id(self) -> str:
        return f"{self.messages[0].ts:.6f}"

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
        seen, out = set(), []
        for m in self.messages:
            for r in m.reactions:
                if r not in seen:
                    seen.add(r)
                    out.append(r)
        return out

    @property
    def is_bookmarked(self) -> bool:
        return bool(set(self.reactions) & config.TRIGGER_EMOJI)

    @property
    def raw_text(self) -> str:
        return "\n".join(f"{m.user_name}: {m.text}" for m in self.messages)

    @property
    def permalink(self) -> str:
        anchor = self.thread_id.replace(".", "")
        return f"https://{config.SLACK_WORKSPACE}.slack.com/archives/{self.channel_id}/p{anchor}"


def _new_unit(messages: list[Message]) -> Unit:
    head = messages[0]
    return Unit(
        channel_id=head.channel_id, channel_name=head.channel_name,
        channel_tier=head.channel_tier, messages=list(messages),
    )


def _split_oversized(unit: Unit) -> list[Unit]:
    """Recursively halve at the widest internal gap until every piece fits."""
    if len(unit.messages) <= config.MAX_MESSAGES_PER_UNIT:
        return [unit]
    messages = unit.messages
    gaps = [(messages[i].ts - messages[i - 1].ts, i) for i in range(1, len(messages))]
    _, cut = max(gaps)
    left, right = _new_unit(messages[:cut]), _new_unit(messages[cut:])
    return _split_oversized(left) + _split_oversized(right)


def segment(messages: list[Message]) -> list[Unit]:
    by_channel: dict[str, list[Message]] = {}
    for m in messages:
        by_channel.setdefault(m.channel_name, []).append(m)

    units: list[Unit] = []
    for channel_messages in by_channel.values():
        ordered = sorted(channel_messages, key=lambda m: m.ts)

        # Rule 1 -- threads.
        threads: dict[float, list[Message]] = {}
        loose: list[Message] = []
        for m in ordered:
            if m.thread_ts is not None:
                threads.setdefault(m.thread_ts, []).append(m)
            else:
                loose.append(m)
        for thread_messages in threads.values():
            units.append(_new_unit(sorted(thread_messages, key=lambda m: m.ts)))

        # Rule 2 -- bursts over what is left.
        burst: list[Message] = []
        for m in loose:
            if burst and (m.ts - burst[-1].ts) > config.SEGMENT_GAP_SECONDS:
                units.append(_new_unit(burst))
                burst = []
            burst.append(m)
        if burst:
            units.append(_new_unit(burst))

    # Rule 4 before rule 3: a split piece can fall under the minimum and should then
    # be dropped by the same rule that drops any other too-small unit.
    split: list[Unit] = []
    for u in units:
        split.extend(_split_oversized(u))

    # Rule 3 -- drop the trivially small, unless someone explicitly flagged it.
    kept = [
        u for u in split
        if len(u.messages) >= config.MIN_MESSAGES_PER_UNIT or u.is_bookmarked
    ]
    kept.sort(key=lambda u: (u.channel_name, u.ts_start))
    return kept


def group_by_channel(messages: list[Message]) -> dict[str, list[Message]]:
    out: dict[str, list[Message]] = {}
    for m in messages:
        out.setdefault(m.channel_name, []).append(m)
    return out
