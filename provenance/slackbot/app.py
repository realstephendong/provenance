"""The Slack bot: index a conversation into Provenance from inside Slack.

It runs over Socket Mode, so it needs no public URL and no tunnel. Deploy one copy
with the workspace bot token and a shared Elasticsearch; every member can use that
one installation.

Two triggers, one code path:

  * the message shortcut "Index in Provenance" (a message's ... menu). Its payload
    carries `message.thread_ts`, so it knows the conversation exactly -- this is the
    unambiguous one.
  * `/provenance`. Slack does **not** put `thread_ts` in a slash-command payload, so a
    bare command cannot tell which thread you typed it in. Instead it offers the
    channel's recent conversations as buttons. `/provenance <message link>` skips
    the picker.

Slack gives a listener 3 seconds to acknowledge, and indexing costs an LLM summary
plus an embedding -- several seconds. So every listener acks first and reports back
over `response_url` when the work is done.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
import time

from .. import config
from ..ingest import load, slack_live
from ..ingest.slack_client import SlackClient, SlackError
from . import index_thread
from .index_thread import Candidate, IndexedThread, IndexUnavailable, NotFound

log = logging.getLogger("provenance.slackbot")

MISSING_TOKENS_HELP = """\
The workspace bot needs two tokens.

  1. Update your Slack app from slack_app_manifest.yml
     (https://api.slack.com/apps -> your app -> App Manifest), then reinstall it.
     The manifest turns on Socket Mode, a bot user, the /provenance command and the
     "Index in Provenance" message shortcut.
  2. From "OAuth & Permissions", copy the *Bot* User OAuth Token (xoxb-...):
        SLACK_BOT_TOKEN=xoxb-...
  3. From "Basic Information" -> "App-Level Tokens", make a token with the
     `connections:write` scope and copy it (xapp-...):
        SLACK_APP_TOKEN=xapp-...
Full steps: "The Slack bot" in README.md."""


# --- one event loop, for the lifetime of the process --------------------------------

_loop: asyncio.AbstractEventLoop | None = None
_loop_ready = threading.Event()
_loop_lock = threading.Lock()

# Long enough for a summary + an embedding with retries, short enough that a wedged
# OpenAI call gives the listener thread back rather than holding it forever.
WORK_TIMEOUT_SECONDS = 180


def _ensure_loop() -> asyncio.AbstractEventLoop:
    """One long-lived asyncio loop on a background thread.

    `llm._client()` is a process-wide AsyncOpenAI behind an lru_cache, and its httpx
    connection pool binds to whichever loop first uses it. Bolt runs sync listeners on
    a thread pool, so `asyncio.run` per request would hand that one cached client a
    fresh loop every time and fail on the second command with "Event loop is closed".
    """
    global _loop
    with _loop_lock:
        if _loop is None:
            _loop = asyncio.new_event_loop()

            def run() -> None:
                asyncio.set_event_loop(_loop)
                _loop_ready.set()
                _loop.run_forever()

            threading.Thread(target=run, name="provenance-ingest", daemon=True).start()
    _loop_ready.wait(10)
    return _loop


def run_async(coro):
    return asyncio.run_coroutine_threadsafe(coro, _ensure_loop()).result(
        timeout=WORK_TIMEOUT_SECONDS
    )


# --- shared clients ------------------------------------------------------------------

_bot_client: SlackClient | None = None
_es = None


def bot_client() -> SlackClient:
    """The installed workspace bot's client. `httpx.Client` is thread-safe."""
    global _bot_client
    if _bot_client is None:
        _bot_client = SlackClient(config.SLACK_BOT_TOKEN)
    return _bot_client


def es_client():
    global _es
    if _es is None:
        _es = load.client()
    return _es


# --- rendering -------------------------------------------------------------------------

ACTION_INDEX = "provenance_index"      # the picker button; Bolt dispatches on it


def _trim(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _ago(ts: float) -> str:
    seconds = max(0.0, time.time() - ts)
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def _context(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def help_blocks() -> list[dict]:
    return [
        _section(
            "*Provenance* indexes a Slack conversation so it can surface next to the "
            "code it explains."
        ),
        _section(
            "• *Index in Provenance* — in any message's `...` menu. Knows the thread "
            "exactly; this is the one to use.\n"
            f"• `{config.SLACK_BOT_COMMAND}` — pick from this channel's recent "
            "conversations. Slack doesn't tell a slash command which thread you typed "
            "it in, hence the picker.\n"
            f"• `{config.SLACK_BOT_COMMAND} <message link>` — index that conversation "
            "directly."
        ),
        _context(
            ":dart: Name a file path, a PR number or a `backticked` identifier in the "
            "conversation and it becomes an *exact* match for that code, not just a "
            "semantic one."
        ),
    ]


def picker_blocks(candidates: list[Candidate]) -> list[dict]:
    blocks: list[dict] = [_section("*Which conversation should I index?*")]
    for candidate in candidates:
        unit, count = candidate.unit, candidate.message_count
        kind = "thread" if candidate.is_thread else "in channel"
        meta = (f"{', '.join(unit.participants[:3])} · {count} "
                f"message{'s' if count != 1 else ''} {kind} · {_ago(candidate.ts_last)}")
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{meta}*\n{_trim(unit.messages[0].text, 140)}"},
            "accessory": {
                "type": "button",
                "text": {"type": "plain_text", "text": "Index"},
                "action_id": ACTION_INDEX,
                "value": json.dumps({"c": unit.channel_id, "t": unit.thread_id}),
            },
        })
    blocks.append(_context(
        "Not listed? Use *Index in Provenance* in the message's `...` menu, or paste "
        f"its link: `{config.SLACK_BOT_COMMAND} <link>`"
    ))
    return blocks


def result_blocks(indexed: IndexedThread) -> list[dict]:
    count = indexed.message_count
    blocks = [
        _section(
            f":white_check_mark: Indexed *#{indexed.channel_name}* — {count} "
            f"message{'s' if count != 1 else ''} · <{indexed.permalink}|open thread>"
        ),
        _section(f"> {_trim(indexed.summary, 400)}"),
    ]
    if indexed.exact_hooks:
        hooks = " · ".join(f"`{h}`" for h in indexed.exact_hooks[:8])
        blocks.append(_context(f":dart: Exact-match hooks: {hooks}"))
    else:
        blocks.append(_context(
            ":warning: No file path, PR or commit named here, so this can only match "
            "*semantically*. Mention a file (`webhooks/delivery.py`) or a PR (`#4821`) "
            "in the thread and re-index for a guaranteed hit."
        ))
    blocks.extend(_context(f":information_source: {n}") for n in indexed.notes)
    return blocks


# --- listeners ---------------------------------------------------------------------------


def _index_and_report(respond, channel_id: str, target_ts: str) -> None:
    """Shared tail of all three triggers: report progress, do the work, report back."""
    respond(text="Indexing…", blocks=[_context(":hourglass_flowing_sand: Indexing…")],
            replace_original=True)
    try:
        indexed = run_async(
            index_thread.index_at(es_client(), bot_client(), channel_id, target_ts)
        )
    except (NotFound, IndexUnavailable, index_thread.ChannelNotAllowed) as exc:
        respond(text=f":x: {exc}", replace_original=True)
        return
    except SlackError as exc:
        respond(text=f":x: Slack wouldn't let me read that ({exc.code}).",
                replace_original=True)
        return
    except Exception as exc:
        log.exception("indexing failed")
        respond(text=f":x: Indexing failed: `{exc}`. Check the bot's console.",
                replace_original=True)
        return

    log.info("indexed #%s/%s as %s", indexed.channel_name, indexed.thread_id, indexed.doc_id)
    respond(text=f"Indexed #{indexed.channel_name}", blocks=result_blocks(indexed),
            replace_original=True)


def register(app) -> None:
    @app.command(config.SLACK_BOT_COMMAND)
    def handle_command(ack, respond, command) -> None:
        ack()
        text = (command.get("text") or "").strip()

        if text in {"help", "-h", "--help", "?"}:
            respond(text="Provenance help", blocks=help_blocks())
            return

        if text:
            target = index_thread.parse_permalink(text)
            if target is None:
                respond(
                    text=f":x: I don't recognise `{_trim(text, 80)}`. Paste a Slack "
                         f"message link, or run `{config.SLACK_BOT_COMMAND}` with no "
                         f"arguments to pick from recent conversations."
                )
                return
            _index_and_report(respond, *target)
            return

        try:
            index_thread.require_allowed_channel(command["channel_id"])
            candidates = index_thread.recent_candidates(
                bot_client(), command["channel_id"], config.SLACK_BOT_PICKER_LIMIT
            )
        except (NotFound, index_thread.ChannelNotAllowed, SlackError) as exc:
            respond(text=f":x: Couldn't read this channel's history ({exc}).")
            return

        if not candidates:
            respond(text="Nothing recent to index in this channel yet.")
            return
        respond(text="Pick a conversation to index", blocks=picker_blocks(candidates))

    @app.shortcut(config.SLACK_BOT_SHORTCUT)
    def handle_shortcut(ack, respond, shortcut) -> None:
        ack()
        message = shortcut.get("message", {})
        target = message.get("thread_ts") or message.get("ts")
        channel_id = (shortcut.get("channel") or {}).get("id")
        if not target or not channel_id:
            respond(text=":x: That shortcut didn't carry a message I can read.")
            return
        _index_and_report(respond, channel_id, target)

    @app.action(ACTION_INDEX)
    def handle_button(ack, respond, action) -> None:
        ack()
        try:
            chosen = json.loads(action["value"])
        except (KeyError, ValueError):
            respond(text=":x: That button is stale — run the command again.",
                    replace_original=True)
            return
        _index_and_report(respond, chosen["c"], chosen["t"])


# --- boot -------------------------------------------------------------------------------


def _preflight() -> None:
    """Fail before accepting a single command, not on the first one.

    The workspace check is not optional politeness: `Unit.permalink` reads
    `config.SLACK_WORKSPACE`, which defaults to the `acme` placeholder until
    `auth.test` overwrites it. Skip this and every permalink the bot writes 404s.
    """
    if not config.SLACK_BOT_TOKEN or not config.SLACK_APP_TOKEN:
        sys.exit(MISSING_TOKENS_HELP)
    if not config.SLACK_BOT_CHANNEL_IDS:
        sys.exit("SLACK_BOT_CHANNEL_IDS is empty. Set it to the channel IDs the shared bot may index.")
    try:
        config.require_api_key()
    except config.MissingAPIKey as exc:
        sys.exit(str(exc))

    print("checking workspace bot...")
    try:
        auth = bot_client().call("auth.test")
    except SlackError as exc:
        sys.exit(f"Slack rejected SLACK_BOT_TOKEN ({exc.code}). Reinstall the workspace app and update it.")
    team_id = auth.get("team_id", "")
    if config.SLACK_TEAM_ID and team_id != config.SLACK_TEAM_ID:
        sys.exit(
            f"SLACK_BOT_TOKEN is for workspace {team_id}, but SLACK_TEAM_ID is "
            f"{config.SLACK_TEAM_ID}. Install the app in the intended workspace."
        )
    slack_live.apply_workspace(auth.get("url", ""))
    print(f"  bot valid: @{auth.get('user', '?')} in {auth.get('team', team_id)}")
    print(f"  allowed channels: {', '.join(config.SLACK_BOT_CHANNEL_IDS)}")
    print(f"  permalinks -> {config.SLACK_WORKSPACE}.slack.com")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    _preflight()

    try:
        from slack_bolt import App
        from slack_bolt.adapter.socket_mode import SocketModeHandler
    except ImportError:
        sys.exit(
            "slack_bolt is not installed.\n"
            "  make install          # or: .venv/bin/pip install slack_bolt"
        )

    app = App(token=config.SLACK_BOT_TOKEN, raise_error_for_unhandled_request=False)
    register(app)

    print(f"\nProvenance Slack bot ready: {config.SLACK_BOT_COMMAND} "
          f'and the "Index in Provenance" message shortcut.')
    print(f"  writing to {config.ES_URL}/{config.INDEX}   (ctrl-c to stop)\n")
    SocketModeHandler(app, config.SLACK_APP_TOKEN).start()
