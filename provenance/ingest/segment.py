"""Messages -> conversation units.

Four fixed rules, deliberately cheap and un-ML (20 lists embedding-based topic-shift
segmentation as a non-goal):

  1. anything carrying `thread_ts` groups by thread;
  2. remaining top-level messages split into bursts on any gap > SEGMENT_GAP_SECONDS
     -- but only where SEGMENT_TRUST_TIME says a gap means anything in this
     workspace; where it does not, each loose message stands alone;
  3. units under MIN_MESSAGES_PER_UNIT are dropped unless bookmark-reacted;
  4. units over MAX_MESSAGES_PER_UNIT split into sequential windows.

Rule 2 is the only rule that infers anything from a timestamp, and `gap_report` exists
to tell you whether that inference is safe on your corpus before you trust its output.
Rule 4 used to cut at a unit's widest internal gap and no longer does, for the same
reason -- a window boundary is arbitrary, but it does not pose as evidence.

`Unit.thread_id` is derived from the unit's first message timestamp, which makes it
stable across runs: a thread that gains a reply keeps the same id, so `load.point_id`
resolves to the same Elasticsearch `_id` and the re-ingest is an overwrite rather
than a duplicate. That property is what 11.7's incremental mode depends on.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .. import config
from .extract import extract_refs
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
    """Split into sequential windows of MAX_MESSAGES_PER_UNIT, in order.

    This used to cut at the unit's widest internal gap, which made the boundary a
    function of timestamps. In a channel seeded or bulk-posted in one sitting that is
    a few seconds of noise deciding where a conversation ends -- the same reason rule
    2 now asks SEGMENT_TRUST_TIME before believing a gap. A window boundary is
    arbitrary too, but it is arbitrary in a way that does not pretend to be evidence,
    and it is identical for every caller segmenting the same messages.

    Not claimed: better id stability. Measured over a 130-message thread, both cuts
    are perfectly stable when the thread gains replies (the everyday case), and both
    re-cut the pieces after a deleted message -- which is what `--mode reconcile`
    exists to clean up.
    """
    size = config.MAX_MESSAGES_PER_UNIT
    if len(unit.messages) <= size:
        return [unit]
    return [_new_unit(unit.messages[i:i + size]) for i in range(0, len(unit.messages), size)]


def _carries_signal(unit: Unit) -> bool:
    """Is a single loose message worth indexing on its own?

    Only consulted where SEGMENT_TRUST_TIME is false and every loose message is
    therefore its own unit -- the MIN_MESSAGES_PER_UNIT floor would otherwise drop
    all of them, which is a worse answer than the blended unit it replaced. "we
    capped retries at 7s, see thread" is exactly the artifact this product exists to
    find; "lol" and "+1" are not.
    """
    text = "\n".join(m.text for m in unit.messages)
    if len(text.split()) >= config.SEGMENT_SOLO_MIN_WORDS or "http" in text:
        return True
    refs = extract_refs(text)
    return any(refs[k] for k in ("pr_refs", "commit_shas", "ticket_refs", "file_paths", "symbols"))


def segment(messages: list[Message], *, min_messages: int | None = None) -> list[Unit]:
    """`min_messages` overrides rule 3's floor. The Slack bot passes 1: it segments a
    channel only to show someone which conversations exist and to find the one they
    picked, and a unit too small to index on a batch scan is still a unit they can
    point at. Nothing else overrides it -- batch ingest keeps the configured floor.

    Rule 2 reads `config.SEGMENT_TRUST_TIME` rather than taking a parameter, on
    purpose: every caller must segment the same messages the same way or their unit
    boundaries, and so their document ids, stop agreeing. See the flag's note in
    config.py."""
    floor = config.MIN_MESSAGES_PER_UNIT if min_messages is None else min_messages
    trust_time = config.SEGMENT_TRUST_TIME

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

        # Rule 2 -- bursts over what is left, where a gap means anything at all.
        if not trust_time:
            units.extend(_new_unit([m]) for m in loose)
        else:
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

    # Rule 3 -- drop the trivially small, unless someone explicitly flagged it, or
    # it is a solo loose message in a channel whose gaps carry no boundary signal and
    # it says something (`_carries_signal`).
    kept = [
        u for u in split
        if len(u.messages) >= floor
        or u.is_bookmarked
        or (not trust_time and len(u.messages) == 1 and _carries_signal(u))
    ]
    kept.sort(key=lambda u: (u.channel_name, u.ts_start))
    return kept


def group_by_channel(messages: list[Message]) -> dict[str, list[Message]]:
    out: dict[str, list[Message]] = {}
    for m in messages:
        out.setdefault(m.channel_name, []).append(m)
    return out


# --- is a gap evidence of anything here? -----------------------------------------


@dataclass
class ChannelGaps:
    """What the intervals between one channel's loose messages look like."""
    channel_name: str
    loose_messages: int
    p50_gap: float
    p90_gap: float
    max_gap: float
    degenerate: bool      # the gaps carry no boundary signal; rule 2 cannot work here


