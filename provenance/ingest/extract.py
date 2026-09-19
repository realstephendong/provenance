"""Structured reference extraction: the lexical half of the join.

Everything here is deliberately conservative. A bare `#4821` is only trusted when the
surrounding text is code-adjacent, because `#4821` in prose is as likely to be a
ticket in someone else's tracker, a dollar figure, or a room number.
"""

from __future__ import annotations

import re

PR_URL = re.compile(r"github\.com/[\w.-]+/[\w.-]+/pull/(\d+)")
PR_HASH = re.compile(r"(?:^|\s)#(\d{1,6})\b")
SHA = re.compile(r"\b([0-9a-f]{7,40})\b")
TICKET = re.compile(r"\b([A-Z]{2,6}-\d{1,5})\b")
PATH = re.compile(r"\b([\w./-]+\.(?:py|rb|ts|tsx|js|go|java|sql))\b")
SYMBOL = re.compile(r"`([A-Za-z_][A-Za-z0-9_]{2,})`")
_CODE_CONTEXT = re.compile(
    r"(github\.com|\.py\b|\.ts\b|\.go\b|\brepo\b|\bPR\b|\bpull request\b|\bmerge[d]?\b|\bcommit\b|\bbranch\b|\bdiff\b)",
    re.IGNORECASE,
)


def _dedupe(items):
    seen, out = set(), []
    for i in items:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def extract_refs(text: str) -> dict:
    pr_refs = [int(m) for m in PR_URL.findall(text)]
    if _CODE_CONTEXT.search(text):
        pr_refs += [int(m) for m in PR_HASH.findall(text)]   # bare "#4821" only trusted in code-adjacent context
    shas = [s.lower()[:7] for s in SHA.findall(text) if not s.isdigit() and any(c.isalpha() for c in s)]
    symbols = SYMBOL.findall(text)
    symbols += re.findall(r"\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b", text)      # SCREAMING_CASE without backticks
    symbols += re.findall(r"\b([A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+)\b", text)  # CamelCase without backticks
    return {
        "pr_refs": _dedupe(pr_refs), "commit_shas": _dedupe(shas),
        "ticket_refs": _dedupe(TICKET.findall(text)), "file_paths": _dedupe(PATH.findall(text)),
        "symbols": _dedupe(symbols),
    }
