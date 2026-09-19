"""One LLM call per conversation unit: thread -> retrievable engineering prose.

This is the ingest-side half of 2.2 -- code is never embedded against Slack prose
directly, so both sides are rewritten into the same register first. The query side
does the mirror-image rewrite in service/query_build.py.
"""

from __future__ import annotations

import re

from .. import config, llm
from .segment import Unit

PROMPT = """You are indexing engineering Slack threads so developers can later find
the discussion that explains a piece of code.

Write 2-3 sentences capturing: what problem was being discussed, what was decided, and
any specific system, service, constant, or file named. Use the vocabulary of the
thread; do not generalize. Write it as a description of the subject matter, not as a
description of the conversation. Never write "the team discussed" or "this thread is
about".

Then a line: SYMBOLS: comma-separated identifiers, error strings, flag names, or
constants mentioned. Empty if none.

Thread:
{thread_text}"""

_SYMBOLS_LINE = re.compile(r"^\s*SYMBOLS\s*:\s*(.*)$", re.IGNORECASE | re.MULTILINE)
_NO_SYMBOLS = {"none", "n/a", "na", "empty", "-", "(none)", "none."}
FALLBACK_CHARS = 600


def _parse(raw: str, unit: Unit) -> tuple[str, list[str]]:
    """Split the trailing `SYMBOLS:` line off the summary body.

    A failed call arrives as llm.complete_many's `__ERROR__` sentinel. The thread is
    never dropped for it (18 row 6) -- it just gets a worse summary.
    """
    if not raw or raw.startswith("__ERROR__"):
        return unit.raw_text[:FALLBACK_CHARS], []

    symbols: list[str] = []
    match = _SYMBOLS_LINE.search(raw)
    if match:
        # The prompt says "Empty if none", and the model answers that literally often
        # enough to matter: 11 of 38 documents in one live corpus carried `none` as a
        # symbol. `symbols` is boosted in the lexical channel and is now compared as
        # an identity by the exact tier, so a junk token there is not cosmetic.
        symbols = [
            s.strip() for s in match.group(1).split(",")
            if s.strip() and s.strip().lower() not in _NO_SYMBOLS
        ]
        raw = raw[:match.start()]
    summary = raw.strip()
    return (summary or unit.raw_text[:FALLBACK_CHARS]), symbols


async def summarize_units(units: list[Unit]) -> list[tuple[str, list[str]]]:
    """Returns one (summary, symbols) pair per unit, positionally aligned."""
    if not units:
        return []
    prompts = [PROMPT.format(thread_text=u.raw_text) for u in units]
    raw = await llm.complete_many(prompts, config.SUMMARY_MODEL, max_tokens=400)

    failures = sum(1 for r in raw if r.startswith("__ERROR__"))
    if failures:
        print(f"  ! {failures}/{len(units)} summaries failed, using truncated raw text for those")
    return [_parse(r, u) for r, u in zip(raw, units)]
