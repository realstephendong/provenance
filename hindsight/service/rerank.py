"""LLM rerank, then synthesis.

The `why` line is a large share of the perceived intelligence of the tool for
a very small share of the work: it appears verbatim on the result card and is
what makes a hit look understood rather than merely retrieved.
"""

from __future__ import annotations

from .. import config, llm

RERANK_PROMPT = """A developer selected this code and wants the discussions that explain why it
is the way it is.

CODE DESCRIPTION:
{description}

FILE: {file_path}
KEY IDENTIFIERS: {symbols}

CANDIDATE DISCUSSIONS:
{candidates}

For each candidate decide whether it genuinely explains this specific code --
its constants, its special cases, its failure handling. A discussion about
similar-sounding behaviour in a different system is NOT relevant.

Return JSON: {{"items": [{{"id": "...", "relevant": true, "why": "one line, max 15 words, why this explains the code"}}]}}

Order by how well each explains the code. Include every candidate, with
relevant set to false for the ones that do not."""

SYNTHESIS_PROMPT = """A developer selected this code and asked why it is the way it is.

CODE DESCRIPTION:
{description}

DISCUSSIONS, in the order they will be shown, numbered for citation:
{threads}

Write 2-3 sentences answering why the code is the way it is. Cite with [1],
[2] matching the numbers above. Name the specific reasons and numbers the
discussions give.

If the discussions do not actually explain this code, say so plainly in one
sentence instead of inventing a connection."""


def _candidate_block(hits: list[dict]) -> str:
    rows = []
    for h in hits:
        p = h["payload"]
        rows.append(
            f'- id: "{h["id"]}"\n'
            f'  channel: #{p.get("channel_name")}\n'
            f'  date: {p.get("date_str", "")}\n'
            f'  summary: {p.get("summary", "")[:600]}'
        )
    return "\n".join(rows)


async def rerank(hits: list[dict], *, description: str, file_path: str, symbols: list[str]) -> list[dict]:
    """Keep the relevant hits, in model order, each carrying its `why`."""
    if not hits:
        return []

    prompt = RERANK_PROMPT.format(
        description=description,
        file_path=file_path,
        symbols=", ".join(symbols[:20]),
        candidates=_candidate_block(hits[: config.RERANK_INPUT]),
    )

    try:
        data = await llm.complete_json(prompt, config.RERANK_MODEL)
    except Exception as exc:
        # Never lose the whole answer to a rerank failure: fall back to score
        # order with no `why` lines.
        print(f"  ! rerank failed ({exc}); falling back to score order")
        for h in hits:
            h["why"] = ""
        return hits[: config.RERANK_OUTPUT]

    items = data.get("items", data if isinstance(data, list) else [])
    by_id = {h["id"]: h for h in hits}

    out = []
    for item in items:
        h = by_id.get(str(item.get("id")))
        if h is None or not item.get("relevant"):
            continue
        h["why"] = (item.get("why") or "").strip()
        out.append(h)
        if len(out) >= config.RERANK_OUTPUT:
            break

    # An exact match is evidence the reranker cannot see: it means this thread
    # references the very commit that wrote these lines. Never drop one, and
    # keep exact hits above semantic ones, in the order the exact tier ranked
    # them (PR match before SHA before bare file path).
    rescued = [h for h in hits if h.get("match_type") == "exact" and h not in out]
    for h in rescued:
        if not h.get("why"):
            h["why"] = _EXACT_WHY.get(h.get("reason", ""), "Directly references this code")

    exact_kept = [h for h in out if h.get("match_type") == "exact"]
    rest = [h for h in out if h.get("match_type") != "exact"]
    ordered = _in_tier_order(hits, exact_kept + rescued) + rest

    return ordered[: config.RERANK_OUTPUT]


_EXACT_WHY = {
    "pr": "References the pull request that introduced these lines",
    "sha": "References the commit that introduced these lines",
    "path": "Discusses this file directly",
}


def _in_tier_order(hits: list[dict], subset: list[dict]) -> list[dict]:
    """Restore the exact tier's own ranking over an arbitrary subset."""
    rank = {id(h): i for i, h in enumerate(hits)}
    return sorted(subset, key=lambda h: rank.get(id(h), 1_000))


async def synthesize(hits: list[dict], *, description: str) -> str | None:
    if not hits:
        return None

    threads = []
    for i, h in enumerate(hits, 1):
        p = h["payload"]
        threads.append(
            f'[{i}] #{p.get("channel_name")} ({p.get("date_str", "")})\n'
            f'{(p.get("raw_text") or p.get("summary") or "")[:2500]}'
        )

    prompt = SYNTHESIS_PROMPT.format(description=description, threads="\n\n".join(threads))
    try:
        return await llm.complete(prompt, config.SYNTHESIS_MODEL, max_tokens=350)
    except Exception as exc:
        print(f"  ! synthesis failed ({exc})")
        return None
