"""The fixed eval set.

Every tuple is (label, file, line_start, line_end, expected_evidence, expect_null,
expect_conflict), where `expected_evidence` is a list of (channel_name, marker)
pairs; `marker` is matched case-insensitively against the thread's summary and
raw text. It used to be a date, which worked only while the corpus was synthetic
and backdated -- live Slack stamps every message with the day it was posted, so
every thread in a channel would share one date.

These are pinned to the seed story in 10. Any change to a prompt, a weight in
config.py, or the Elasticsearch query shape must be verified against this harness
before being trusted -- it is the only objective signal in the project.
"""

from __future__ import annotations

QUERIES = [
    ("retry backoff - exact PR + conflict + supersede chain",
     "webhooks/delivery.py", 20, 40,
     [("eng-incidents", "five-second retry"),   # the superseded decision
      ("eng-incidents", "seven-second"),        # the evidence that replaced it
      ("eng-payments", "fixed retry spacing")], # the original proposal
     False, True),   # expect a SUPERSEDES/CONFLICTS_WITH edge in the graph
    ("settlement timeout constant", "payments/settlement.py", 1, 20,
     [("eng-payments", "90 seconds")], False, False),
    ("null case: nothing discusses this", "utils/strings.py", 1, 21, [], True, False),
]
