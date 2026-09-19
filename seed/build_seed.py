#!/usr/bin/env python3
"""Deterministic demo corpus generator.

Writes two things, both disposable and both gitignored -- delete and regenerate
freely, neither is ever hand-edited:

  seed/repo/    a real git repository with backdated commits
  seed/slack/   a Slack workspace export directory

and rewrites `seed/mock_integrations/github_prs.json` so its `commit_shas` carry the
*actual* short SHAs git produced. The spec's literal fixture lists `8f2a91c` /
`130e156`; git assigns content-addressed SHAs that cannot be forced to arbitrary
values, so the fixture is regenerated here instead. Everything else in the fixture
(PR numbers, titles, authors, dates) is the literal spec content and the PR numbers
are what every join in the pipeline actually keys on.

Usage:
    python seed/build_seed.py                 # build repo + slack + fixtures
    python seed/build_seed.py --append        # append a late reply to an old thread
    python seed/build_seed.py --with-malformed  # also drop in an unreadable day file
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _repo_files as repo_files          # noqa: E402
import _slack_data as slack_data          # noqa: E402

SEED_DIR = Path(__file__).resolve().parent
REPO_DIR = SEED_DIR / "repo"
SLACK_DIR = SEED_DIR / "slack"
FIXTURE_DIR = SEED_DIR / "mock_integrations"

WORKSPACE = "acme"
FIRST_MESSAGE_HOUR = 9          # UTC
MESSAGE_SPACING_SECONDS = 420   # 7 minutes -- well inside SEGMENT_GAP_SECONDS

# --- git repository -----------------------------------------------------------

AUTHORS = {
    "jordan": ("Jordan Lee", "jordan.lee@acme.example"),
    "priya": ("Priya Raman", "priya.raman@acme.example"),
    "mira": ("Mira Cheng", "mira.cheng@acme.example"),
}

# (author key, ISO date, commit subject, {path: content})
# Subjects end in `(#NNNN)` so gitctx.py's squash-merge regex resolves the PR.
COMMITS = [
    ("mira", "2025-06-01T10:00:00+00:00", "Initial commit", repo_files.BASE_FILES),
    ("priya", "2025-08-15T11:20:00+00:00", "Raise settlement batch timeout to 90s (#3902)",
     {"payments/settlement.py": repo_files.PAYMENTS_SETTLEMENT}),
    ("jordan", "2026-01-18T16:00:00+00:00", "Add webhook retry with 5s backoff (#4100)",
     {"webhooks/delivery.py": repo_files.DELIVERY_V1}),
    ("priya", "2026-02-11T14:32:00+00:00", "Fix webhook retry backoff (#4821)",
     {"webhooks/delivery.py": repo_files.DELIVERY_V2}),
]

# Which commit (by index into COMMITS) each PR's merge produced.
PR_TO_COMMIT_INDEX = {4100: 2, 4821: 3}


def _git(*args: str, env: dict | None = None) -> str:
    full_env = {**os.environ, **(env or {})}
    result = subprocess.run(
        ["git", "-C", str(REPO_DIR), *args],
        env=full_env, capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def build_repo() -> dict[int, str]:
    """Create `seed/repo` from scratch. Returns {pr_number: short_sha}."""
    if REPO_DIR.exists():
        shutil.rmtree(REPO_DIR)
    REPO_DIR.mkdir(parents=True)

    _git("init", "-q", "-b", "main")
    _git("config", "user.name", "Provenance Seed")
    _git("config", "user.email", "seed@acme.example")
    _git("config", "commit.gpgsign", "false")

    shas: list[str] = []
    for author_key, iso_date, subject, files in COMMITS:
        for rel_path, content in files.items():
            path = REPO_DIR / rel_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        name, email = AUTHORS[author_key]
        _git("add", "-A")
        _git(
            "commit", "-q", "-m", subject,
            env={
                "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
                "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email,
                "GIT_AUTHOR_DATE": iso_date, "GIT_COMMITTER_DATE": iso_date,
            },
        )
        shas.append(_git("rev-parse", "--short=7", "HEAD"))

    print(f"repo:  {REPO_DIR}")
    for (author_key, iso_date, subject, _), sha in zip(COMMITS, shas):
        print(f"       {sha}  {iso_date[:10]}  {AUTHORS[author_key][0]:<12}  {subject}")
    return {pr: shas[idx] for pr, idx in PR_TO_COMMIT_INDEX.items()}


# --- slack export -------------------------------------------------------------


def _day_base_ts(date_str: str) -> float:
    day = datetime.strptime(date_str, "%Y-%m-%d").replace(
        hour=FIRST_MESSAGE_HOUR, tzinfo=timezone.utc
    )
    return day.timestamp()


def _ts_str(base: float, index: int) -> str:
    """Slack-style timestamp: whole seconds plus a distinct microsecond suffix."""
    return f"{base + index * MESSAGE_SPACING_SECONDS + (index + 1) / 10000.0:.6f}"


def _thread_parent_ts(thread_key: str) -> str:
    """The ts of a thread's first message -- stable across runs, so `--append` can
    attach a reply to a thread built by an earlier invocation."""
    for thread in slack_data.THREADS:
        if thread["key"] == thread_key:
            return _ts_str(_day_base_ts(thread["date"]), 0)
    raise KeyError(f"no thread with key {thread_key!r}")


def _message_json(uid: str, text: str, ts: str, thread_ts: str | None, reactions: list[str]) -> dict:
    msg: dict = {"type": "message", "user": uid, "text": text, "ts": ts}
    if thread_ts:
        msg["thread_ts"] = thread_ts
    if reactions:
        msg["reactions"] = [{"name": r, "count": 1, "users": [uid]} for r in reactions]
    return msg


def build_slack(with_malformed: bool = False) -> None:
    if SLACK_DIR.exists():
        shutil.rmtree(SLACK_DIR)
    SLACK_DIR.mkdir(parents=True)

    (SLACK_DIR / "users.json").write_text(json.dumps([
        {
            "id": u["id"],
            "name": u["name"],
            "profile": {"display_name": u["display_name"], "real_name": u["display_name"]},
        }
        for u in slack_data.USERS
    ], indent=2))

    (SLACK_DIR / "channels.json").write_text(json.dumps(
        [{"id": c["id"], "name": c["name"]} for c in slack_data.CHANNELS], indent=2
    ))
    (SLACK_DIR / "channel_tiers.json").write_text(json.dumps(
        {c["name"]: c["tier"] for c in slack_data.CHANNELS}, indent=2
    ))

    # One file per channel per day; a day can hold more than one conversation.
    by_day: dict[tuple[str, str], list[dict]] = {}
    for thread in slack_data.THREADS:
        base = _day_base_ts(thread["date"])
        parent_ts = _ts_str(base, 0)
        for i, (uid, text, reactions) in enumerate(thread["messages"]):
            by_day.setdefault((thread["channel"], thread["date"]), []).append(
                _message_json(
                    uid, text, _ts_str(base, i),
                    parent_ts if thread["threaded"] else None,
                    reactions,
                )
            )

    for (channel, date), messages in sorted(by_day.items()):
        channel_dir = SLACK_DIR / channel
        channel_dir.mkdir(exist_ok=True)
        messages.sort(key=lambda m: float(m["ts"]))
        (channel_dir / f"{date}.json").write_text(json.dumps(messages, indent=2))

    if with_malformed:
        # Exercises 18 row 25: a day file that cannot be parsed must be skipped
        # with a log line, not abort the ingest.
        broken = SLACK_DIR / "eng-general" / "2026-03-06.json"
        broken.write_text('[{"type": "message", "text": "truncated export')
        print(f"       (wrote a deliberately malformed day file at {broken.name})")

    total = sum(len(m) for m in by_day.values())
    print(f"slack: {SLACK_DIR}  ({len(by_day)} day files, {total} messages)")


def append_late_reply() -> None:
    """Append one reply to an already-built, already-indexed thread.

    This is the input `--mode incremental` is judged on: the reply is newer than the
    checkpoint, but it belongs to a thread whose other messages are not, so a correct
    incremental run has to rebuild the whole thread rather than index this message
    alone. See 11.7.
    """
    payload = slack_data.APPEND_MESSAGE
    if not SLACK_DIR.is_dir():
        sys.exit("no seed/slack yet -- run `python seed/build_seed.py` first")

    parent_ts = _thread_parent_ts(payload["thread_key"])
    day_file = SLACK_DIR / payload["channel"] / f"{payload['date']}.json"
    day_file.parent.mkdir(parents=True, exist_ok=True)

    existing = json.loads(day_file.read_text()) if day_file.exists() else []
    # Idempotent by message text, not by timestamp: the timestamp is derived from the
    # file's current length, so re-running would otherwise keep appending copies.
    if any(m.get("text") == payload["text"] for m in existing):
        print("append: already present, nothing to do")
        return
    ts = _ts_str(_day_base_ts(payload["date"]), len(existing))

    existing.append(_message_json(
        payload["user"], payload["text"], ts, parent_ts, payload["reactions"]
    ))
    day_file.write_text(json.dumps(existing, indent=2))
    print(f"append: +1 message to {day_file.relative_to(SEED_DIR)} "
          f"(reply on thread {payload['thread_key']}, parent ts {parent_ts})")
    print("        now run: make ingest-incremental")


# --- mock integration fixtures -------------------------------------------------


def write_fixtures(pr_shas: dict[int, str]) -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)

    (FIXTURE_DIR / "github_prs.json").write_text(json.dumps([
        {
            "number": 4100,
            "title": "Add webhook retry with 5s backoff",
            "author": "jordan-lee",
            "merged_at": "2026-01-18T16:00:00Z",
            "files": ["webhooks/delivery.py"],
            "commit_shas": [pr_shas[4100]],
        },
        {
            "number": 4821,
            "title": "Fix webhook retry backoff",
            "author": "priya-raman",
            "merged_at": "2026-02-11T14:32:00Z",
            "files": ["webhooks/delivery.py"],
            "commit_shas": [pr_shas[4821]],
        },
    ], indent=2) + "\n")

    (FIXTURE_DIR / "tickets.json").write_text(json.dumps([
        {
            "key": "ENG-4821",
            "title": "Webhook duplicate deliveries during merchant failover",
            "status": "Done",
            "assignee": "Priya Raman",
            "pr_number": 4821,
        },
    ], indent=2) + "\n")

    (FIXTURE_DIR / "sentry_issues.json").write_text(json.dumps([
        {
            "id": "WEBHOOK-184",
            "title": "Duplicate webhook delivery spike during failover window",
            "first_seen": "2026-01-25T09:12:00Z",
            "status": "resolved",
            "pr_number": 4821,
        },
    ], indent=2) + "\n")

    print(f"mocks: {FIXTURE_DIR}  (commit_shas bound to the real SHAs above)")
    print("       note: PR #3902 and #3455 are referenced in Slack but absent from the")
    print("       fixtures on purpose -- that is 18 row 23, adapters return [] and")
    print("       graph.py simply adds no node.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--append", action="store_true",
                        help="append a late reply to an existing thread (incremental test)")
    parser.add_argument("--with-malformed", action="store_true",
                        help="also write an unreadable day file, to exercise ingest resilience")
    args = parser.parse_args()

    if args.append:
        append_late_reply()
        return

    pr_shas = build_repo()
    build_slack(with_malformed=args.with_malformed)
    write_fixtures(pr_shas)
    print("\nseed complete. next: make es && make ingest")


if __name__ == "__main__":
    main()
