"""Literal Slack corpus for the demo, as structured data.

`build_seed.py` turns this into a Slack workspace export on disk (`users.json`,
`channels.json`, `channel_tiers.json`, and one `YYYY-MM-DD.json` per channel per day).

Message texts are written so that `ingest/extract.py`'s regexes fire on the
*conversation unit*, not necessarily on any single message: a bare `#4821` is only
trusted when the surrounding text is code-adjacent, so every thread that references a
PR by number also carries a github.com URL or the literal token "PR" somewhere in the
same unit. That is deliberate, not incidental.

Five of these threads are the demo spine (the 5s -> 7s -> jittered -> bounded-window
retry backoff story, across #4100, #4821, #5012 and #5233). Three are distractors with
overlapping vocabulary but a different subject. `utils/strings.py` is discussed
nowhere at all -- it is the null case the eval suite asserts on.
"""

from __future__ import annotations

# --- workspace directory ------------------------------------------------------

USERS = [
    {"id": "U0JORDAN", "name": "jordan-lee", "display_name": "Jordan Lee"},
    {"id": "U0PRIYA", "name": "priya-raman", "display_name": "Priya Raman"},
    {"id": "U0SAM", "name": "sam-okafor", "display_name": "Sam Okafor"},
    {"id": "U0MIRA", "name": "mira-cheng", "display_name": "Mira Cheng"},
]

# Display names match the git commit author names written by build_seed.py on
# purpose: that is what lets AUTHOR_MATCH_BOOST (9.5) actually fire in the demo.

CHANNELS = [
    {"id": "C0PAYMENTS", "name": "eng-payments", "tier": 1},
    {"id": "C0INCIDENTS", "name": "eng-incidents", "tier": 1},
    {"id": "C0SEARCH", "name": "eng-search", "tier": 2},
    {"id": "C0GENERAL", "name": "eng-general", "tier": 3},
]

# --- conversations ------------------------------------------------------------
# Each unit: channel, date (UTC), threaded (thread vs. top-level burst), and messages
# as (user_id, text, [reaction names]).

