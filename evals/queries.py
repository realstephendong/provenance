"""Five fixed queries with expected threads.

Trap #6: without this, scoring changes become superstition. Threads are
identified by (channel, date), which is unique in the seed corpus and
survives re-ingest (point ids do not survive a corpus edit).
"""

# (label, file, line_start, line_end, [expected (channel, date)], expect_null)
QUERIES = [
    (
        "demo: why is the retry backoff 7 seconds",
        "webhooks/delivery.py", 23, 68,
        [
            ("eng-incidents", "2026-01-28"),  # names PR #4821
            ("eng-payments", "2026-01-12"),   # intent, before the commit
            ("eng-incidents", "2026-04-02"),  # consequence, after the commit
        ],
        False,
    ),
    (
        "settlement timeout constant",
        "payments/settlement.py", 1, 20,
        [("eng-payments", "2025-08-14")],
        False,
    ),
    (
        "search indexer retry backoff",
        "search/indexer.py", 1, 18,
        [("eng-general", "2026-03-10")],
        False,
    ),
    (
        "webhook signature verification",
        "webhooks/signing.py", 1, 14,
        [("eng-general", "2026-05-20")],
        False,
    ),
    (
        "null case: nothing discusses this",
        "utils/strings.py", 1, 21,
        [],
        True,
    ),
]
