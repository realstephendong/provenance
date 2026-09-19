"""The seed Slack workspace.

Built backwards from the demo: a developer selects the retry loop in
webhooks/delivery.py and needs to learn (a) why the backoff is 7 seconds and
(b) why enterprise merchants are special-cased.

The story the corpus tells, and which no code comment states:

  * 30s flat backoff blew merchants' fulfilment SLAs, so payments wanted it
    lower. The contractual ceiling is 40s for all 5 attempts, which caps the
    value at 8s. Merchant load-balancer failover p99 measured 6.2s, so
    anything under ~7s retries into an endpoint still draining connections.
    7 is the only number that satisfies both. -> thread A (intent, before)
  * INC-2291: fast flat retries from every endpoint at once knocked over
    Northwind's ingest queue. Enterprise tier got linear backoff. -> thread B
    (names PR #4821, the commit the demo blames to)
  * Two months later duplicate deliveries showed the value must not be
    raised. -> thread C (consequence, after the commit)

Decoys share vocabulary but explain different code. If they are not tempting,
retrieval has not been tested.
"""

USERS = {
    "priya": ("U01PRIYA", "Priya Raman"),
    "dmitri": ("U02DMITR", "Dmitri Sokolov"),
    "amara": ("U03AMARA", "Amara Osei"),
    "jonas": ("U04JONAS", "Jonas Lindqvist"),
    "mei": ("U05MEITA", "Mei Tanaka"),
    "rafa": ("U06RAFAE", "Rafael Costa"),
    "nadia": ("U07NADIA", "Nadia Haddad"),
    "tobi": ("U08TOBIA", "Tobi Adeyemi"),
    "pagerduty": ("B09PDUTY", "PagerDuty"),
}

# name -> (id, tier). Tier drives w_channel at query time.
CHANNELS = {
    "eng-incidents": ("C01EINCD", 1),
    "eng-payments": ("C02EPAYM", 1),
    "eng-general": ("C03EGENL", 2),
    "random": ("C04RANDM", 3),
}

