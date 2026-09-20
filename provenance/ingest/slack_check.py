"""Does the shared workspace bot have access to the approved public channels?

    python -m provenance.ingest.slack_check        (or: make slack-check)

Runs four real checks against Slack -- bot token present, token valid and for the
right workspace, channel visible, channel readable -- prints one line per step and a
plain verdict per channel. It joins approved public channels when needed, but never
reads private-channel content in this shared path.

`check_access` is also the gate for `ingest --source slack`, so a person without access
is told so before any OpenAI spend.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass, field

from .. import config
from .slack_client import SlackClient, SlackError

NO_TOKEN_HELP = """\
SLACK_BOT_TOKEN is not set.

  1. Create a Slack app from slack_app_manifest.yml in your workspace
     (https://api.slack.com/apps -> Create New App -> From a manifest).
     It must stay an internal app -- do not distribute it.
  2. Install it to the workspace (an admin may have to approve it).
  3. Copy the "Bot User OAuth Token" (starts with xoxb-) from "OAuth & Permissions".
  4. Put it in .env:   SLACK_BOT_TOKEN=xoxb-...
     Set SLACK_BOT_CHANNEL_IDS to the public channels that may enter the shared index.
  5. Run this check again.

Full steps: "Connecting to Slack" in README.md."""

_BAD_TOKEN = {"invalid_auth", "not_authed", "token_revoked", "token_expired", "account_inactive"}


@dataclass
class ChannelAccess:
    channel_id: str
    name: str = ""
    ok: bool = False
    reason: str = ""      # why not, when ok is False
    fix: str = ""
    # Which data plane this channel belongs to. A private channel is only visible to
    # its members, so indexing it into the shared company index would publish it to
    # everyone who can use Provenance. It goes to the person's own machine instead --
    # see `local_agent/`. Captured here because `conversations.info` already answers
    # it, and this is the only place the whole channel list passes through.
    is_private: bool = False


@dataclass
class AccessReport:
    ok: bool = False
    discovered: bool = False   # the channel list came from `*`, not from .env
    user: str = ""
    team_id: str = ""
    workspace_url: str = ""
    channels: list[ChannelAccess] = field(default_factory=list)
    error: str = ""       # a token/workspace-level failure that stops the check early

    def readable(self, *, private: bool | None = None) -> list[ChannelAccess]:
        """The channels this token can actually read, optionally one plane's worth.

        `private=False` is the shared index's list, `private=True` the local one, and
        `None` everything. Having one place answer this is what stops a private
        channel reaching the company index because some caller forgot to filter.
        """
        return [
            c for c in self.channels
            if c.ok and (private is None or c.is_private == private)
        ]


def _explain(err: SlackError, channel_id: str) -> tuple[str, str]:
    """(reason, fix) in plain words for a Slack error on a channel call."""
    if err.code == "channel_not_found":
        return (
            f"channel {channel_id} is not visible to you",
            "it may be private, or the channel ID is wrong -- invite the bot to a "
            "private channel or check the allowlist",
        )
    if err.code == "not_in_channel":
        return "the bot is not a member of the channel", "allow it to join the public channel or invite it"
    if err.code == "missing_scope":
        needed = err.needed or "the channels:*/groups:* history and read scopes"
        return (
            f"your token is missing a scope ({needed})",
            "add it under 'Bot Token Scopes' (slack_app_manifest.yml lists them) "
            "and reinstall the app, then copy the new bot token",
        )
    if err.code in _BAD_TOKEN:
        return "Slack rejected the bot token", "reinstall the app and update SLACK_BOT_TOKEN"
    if err.code == "access_denied":
        return "Slack denied access to this channel", "ask a workspace admin or channel member"
    return f"Slack returned '{err.code}'", "re-run; if it persists, check the Slack app's settings"


def discover_channels(client: SlackClient, *, private: bool | None = None,
                      say: Callable[[str], None] = print) -> list[str]:
    """Every non-archived channel in the requested visibility plane.

    Listing is not reading: a public channel appears here whether or not you have
    joined it, and a private one only if you are a member. Whether each is actually
    readable is still settled by the per-channel probe below, which is the only
    answer this module ever trusts.
    """
    types = (
        "public_channel" if private is False else
        "private_channel" if private is True else
        "public_channel,private_channel"
    )
    ids = [
        ch["id"]
        for ch in client.paginate("conversations.list", "channels",
                                  types=types,
                                  exclude_archived=True, limit=200)
        if not ch.get("is_archived")
    ]
    plane = "public" if private is False else "private" if private is True else "all"
    say(f"  [ok] discovered {len(ids)} {plane} channels from the wildcard allowlist")
    return ids


def check_access(
    client: SlackClient,
    team_id: str,
    channel_ids: list[str],
    say: Callable[[str], None] = print,
    private: bool | None = None,
    join_public: bool = False,
) -> AccessReport:
    report = AccessReport()

    try:
        auth = client.call("auth.test")
    except SlackError as exc:
        reason, fix = _explain(exc, "")
        report.error = f"{reason} -> {fix}"
        say(f"  [x] token: {reason}")
        return report
    report.user = auth.get("user", "")
    report.team_id = auth.get("team_id", "")
    report.workspace_url = auth.get("url", "")
    say(f"  [ok] token valid: signed in as @{report.user} in '{auth.get('team', '?')}'")

    if team_id and report.team_id != team_id:
        report.error = (
            f"this token is for workspace {report.team_id}, but SLACK_TEAM_ID is {team_id} "
            "-> install the app in the right workspace and use that token"
        )
        say(f"  [x] workspace: {report.error}")
        return report
    say(f"  [ok] workspace: {report.team_id}")

    if "*" in channel_ids:
        report.discovered = True
        try:
            channel_ids = discover_channels(client, private=private, say=say)
        except SlackError as exc:
            reason, fix = _explain(exc, "")
            report.error = f"could not list channels: {reason} -> {fix}"
            say(f"  [x] channels: {report.error}")
            return report

    for cid in channel_ids:
        access = ChannelAccess(channel_id=cid)
        report.channels.append(access)
        try:
            info = client.call("conversations.info", channel=cid)
            channel = info.get("channel", {})
            access.name = channel.get("name", cid)
            access.is_private = bool(channel.get("is_private"))
            say(f"  [ok] channel visible: #{access.name} ({cid})"
                + (" [private]" if access.is_private else ""))
            if private is not None and access.is_private != private:
                access.reason = (
                    "private channels cannot enter the shared index"
                    if access.is_private else "public channels belong to the shared index"
                )
                access.fix = (
                    "remove it from SLACK_BOT_CHANNEL_IDS"
                    if access.is_private else "use the shared bot allowlist"
                )
                say(f"  [ok] skipped #{access.name}: outside this indexing plane")
                continue
            if join_public and not access.is_private and not channel.get("is_member"):
                client.call("conversations.join", channel=cid)
                say(f"  [ok] bot joined public channel: #{access.name}")
            client.call("conversations.history", channel=cid, limit=1)
            say(f"  [ok] channel readable: #{access.name}")
            access.ok = True
        except SlackError as exc:
            access.reason, access.fix = _explain(exc, cid)
            say(f"  [x] {cid}: {access.reason}")

    readable = [c for c in report.channels if c.ok]
    private = [c for c in readable if c.is_private]
    if private:
        say(f"  [ok] {len(private)} private channel(s) -> this machine only, "
            "never the shared index")
    if report.discovered:
        # `*` is "whatever I can read", so a channel the token cannot read is not an
        # error -- it is the answer. An explicitly listed channel is different: a
        # person named it, and silently skipping it would index less than they asked
        # for while reporting success.
        skipped = len(report.channels) - len(readable)
        if skipped:
            say(f"  [ok] skipping {skipped} channel(s) this token cannot read")
        report.ok = bool(readable)
    else:
        report.ok = bool(report.channels) and all(c.ok for c in report.channels)
    return report


def verdict_lines(report: AccessReport) -> list[str]:
    if report.error:
        return [f"NO ACCESS: {report.error}"]
    if not report.channels:
        return ["NO ACCESS: no channels configured -> set SLACK_BOT_CHANNEL_IDS in .env"]
    if report.discovered:
        readable = [c for c in report.channels if c.ok]
        if not readable:
            return ["NO ACCESS: SLACK_BOT_CHANNEL_IDS=* found no public channel the bot can read"]
        return [f"ACCESS OK: {len(readable)} channel(s) — "
                + ", ".join(f"#{c.name}" for c in readable)]
    return [
        f"ACCESS OK: #{c.name}" if c.ok else f"NO ACCESS: #{c.name or c.channel_id}: {c.reason} -> {c.fix}"
        for c in report.channels
    ]


def main() -> None:
    if not config.SLACK_BOT_TOKEN:
        print(NO_TOKEN_HELP)
        sys.exit(1)

    print(f"Checking Slack access (workspace {config.SLACK_TEAM_ID}, "
          f"public channels {', '.join(config.SLACK_BOT_CHANNEL_IDS) or '-'})")
    with SlackClient(config.SLACK_BOT_TOKEN) as client:
        report = check_access(client, config.SLACK_TEAM_ID, config.SLACK_BOT_CHANNEL_IDS,
                              private=False, join_public=True)
    print()
    for line in verdict_lines(report):
        print(line)
    sys.exit(0 if report.ok else 1)


if __name__ == "__main__":
    main()
