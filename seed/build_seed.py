#!/usr/bin/env python3
"""Regenerate the seed workspace from scratch, deterministically.

    python seed/build_seed.py

Writes seed/slack (a Slack workspace export) and seed/repo (a git repo whose
history is backdated across 18 months). Both are disposable -- you will reset
and re-ingest many times while tuning, and hand-editing JSON at 4am is where
projects die.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _repo_files as R
import _slack_data as S

SEED = Path(__file__).resolve().parent
SLACK_DIR = SEED / "slack"
REPO_DIR = SEED / "repo"

GITHUB_REPO = "https://github.com/acme/acme-platform"


def iso_to_ts(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


def slack_ts(ts: float, seq: int) -> str:
    """Slack-style timestamp id. Unique per message, stable across runs."""
    return f"{int(ts)}.{seq:06d}"


# --- Slack export ---------------------------------------------------------


def build_slack() -> None:
    if SLACK_DIR.exists():
        shutil.rmtree(SLACK_DIR)
    SLACK_DIR.mkdir(parents=True)

    users = [
        {
            "id": uid,
            "name": key,
            "is_bot": key == "pagerduty",
            "profile": {"display_name": name, "real_name": name},
        }
        for key, (uid, name) in S.USERS.items()
    ]
    (SLACK_DIR / "users.json").write_text(json.dumps(users, indent=2))

    channels = [
        {
            "id": cid,
            "name": name,
            "created": int(iso_to_ts("2025-04-01T00:00:00-04:00")),
            "purpose": {"value": f"#{name}"},
            "members": [u[0] for u in S.USERS.values()],
        }
        for name, (cid, _tier) in S.CHANNELS.items()
    ]
    (SLACK_DIR / "channels.json").write_text(json.dumps(channels, indent=2))

    # Not part of a real export. This stands in for the channel-approval
    # config a deployed Hindsight would own, and drives w_channel.
    tiers = {name: tier for name, (_cid, tier) in S.CHANNELS.items()}
    (SLACK_DIR / "channel_tiers.json").write_text(json.dumps(tiers, indent=2))

    # channel -> date -> [message]
    by_day: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    seq = 0

    for thread in S.THREADS:
        cname = thread["channel"]
        reactions = thread.get("reactions", {})
        t = iso_to_ts(thread["start"])
        parent_ts = None

        for i, (ukey, gap, text) in enumerate(thread["messages"]):
            t += gap
            seq += 1
            uid, _name = S.USERS[ukey]
            ts = slack_ts(t, seq)
            if i == 0:
                parent_ts = ts

            msg = {"type": "message", "user": uid, "text": text, "ts": ts}
            if thread.get("threaded"):
                msg["thread_ts"] = parent_ts
                if i == 0:
                    msg["reply_count"] = len(thread["messages"]) - 1
                else:
                    msg["parent_user_id"] = S.USERS[thread["messages"][0][0]][0]

            if i in reactions:
                counts: dict[str, int] = defaultdict(int)
                for r in reactions[i]:
                    counts[r] += 1
                msg["reactions"] = [
                    {"name": n, "count": c, "users": [S.USERS["mei"][0]]}
                    for n, c in counts.items()
                ]

            day = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d")
            by_day[cname][day].append(msg)

    for cname, days in by_day.items():
        cdir = SLACK_DIR / cname
        cdir.mkdir(parents=True, exist_ok=True)
        for day, msgs in days.items():
            msgs.sort(key=lambda m: float(m["ts"]))
            (cdir / f"{day}.json").write_text(json.dumps(msgs, indent=2))

    total = sum(len(m) for d in by_day.values() for m in d.values())
    print(f"slack export: {len(by_day)} channels, {total} messages -> {SLACK_DIR}")


# --- git repo -------------------------------------------------------------

AUTHORS = {
    "priya": ("Priya Raman", "priya.raman@acme.dev"),
    "dmitri": ("Dmitri Sokolov", "dmitri.sokolov@acme.dev"),
    "rafa": ("Rafael Costa", "rafael.costa@acme.dev"),
    "tobi": ("Tobi Adeyemi", "tobi.adeyemi@acme.dev"),
}

SETTLEMENT_V1 = R.SETTLEMENT.replace("BATCH_SIZE = 500", "BATCH_SIZE = 200")

# (date, author key, subject, {path: content}, branch_merge?)
COMMITS = [
    (
        "2025-04-02T09:41:00-04:00", "priya",
        "Initial platform skeleton (#4102)",
        {
            "README.md": R.README,
            "webhooks/__init__.py": R.INIT,
            "webhooks/signing.py": R.SIGNING,
            "webhooks/delivery.py": R.DELIVERY_V1,
        },
        None,
    ),
    (
        "2025-08-20T15:12:00-04:00", "dmitri",
        "Add settlement batch runner",
        {"payments/__init__.py": R.INIT, "payments/settlement.py": SETTLEMENT_V1},
        # A real merge commit, so SHA->PR strategy 2 has something to resolve.
        ("settlement-batch", 4390),
    ),
    (
        "2025-11-05T11:03:00-05:00", "tobi",
        "Add merchant search index refresh (#4512)",
        {"search/__init__.py": R.INIT, "search/indexer.py": R.INDEXER},
        None,
    ),
    (
        # THE commit. git blame over the retry loop resolves here, which
        # resolves to #4821, which Slack thread B references by URL.
        "2026-02-11T16:28:00-05:00", "priya",
        "Reduce webhook retry backoff to 7s (#4821)",
        {"webhooks/delivery.py": R.DELIVERY_V2},
        None,
    ),
    (
        "2026-07-08T10:15:00-04:00", "rafa",
        "Raise settlement batch size to 500 rows (#5012)",
        {"payments/settlement.py": R.SETTLEMENT},
        None,
    ),
    (
        # Nothing in Slack discusses this. It is the null-handling demo beat.
        "2026-08-26T14:02:00-04:00", "tobi",
        "Add string helpers (#5188)",
        {"utils/__init__.py": R.INIT, "utils/strings.py": R.STRINGS},
        None,
    ),
]


def git(*args: str, env: dict | None = None) -> str:
    full = {**os.environ, **(env or {})}
    out = subprocess.run(
        ["git", "-C", str(REPO_DIR), *args],
        check=True, capture_output=True, text=True, env=full,
    )
    return out.stdout.strip()


def commit_env(iso: str, author_key: str) -> dict:
    name, email = AUTHORS[author_key]
    # Backdate both. Time weighting is invisible if every commit is today.
    return {
        "GIT_AUTHOR_DATE": iso,
        "GIT_COMMITTER_DATE": iso,
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
    }


def write_files(files: dict[str, str]) -> None:
    for rel, content in files.items():
        p = REPO_DIR / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


def build_repo() -> None:
    if REPO_DIR.exists():
        shutil.rmtree(REPO_DIR)
    REPO_DIR.mkdir(parents=True)
    git("init", "-q", "-b", "main")
    git("config", "user.name", "Seed")
    git("config", "user.email", "seed@acme.dev")
    git("config", "commit.gpgsign", "false")
    git("remote", "add", "origin", f"{GITHUB_REPO}.git")

    for iso, author, subject, files, merge in COMMITS:
        env = commit_env(iso, author)
        if merge is None:
            write_files(files)
            git("add", "-A")
            git("commit", "-q", "-m", subject, env=env)
            continue

        branch, pr = merge
        git("checkout", "-q", "-b", branch)
        write_files(files)
        git("add", "-A")
        git("commit", "-q", "-m", subject, env=env)
        git("checkout", "-q", "main")
        git(
            "merge", "--no-ff", "-q", branch,
            "-m", f"Merge pull request #{pr} from acme/{branch}\n\n{subject}",
            env=env,
        )
        git("branch", "-q", "-D", branch)

    log = git("log", "--format=%h %ad %an %s", "--date=short")
    print(f"git repo -> {REPO_DIR}")
    for line in log.splitlines():
        print("  " + line)


def main() -> None:
    build_slack()
    build_repo()
    print("\nseed ready. next: make ingest")


if __name__ == "__main__":
    main()
