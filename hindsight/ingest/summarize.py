"""One LLM call per thread.

This is the text that gets dense-embedded, so it matters more than anything
else in the pipeline. The prompt is tuned to describe the subject matter
rather than the conversation: "the retry backoff was lowered to 7 seconds
because..." embeds near code; "the team discussed retry behaviour" does not.
"""

from __future__ import annotations

import re

from .. import config, llm
from .segment import Unit

PROMPT = """You are indexing engineering Slack threads so developers can later find
the discussion that explains a piece of code.

Write 2-3 sentences capturing: what problem was being discussed, what was
decided, and any specific system, service, constant, or file named. Use the
vocabulary of the thread; do not generalize. Write it as a description of
the subject matter, not as a description of the conversation. Never write
"the team discussed" or "this thread is about".

Then a line: SYMBOLS: comma-separated identifiers, error strings, flag
names, or constants mentioned. Empty if none.

Thread:
{thread_text}"""

_SYMBOLS_LINE = re.compile(r"^\s*SYMBOLS\s*:\s*(.*)$", re.IGNORECASE | re.MULTILINE)


def _parse(raw: str) -> tuple[str, list[str]]:
    match = _SYMBOLS_LINE.search(raw)
    symbols: list[str] = []
    if match:
        symbols = [s.strip() for s in match.group(1).split(",") if s.strip()]
        raw = raw[: match.start()]
    return raw.strip(), symbols


async def summarize_units(units: list[Unit]) -> list[tuple[str, list[str]]]:
    """Returns (summary, llm_symbols) per unit, in order."""
    prompts = [PROMPT.format(thread_text=u.raw_text) for u in units]
    raws = await llm.complete_many(
        prompts, config.SUMMARY_MODEL, concurrency=config.SUMMARY_CONCURRENCY, max_tokens=400
    )

    out: list[tuple[str, list[str]]] = []
    for unit, raw in zip(units, raws):
        if raw.startswith("__ERROR__"):
            # Never lose a thread to one bad call: fall back to the raw text
            # truncated, which still embeds and still matches lexically.
            print(f"  ! summary failed for {unit.thread_id}: {raw[10:120]}")
            out.append((unit.raw_text[:600], []))
            continue
        out.append(_parse(raw))
    return out
