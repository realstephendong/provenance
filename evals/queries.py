"""The fixed eval set.

Every tuple is (label, file, line_start, line_end, expected_evidence, expect_null,
expect_conflict), where `expected_evidence` is a list of (channel_name, date) pairs
that should appear in the results.

These are pinned to the seed story in 10. Any change to a prompt, a weight in
config.py, or the Elasticsearch query shape must be verified against this harness
before being trusted -- it is the only objective signal in the project.
"""

from __future__ import annotations

QUERIES = [
    ("retry backoff - exact PR + conflict + supersede chain",
     "webhooks/delivery.py", 20, 40,
     [("eng-incidents", "2026-01-28"), ("eng-payments", "2026-01-12"), ("eng-incidents", "2026-04-02")],
     False, True),   # expect a SUPERSEDES/CONFLICTS_WITH edge in the graph
    ("settlement timeout constant", "payments/settlement.py", 1, 20,
     [("eng-payments", "2025-08-14")], False, False),
    ("null case: nothing discusses this", "utils/strings.py", 1, 21, [], True, False),
]
