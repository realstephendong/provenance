"""Reference extraction. Plain regex over thread text, before summarization.

These extractions are what the exact-match tier keys on, so they matter more
than their size suggests: a missed PR number is a missed "Exact match" badge.
"""

from __future__ import annotations

import re

PR_URL = re.compile(r"github\.com/[\w.-]+/[\w.-]+/pull/(\d+)")
PR_HASH = re.compile(r"(?:^|\s)#(\d{1,6})\b")
SHA = re.compile(r"\b([0-9a-f]{7,40})\b")
TICKET = re.compile(r"\b([A-Z]{2,6}-\d{1,5})\b")
PATH = re.compile(r"\b([\w./-]+\.(?:py|rb|ts|tsx|js|go|java|sql))\b")
SYMBOL = re.compile(r"`([A-Za-z_][A-Za-z0-9_]{2,})`")

# PR_HASH alone matches "#4821" but also "#1" in prose and channel names. Only
# trust it when the thread looks like it is talking about code at all.
_CODE_CONTEXT = re.compile(
    r"(github\.com|\.py\b|\.ts\b|\.go\b|\brepo\b|\bPR\b|\bpull request\b|\bmerge[d]?\b|\bcommit\b|\bbranch\b|\bdiff\b)",
    re.IGNORECASE,
)


def _dedupe(items) -> list:
    seen, out = set(), []
    for i in items:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def extract_refs(text: str) -> dict:
    """Pull PR numbers, SHAs, tickets, file paths and symbols out of a thread."""
    pr_refs = [int(m) for m in PR_URL.findall(text)]

    if _CODE_CONTEXT.search(text):
        pr_refs += [int(m) for m in PR_HASH.findall(text)]

    # A 7-40 char hex run also matches long decimals, so drop anything with no
    # letter in it.
    shas = [
        s.lower()[:7]
        for s in SHA.findall(text)
        if not s.isdigit() and any(c.isalpha() for c in s)
    ]

    symbols = SYMBOL.findall(text)
    # Identifiers people write without backticks, which is most of the time in
    # real Slack: SCREAMING_CASE constants and CamelCase types.
    symbols += re.findall(r"\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b", text)
    symbols += re.findall(r"\b([A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+)\b", text)

    return {
        "pr_refs": _dedupe(pr_refs),
        "commit_shas": _dedupe(shas),
        "ticket_refs": _dedupe(TICKET.findall(text)),
        "file_paths": _dedupe(PATH.findall(text)),
        "symbols": _dedupe(symbols),
    }