THREADS: list[dict] = [
    # === demo spine =========================================================
    {
        "key": "retry-proposal",
        "channel": "eng-payments",
        "date": "2026-01-12",
        "threaded": True,
        "messages": [
            ("U0JORDAN",
             "Webhook deliveries are timing out during failover. I think a 5s backoff "
             "between retries is enough here, let's ship that and see.",
             []),
            ("U0PRIYA",
             "Do we have real numbers on how long a merchant failover actually takes? "
             "5s feels optimistic to me.",
             []),
            ("U0JORDAN",
             "Nothing measured. The ops runbook claims under 4s. I'm putting it behind "
             "`RETRY_BACKOFF_SECONDS` in `webhooks/delivery.py` so it's a one-line "
             "change if we're wrong. Opened "
             "<https://github.com/acme/payments/pull/4100|PR 4100> for it.",
             []),
            ("U0SAM",
             "Fine by me. Ship it and we'll watch the duplicate delivery rate for a "
             "couple of failovers.",
             []),
        ],
    },
    {
        "key": "retry-incident",
        "channel": "eng-incidents",
        "date": "2026-01-28",
        "threaded": True,
        "messages": [
            ("U0PRIYA",
             "5s was still inside the failover window, cut it too close — we saw "
             "duplicate deliveries again during Monday's merchant failover "
             "(WEBHOOK-184). Went with 7s in the end (#4821), should give the failover "
             "enough headroom this time.",
             ["pushpin"]),
            ("U0SAM",
             "Confirmed off the dashboards, the duplicate spike lines up with the "
             "failover window exactly. Every duplicate is a retry that fired before "
             "the merchant finished cutting over.",
             []),
            ("U0JORDAN",
             "That's on me, the 5s in `RETRY_BACKOFF_SECONDS` was a guess and the "
             "runbook number was stale. 7s it is.",
             []),
            ("U0PRIYA",
             "Tracking as ENG-4821, PR is "
             "<https://github.com/acme/payments/pull/4821|#4821>. Changing "
             "`webhooks/delivery.py` only, no schema work.",
             []),
        ],
    },
    {
        "key": "retry-followup",
        "channel": "eng-incidents",
        "date": "2026-04-02",
        "threaded": True,
        "messages": [
            ("U0PRIYA",
             "Following up on WEBHOOK-184 — retry backoff at 7s (#4821) held up "
             "fine through yesterday's failover, no duplicate deliveries. Closing the "
             "loop on ENG-4821.",
             ["white_check_mark"]),
            ("U0SAM",
             "Nothing in the duplicate-delivery dashboard for the last 30 days either. "
             "The 7s change from PR #4821 has been in prod since February.",
             []),
            ("U0MIRA",
             "Marking WEBHOOK-184 resolved then. Worth leaving the comment in "
             "`webhooks/delivery.py` so nobody trims it back to 5s next quarter.",
             []),
        ],
    },
    {
        "key": "retry-jitter",
        "channel": "eng-incidents",
        "date": "2026-03-02",
        "threaded": True,
        "messages": [
            ("U0MIRA",
             "WEBHOOK-201: every queued delivery for a merchant fires at exactly "
             "the same instant once it recovers, because they were all waiting on "
             "the same fixed 7s in `RETRY_BACKOFF_SECONDS`. The burst is enough to "
             "503 the endpoint right after it comes back up.",
             ["warning"]),
            ("U0SAM",
             "Confirmed on the timeline — recovery, then a spike of near-simultaneous "
             "retries, then the second failure a few hundred ms later. Classic "
             "thundering herd.",
             []),
            ("U0MIRA",
             "Adding random jitter on top of the 7s floor in "
             "`webhooks/delivery.py`, `RETRY_JITTER_SECONDS`. Floor stays at 7s so "
             "we don't reopen ENG-4821 — this only ever adds delay, never removes it. "
             "PR is <https://github.com/acme/payments/pull/5012|#5012>.",
             []),
            ("U0PRIYA",
             "Good, as long as the failover-window guarantee from #4821 still holds "
             "at the floor. Tracking as ENG-5012.",
             []),
        ],
    },
    {
        "key": "retry-window-bound",
        "channel": "eng-incidents",
        "date": "2026-03-30",
        "threaded": True,
        "messages": [
            ("U0PRIYA",
             "WEBHOOK-233: during the multi-merchant outage last night the delivery "
             "worker pool saturated completely. Four attempts at 7-9.5s each plus a "
             "10s timeout per attempt means one dead merchant can pin a worker for "
             "close to 40s, and with several merchants down at once the pool never "
             "drained.",
             ["rotating_light"]),
            ("U0JORDAN",
             "So the jitter from #5012 helped the thundering-herd case but made the "
             "worst case slightly worse per-worker.",
             []),
            ("U0PRIYA",
             "Right. Adding `MAX_RETRY_WINDOW_SECONDS` in `webhooks/delivery.py` — "
             "45s ceiling on the whole retry sequence for one delivery, not per "
             "attempt. Once the budget's gone we bail and let the queue pick it up "
             "later instead of holding the worker. PR "
             "<https://github.com/acme/payments/pull/5233|#5233>, tracking as "
             "ENG-5233.",
             []),
            ("U0SAM",
             "Makes sense — the queue already retries independently, so bailing "
             "early doesn't lose the delivery, just moves who's holding it.",
             []),
        ],
    },
    # === settlement story (second non-null eval case) ========================
    {
        "key": "settlement-timeout",
        "channel": "eng-payments",
        "date": "2025-08-14",
        "threaded": True,
        "messages": [
            ("U0MIRA",
             "Large-merchant settlement batches are timing out mid-acknowledgement "
             "again. The acquirer takes over a minute on the big ones and we give up "
             "at 30s.",
             []),
            ("U0PRIYA",
             "That's how we ended up settling twice on resubmit. Raising "
             "`SETTLEMENT_TIMEOUT_SECONDS` in `payments/settlement.py` to 90s — "
             "sized off the slowest acknowledgement we've observed, not the median.",
             []),
            ("U0SAM",
             "90s is fine, the batch worker isn't on a request path. PR #3902 when "
             "you have it.",
             []),
        ],
    },
    # === distractors ==========================================================
    {
        "key": "indexer-retry",
        "channel": "eng-search",
        "date": "2025-11-20",
        "threaded": False,   # top-level burst -- exercises segmentation rule 2
        "messages": [
            ("U0SAM",
             "Indexer keeps dropping documents on the floor when the search cluster "
             "briefly rejects a bulk request.",
             []),
            ("U0MIRA",
             "It only tries once. Adding `INDEX_RETRY_ATTEMPTS` = 3 with jittered "
             "exponential backoff in `search/indexer.py`, then dead-letter whatever is "
             "still failing.",
             []),
            ("U0SAM",
             "Keep the base delay small, this is a batch path and the cluster recovers "
             "in well under a second. Backoff of 0.5s doubling is plenty.",
             []),
            ("U0MIRA",
             "Done, PR #3455. Nothing to do with the webhook retries, different "
             "failure mode entirely.",
             []),
        ],
    },
    {
        "key": "signing-rotation",
        "channel": "eng-payments",
        "date": "2026-01-20",
        "threaded": True,
        "messages": [
            ("U0JORDAN",
             "Merchant reported signature verification failures right after we rotated "
             "their webhook signing secret.",
             []),
            ("U0MIRA",
             "We cut over instantly. Need an overlap where the previous secret is "
             "still honoured — `SECRET_ROTATION_OVERLAP_SECONDS` of 24h in "
             "`webhooks/signing.py`.",
             []),
            ("U0JORDAN",
             "And leave `SIGNATURE_TOLERANCE_SECONDS` at 300, the failures were "
             "rotation, not clock skew. PR #4150.",
             []),
        ],
    },
    {
        "key": "general-chatter",
        "channel": "eng-general",
        "date": "2026-03-05",
        "threaded": False,
        "messages": [
            ("U0SAM", "Reminder that the office is closed Friday.", []),
            ("U0MIRA", "Is the on-call handover still happening Thursday then?", []),
            ("U0JORDAN", "Yes, Thursday 4pm as usual.", []),
        ],
    },
]

# --- append-mode payload (11.7 / 21 step 6) ----------------------------------
# A reply that lands on an *already indexed* thread weeks later. This is the case
# `--mode incremental` has to get right: the whole thread must be rebuilt, not just
# this tail message.
APPEND_MESSAGE = {
    "thread_key": "retry-followup",
    "channel": "eng-incidents",
    "date": "2026-05-18",
    "user": "U0PRIYA",
    "text": (
        "One more failover last night, still clean at 7s. Considering this closed for "
        "good — WEBHOOK-184 and ENG-4821 both stay resolved, PR #4821 stands."
    ),
    "reactions": [],
}
