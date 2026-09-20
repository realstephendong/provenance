"""Index one Slack conversation on demand -- the write path behind the Slack bot.

`make ingest-slack` reads whole channels in a batch. This indexes a single
conversation the moment someone asks for it, so a discussion that finished two minutes
ago is retrievable before the code it describes has even been written.

It is the same pipeline, not a parallel one: the messages become a `segment.Unit` and
go through `ingest.pipeline.build_and_upsert` exactly as a batch ingest's units do.
Two deliberate divergences, both because an explicit request carries intent a batch
scan cannot infer:

  * rule 3 (drop units under MIN_MESSAGES_PER_UNIT) is lifted -- a two-message
    exchange someone pointed at is worth indexing;
  * the unit is written with `is_bookmarked=True` and a :pushpin: is added to the
    conversation in Slack. The reaction matters beyond the 1.2x retrieval boost:
    without a trigger reaction on the message, the next `make ingest-slack` would
    re-segment the channel, not keep a unit that small, and `--mode reconcile` would
    then delete this document as stale.

Identity is shared with the batch path on purpose. `Unit.thread_id` is the first
message's timestamp and `load.point_id` is a UUID5 of channel_id/thread_id, so the
document written here carries the same _id a later `make ingest-slack` gives the same
conversation: the two overwrite each other, and neither duplicates.

Reads use the installed workspace bot token. To prevent a member from accidentally
putting an arbitrary channel into the shared index, calls are allowed only for
SLACK_BOT_CHANNEL_IDS. Private channels also require inviting the bot.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

from .. import config
from ..ingest import load, pipeline
from ..ingest.segment import Unit, segment
from ..ingest.slack_client import SlackClient, SlackError
from ..ingest.slack_live import resolve_names
from ..ingest.slack_source import Message, to_message

# https://acme.slack.com/archives/C0C34DY037B/p1758304234123456?thread_ts=...
_PERMALINK = re.compile(r"/archives/([A-Z0-9]+)/p(\d{16,})")


class NotFound(RuntimeError):
    """The conversation a request pointed at could not be read."""


class IndexUnavailable(RuntimeError):
    """Elasticsearch is not in a state this can safely write to."""


class ChannelNotAllowed(RuntimeError):
    """The shared bot was invoked outside its configured index scope."""


@dataclass
class IndexedThread:
    """What the bot reports back after a successful write."""

    doc_id: str
    channel_id: str
    channel_name: str
    channel_tier: int
    thread_id: str
    permalink: str
    message_count: int
    participants: list[str] = field(default_factory=list)
    summary: str = ""
    file_paths: list[str] = field(default_factory=list)
    pr_refs: list[int] = field(default_factory=list)
    commit_shas: list[str] = field(default_factory=list)
    ticket_refs: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def exact_hooks(self) -> list[str]:
        """The references `retrieve.exact_tier` can now match this conversation on.

        Empty means the conversation is reachable only through the semantic tier --
        worth saying out loud, because it is the difference between a guaranteed hit
        and a hopeful one.
        """
        return (
            [f"#{n}" for n in self.pr_refs]
            + list(self.commit_shas)
            + list(self.file_paths)
        )


@dataclass
class Candidate:
    """One conversation the picker can offer. See `recent_candidates` for why the
    count and the timestamp are not simply read off the unit."""

    unit: Unit
    message_count: int
    ts_last: float
    is_thread: bool


# --- pointing at a conversation --------------------------------------------------


def parse_permalink(text: str) -> tuple[str, str] | None:
    """A Slack message link -> (channel_id, ts), or None if it isn't one.

    A link to a reply carries `?thread_ts=` for the parent; that wins, so pasting any
    message in a thread indexes the whole thread rather than one reply.
    """
    candidate = text.strip().strip("<>").split("|")[0]
    match = _PERMALINK.search(candidate)
    if not match:
        return None
    channel_id, digits = match.groups()

    thread_ts = parse_qs(urlparse(candidate).query).get("thread_ts", [""])[0]
    if thread_ts:
        return channel_id, thread_ts
    return channel_id, f"{digits[:-6]}.{digits[-6:]}"


def require_allowed_channel(channel_id: str) -> None:
    if "*" not in config.SLACK_BOT_CHANNEL_IDS and channel_id not in config.SLACK_BOT_CHANNEL_IDS:
        raise ChannelNotAllowed(
            "this channel is not enabled for shared Provenance indexing. "
            "Ask an administrator to add it to SLACK_BOT_CHANNEL_IDS."
        )


def ensure_membership(client: SlackClient, channel_id: str) -> None:
    """Join an allowed public channel on first use.

    Slack's `channels:join` scope permits bots to join public channels only. Private
    conversations deliberately remain invite-only: their members decide whether the
    shared index may read them. Calling this before every read is idempotent and avoids
    a deployment-time sweep over every workspace channel.
    """
    try:
        channel = client.call("conversations.info", channel=channel_id).get("channel", {})
    except SlackError as exc:
        if exc.code == "channel_not_found":
            raise NotFound(
                "I can't see this channel. For a private channel, invite the Provenance bot first."
            ) from exc
        raise
    if channel.get("is_member"):
        return
    if channel.get("is_private"):
        raise NotFound("the Provenance bot must be invited to this private channel first")
    try:
        client.call("conversations.join", channel=channel_id)
    except SlackError as exc:
        if exc.code in {"missing_scope", "not_in_channel", "no_permission"}:
            raise NotFound(
                "I couldn't join this public channel. Reinstall the app with `channels:join`."
            ) from exc
        raise


def channel_name(client: SlackClient, channel_id: str) -> str:
    try:
        return client.call("conversations.info", channel=channel_id).get(
            "channel", {}
        ).get("name", channel_id)
    except SlackError as exc:
        raise NotFound(f"can't read that channel ({exc.code})") from exc


def _messages(client: SlackClient, raw: list[dict], channel_id: str, name: str) -> list[Message]:
    names = resolve_names(client, raw)
    tier = config.slack_tier_for(name)
    kept = [
        msg for m in raw
        if (msg := to_message(m, channel_id, name, tier, names)) is not None
    ]
    kept.sort(key=lambda m: m.ts)
    return kept


def _recent_history(client: SlackClient, channel_id: str) -> list[dict]:
    """One page of the channel's newest messages. Deliberately not paginated: this
    only ever answers "what was just being talked about"."""
    body = client.call(
        "conversations.history", channel=channel_id,
        limit=config.SLACK_BOT_HISTORY_MESSAGES,
    )
    return body.get("messages", [])


def recent_candidates(client: SlackClient, channel_id: str, limit: int) -> list[Candidate]:
    """The channel's most recent conversations, most recently active first.

    Both kinds count. A Slack thread is one, and so is a burst of un-threaded
    messages -- `segment` applies the same two rules the batch ingest does, so what
    the picker offers is exactly what would get indexed.

    `conversations.history` returns thread *parents* only, so a thread arrives here as
    a one-message unit no matter how long it ran. `reply_count` and `latest_reply` on
    the parent are what make the picker tell the truth about its size and sort a
    thread someone just replied to above a channel message from an hour ago -- which
    is the whole point, since the conversation you want is the one you just finished.
    """
    ensure_membership(client, channel_id)
    name = channel_name(client, channel_id)
    raw = _recent_history(client, channel_id)
    threads = {
        m["ts"]: (int(m.get("reply_count", 0)), float(m.get("latest_reply") or m["ts"]))
        for m in raw
    }

    messages = _messages(client, raw, channel_id, name)
    out = []
    for unit in segment(messages, min_messages=1):
        replies, latest = threads.get(unit.thread_id, (0, unit.ts_end))
        out.append(Candidate(
            unit=unit,
            message_count=len(unit.messages) + replies,
            ts_last=max(unit.ts_end, latest),
            is_thread=replies > 0,
        ))
    out.sort(key=lambda c: -c.ts_last)
    return out[:limit]


def _holds(unit: Unit, target_ts: str) -> bool:
    return any(f"{m.ts:.6f}" == target_ts for m in unit.messages)


def resolve_units(client: SlackClient, channel_id: str, target_ts: str) -> list[Unit]:
    """Every unit the conversation around `target_ts` produces.

    A threaded message resolves through `conversations.replies`. A message with no
    thread is a loose message, and the unit it belongs to is the *burst* around it --
    so that case re-segments recent history and finds the piece holding it.

    Both paths go through `segment` rather than building a `Unit` by hand, and that is
    the point: rule 4 splits a thread over MAX_MESSAGES_PER_UNIT at its widest internal
    gap. Skipping it here would write one oversized unit whose `thread_id` no longer
    matches any piece a batch ingest builds from the same thread -- so the ids would
    stop agreeing and both copies would survive in the index. Hence a list: an
    over-long thread is several units, and all of them get written.
    """
    name = channel_name(client, channel_id)

    try:
        raw = list(client.paginate(
            "conversations.replies", "messages",
            channel=channel_id, ts=target_ts, limit=config.SLACK_PAGE_SIZE,
        ))
    except SlackError as exc:
        raise NotFound(f"can't read that message ({exc.code})") from exc

    if len(raw) > 1:
        messages = _messages(client, raw, channel_id, name)
        if messages:
            return segment(messages, min_messages=1)

    history = _messages(client, _recent_history(client, channel_id), channel_id, name)
    for unit in segment(history, min_messages=1):
        if _holds(unit, target_ts):
            return [unit]

    # Readable, but nothing indexable came out of it: a bare file upload, a join
    # notice, an emoji-only message. `to_message` drops all three.
    raise NotFound("there's nothing indexable in that message")


# --- writing it ------------------------------------------------------------------


def _ensure_ready(es) -> None:
    """Create the index if this is the first thing to ever write to it, and refuse
    to add a vector to an index built by a different embedder (9.3)."""
    load.ensure_index(es)
    stamped = load.read_embedder_id(es)
    if stamped is None:
        load.stamp_embedder(es)
    elif stamped != config.EMBEDDER_ID:
        raise IndexUnavailable(
            f"the index was built with embedder `{stamped}` but I embed with "
            f"`{config.EMBEDDER_ID}` -- re-run `make ingest-slack` with `--recreate`"
        )


def _pin(client: SlackClient, channel_id: str, ts: str) -> str:
    """Flag the conversation in Slack itself. Returns '' on success, else the reason.

    Uses the workspace bot token, so the flag is consistently attributed to
    Provenance and does not require each requester to grant a user token.
    """
    try:
        client.call(
            "reactions.add", channel=channel_id, timestamp=ts,
            name=config.SLACK_BOT_PIN_EMOJI,
        )
        return ""
    except SlackError as exc:
        if exc.code == "already_reacted":
            return ""
        if exc.code == "missing_scope":
            return (
                "the bot is missing `reactions:write`, so I couldn't flag this "
                "in Slack -- a full re-ingest may drop it if it's a short conversation"
            )
        return f"couldn't add the :{config.SLACK_BOT_PIN_EMOJI}: in Slack ({exc.code})"


def _notes(pin_problem: str) -> list[str]:
    notes = []
    if pin_problem:
        notes.append(pin_problem)
    return notes


async def index_unit(es, client: SlackClient, unit: Unit) -> IndexedThread:
    _ensure_ready(es)

    payloads, written = await pipeline.build_and_upsert(es, [unit], bookmark=True)
    if not written:
        raise IndexUnavailable("Elasticsearch rejected the document")

    # Refresh so the extension can match it on the very next selection. The batch
    # path can wait for the 1s interval; a live demo cannot.
    es.indices.refresh(index=config.INDEX)

    payload = payloads[0]
    pin_problem = _pin(client, unit.channel_id, unit.thread_id)
    return IndexedThread(
        doc_id=load.point_id(unit.channel_id, unit.thread_id),
        channel_id=unit.channel_id,
        channel_name=unit.channel_name,
        channel_tier=unit.channel_tier,
        thread_id=unit.thread_id,
        permalink=unit.permalink,
        message_count=payload["message_count"],
        participants=payload["participants"],
        summary=payload["summary"],
        file_paths=payload["file_paths"],
        pr_refs=payload["pr_refs"],
        commit_shas=payload["commit_shas"],
        ticket_refs=payload["ticket_refs"],
        symbols=payload["symbols"],
        notes=_notes(pin_problem),
    )


async def index_at(es, client: SlackClient, channel_id: str, target_ts: str) -> IndexedThread:
    """Resolve the conversation around one message, index it, and report the piece
    the request actually pointed at."""
    require_allowed_channel(channel_id)
    ensure_membership(client, channel_id)
    units = resolve_units(client, channel_id, target_ts)
    chosen = next((i for i, u in enumerate(units) if _holds(u, target_ts)), 0)

    written = [await index_unit(es, client, u) for u in units]
    if len(written) > 1:
        written[chosen].notes.append(
            f"That thread is longer than {config.MAX_MESSAGES_PER_UNIT} messages, so it "
            f"splits into {len(written)} units — all of them are indexed."
        )
    return written[chosen]