def _percentile(ordered: list[float], fraction: float) -> float:
    """Nearest-rank percentile over an already-sorted list."""
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, round(fraction * len(ordered)) - 1))
    return ordered[index]


def gap_report(messages: list[Message]) -> list[ChannelGaps]:
    """Per channel: how far apart its loose messages actually arrive.

    Rule 2 assumes a large gap means a conversation ended. In a channel written in
    real time that holds. In one seeded or bulk-posted by a script it does not -- every
    gap is seconds, nothing clears SEGMENT_GAP_SECONDS, and the whole channel collapses
    into one blended unit with one summary and one vector. That failure is silent,
    which is the only reason this function exists: ingest prints the evidence so the
    verdict is a person's rather than a threshold's.
    """
    out: list[ChannelGaps] = []
    for cname, channel_messages in sorted(group_by_channel(messages).items()):
        loose = sorted(
            (m for m in channel_messages if m.thread_ts is None), key=lambda m: m.ts
        )
        gaps = sorted(loose[i].ts - loose[i - 1].ts for i in range(1, len(loose)))
        p90 = _percentile(gaps, 0.9)
        out.append(ChannelGaps(
            channel_name=cname,
            loose_messages=len(loose),
            p50_gap=_percentile(gaps, 0.5),
            p90_gap=p90,
            max_gap=gaps[-1] if gaps else 0.0,
            # Below the floor of what could distinguish two conversations, and with
            # enough messages for the answer to mean something. A handful of loose
            # messages in a quiet channel is not evidence of anything either way.
            degenerate=(
                len(loose) > config.MIN_MESSAGES_PER_UNIT
                and p90 < config.SEGMENT_DEGENERATE_P90_SECONDS
            ),
        ))
    return out


def _duration(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def gap_report_lines(report: list[ChannelGaps]) -> list[str]:
    """The report as printable lines, plus a verdict when time is being trusted and
    should not be. Empty when there is nothing to say."""
    degenerate = [c for c in report if c.degenerate]
    if not degenerate:
        return []

    lines = ["  ! loose messages arrive too close together for gaps to mean anything:"]
    for c in degenerate:
        lines.append(
            f"      #{c.channel_name}: {c.loose_messages} loose messages, "
            f"median gap {_duration(c.p50_gap)}, p90 {_duration(c.p90_gap)}, "
            f"widest {_duration(c.max_gap)}"
        )
    if config.SEGMENT_TRUST_TIME:
        lines += [
            f"    rule 2 splits on gaps over {_duration(config.SEGMENT_GAP_SECONDS)}, so every"
            " loose message above lands in one",
            "    blended unit -- one summary, one vector, one permalink for unrelated"
            " conversations.",
            "    Fix it at the source by posting those conversations as Slack threads,"
            " or set",
            "    SEGMENT_TRUST_TIME=false in .env to index each loose message on its own"
            " instead.",
        ]
    else:
        lines.append(
            "    SEGMENT_TRUST_TIME=false, so these are indexed one unit per message"
            " rather than merged."
        )
    return lines