# Each thread: messages are (user, gap_seconds_since_previous, text).
THREADS = [
    # ---------------------------------------------------------------- A ---
    # Intent. One month before the commit. Names the file. Explains the 7.
    {
        "channel": "eng-payments",
        "start": "2026-01-12T10:02:00-05:00",
        "threaded": True,
        "reactions": {0: ["eyes"], 9: ["fire", "+1"]},
        "messages": [
            ("rafa", 0, "ok so northwind escalated again about webhook latency. their fulfilment bot waits on our delivery and we're taking 2.5 min worst case"),
            ("rafa", 40, "that's 5 attempts x 30s flat in webhooks/delivery.py. RETRY_BACKOFF_SECONDS = 30"),
            ("priya", 310, "30 was picked in 2024 when we were retrying into our own proxy. there's no reason for it now"),
            ("dmitri", 95, "can we just make it exponential? 1, 2, 4, 8, 16"),
            ("priya", 180, "no. the merchant contract promises delivery or final failure within 40s. exponential blows past that on attempt 5"),
            ("priya", 25, "whatever we pick, attempts x backoff <= 40. so max 8s flat"),
            ("dmitri", 210, "ah. didn't know 40 was contractual"),
            ("mei", 4200, "careful going too low. i pulled the failover numbers for the top 20 merchant endpoints last quarter — p99 for their LB to finish draining and accept again is 6.2s"),
            ("mei", 55, "if we retry faster than that we're just hammering a box that's still shutting down. you get a retry storm and no delivery"),
            ("priya", 140, "so the window is 6.2 < x <= 8. 7 it is"),
            ("rafa", 90, "7 feels arbitrary written down but it's the only number that fits both constraints, fine by me"),
            ("priya", 60, "i'll put a PR up this week. flat 7, no exponential, MAX_ATTEMPTS stays 5"),
        ],
    },
    # ---------------------------------------------------------------- B ---
    # The incident, and the PR reference the exact-match tier keys on.
    {
        "channel": "eng-incidents",
        "start": "2026-01-28T02:14:00-05:00",
        "threaded": True,
        "reactions": {0: ["rotating_light"], 11: ["pushpin", "+1", "+1"]},
        "messages": [
            ("pagerduty", 0, "[INC-2291] webhook_delivery_failure_rate > 40% for 10m — paging @priya"),
            ("priya", 420, "looking"),
            ("priya", 380, "our egress had a 90s blip at 01:58. every pending webhook queued up and then all retried at once when it cleared"),
            ("amara", 240, "northwind's ingest is down. they're paging us directly. their queue took ~14k deliveries in under a minute"),
            ("priya", 160, "yeah that's us. flat backoff means every one of those retries lands in the same second. we synchronised the entire fleet"),
            ("dmitri", 300, "this is going to get worse with the change to 7s not better right"),
            ("priya", 220, "for enterprise merchants yes. they're the ones running their own queue in front of the endpoint, and they're the ones who can't absorb a burst"),
            ("mei", 1800, "self-serve merchants are fine, they're behind cloudfront or similar, it soaks the burst"),
            ("priya", 900, "proposal: keep flat 7 for everyone, but enterprise tier gets backoff * attempt. 7, 14, 21, 28, 35. spreads them out and still lands inside 40s total? no — 7+14+21+28+35 = 105"),
            ("priya", 130, "the 40s ceiling is per-attempt-wait in the contract, not cumulative. checked with legal last month. linear is fine"),
            ("amara", 500, "ENG-4412 filed for the tier split"),
            ("priya", 26000, "PR is up: https://github.com/acme/acme-platform/pull/4821 — flat 7s, ENTERPRISE_TIER gets linear. rolling out behind the usual canary"),
            ("rafa", 1200, "reviewed. one nit on the log line, otherwise ship it"),
            ("amara", 900, "INC-2291 resolved, northwind confirmed their queue recovered"),
        ],
    },
    # ---------------------------------------------------------------- C ---
    # Consequence, two months AFTER the commit. The two-sided Gaussian exists
    # to keep this thread reachable; a hard cutoff at commit time drops it.
    {
        "channel": "eng-incidents",
        "start": "2026-04-02T11:30:00-04:00",
        "threaded": True,
        "reactions": {6: ["warning", "pushpin"]},
        "messages": [
            ("jonas", 0, "getting duplicate webhook reports from 3 merchants this week. same event delivered twice, ~7s apart"),
            ("priya", 600, "7s apart is the retry backoff. their endpoint is 200-ing after our client timeout fires, so we count it as transient and retry a delivery that actually succeeded"),
            ("jonas", 240, "so do we bump the backoff to give them longer?"),
            ("priya", 180, "no — and this is worth writing down somewhere"),
            ("priya", 60, "7 is not a tuning knob. it's pinned between the 6.2s merchant failover p99 and the 8s implied by the 40s contractual delivery ceiling. move it either way and something breaks"),
            ("priya", 45, "the fix for duplicates is idempotency keys on the merchant side, which is a separate piece of work"),
            ("priya", 30, "if you ever see RETRY_BACKOFF_SECONDS in a diff, that's a red flag, come find me"),
            ("mei", 2400, "+1. i'll add the failover measurement to the runbook so the next person doesn't have to rediscover it"),
            ("jonas", 3600, "understood. opened ENG-5130 for merchant-side idempotency keys instead"),
        ],
    },
    # ------------------------------------------------------------ decoy 1 --
    # Retry backoff, exponential, wrong system entirely.
    {
        "channel": "eng-general",
        "start": "2026-03-10T14:05:00-04:00",
        "threaded": True,
        "reactions": {},
        "messages": [
            ("tobi", 0, "search reindex is falling over when opensearch does a rolling restart"),
            ("nadia", 300, "what's the retry backoff on the indexer?"),
            ("tobi", 120, "INDEX_RETRY_BASE_SECONDS = 2 with exponential, 6 attempts. so 2,4,8,16,32,64"),
            ("nadia", 200, "that seems fine? a rolling restart is way under 2 minutes"),
            ("tobi", 400, "problem is we give up after 6 and drop the doc. should just keep retrying, the index can be stale for a bit"),
            ("nadia", 180, "yeah exponential + no ceiling on attempts for indexing, it's not user facing. different situation from webhooks where there's an actual sla"),
            ("tobi", 240, "right. i'll bump INDEX_MAX_RETRIES and leave the backoff alone"),
        ],
    },
    # ------------------------------------------------------------ decoy 2 --
    # A different hardcoded timeout constant, same team, adjacent vocabulary.
    {
        "channel": "eng-payments",
        "start": "2025-08-14T09:20:00-04:00",
        "threaded": True,
        "reactions": {},
        "messages": [
            ("dmitri", 0, "SETTLEMENT_TIMEOUT_SECONDS = 45 in payments/settlement.py — anyone remember where 45 came from"),
            ("rafa", 900, "acquirer's documented p99 for a batch response. we added 50% headroom and rounded"),
            ("dmitri", 300, "their p99 is 30?"),
            ("rafa", 120, "was, in 2024. might be worth remeasuring"),
            ("dmitri", 600, "batches are 500 rows now, up from 200. 45 is tight"),
            ("rafa", 240, "if you raise it raise BATCH_SIZE headroom too, the timeout is per batch not per row"),
            ("dmitri", 3000, "filed ENG-3980 to remeasure before touching it"),
        ],
    },
    # ------------------------------------------------------------ decoy 3 --
    # Same file tree (webhooks/), different concern.
    {
        "channel": "eng-general",
        "start": "2026-05-20T16:40:00-04:00",
        "threaded": True,
        "reactions": {},
        "messages": [
            ("nadia", 0, "merchant is saying our webhook signatures don't verify. webhooks/signing.py"),
            ("jonas", 420, "are they signing the timestamp prefix? sign_payload puts ts before the body"),
            ("nadia", 260, "they're hashing the body only"),
            ("jonas", 100, "that's it then. it's in the integration docs but the example snippet is wrong, i'll fix"),
            ("nadia", 800, "confirmed, they got it working. docs snippet was the problem"),
            ("jonas", 200, "PR for the docs: https://github.com/acme/acme-docs/pull/212"),
        ],
    },
    # ------------------------------------------------------------- filler --
    {
        "channel": "eng-general",
        "start": "2026-02-19T11:00:00-05:00",
        "threaded": True,
        "reactions": {},
        "messages": [
            ("mei", 0, "reminder: python 3.12 upgrade lands on main friday, rebase your branches"),
            ("tobi", 1200, "does that break the pinned onnxruntime thing"),
            ("mei", 400, "no, checked, it's fine on 3.12"),
            ("amara", 2000, "thanks for doing this"),
        ],
    },
    {
        "channel": "eng-payments",
        "start": "2026-06-03T13:15:00-04:00",
        "threaded": True,
        "reactions": {},
        "messages": [
            ("rafa", 0, "payout report for may is out, refund rate is down 0.4pp"),
            ("dmitri", 700, "nice. is that the new dispute flow?"),
            ("rafa", 300, "partly. also we stopped double-charging on the retry path, which was mostly northwind"),
            ("amara", 1500, "good"),
        ],
    },
    # -------------------------------------------------------------- noise --
    {
        "channel": "random",
        "start": "2026-03-04T12:30:00-05:00",
        "threaded": True,
        "reactions": {1: ["dog"]},
        "messages": [
            ("tobi", 0, "new coffee machine on 4 is a genuine upgrade"),
            ("nadia", 600, "my dog says hello"),
            ("mei", 900, "the bar is on the floor and the machine cleared it"),
            ("tobi", 200, "it has a timer. you can set a backoff before it grinds. peak engineering"),
            ("jonas", 1800, "lol"),
        ],
    },
    {
        "channel": "random",
        "start": "2026-07-22T17:00:00-04:00",
        "threaded": True,
        "reactions": {},
        "messages": [
            ("amara", 0, "anyone going to the platform conf in october"),
            ("dmitri", 3000, "i have a talk submitted, waiting to hear"),
            ("priya", 1200, "what's the talk"),
            ("dmitri", 800, "settlement batching, mostly the boring parts"),
            ("amara", 400, "the boring parts are the good parts"),
            ("nadia", 5000, "+1"),
        ],
    },
]
