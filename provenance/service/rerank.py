"""LLM rerank, exact-hit rescue, and synthesis with conflict detection.

Two of 2's non-negotiables live here. Structural evidence outranks inferred
evidence unconditionally: an exact-tier hit the reranker calls irrelevant is kept
anyway (18 row 21). And the system says "no relevant context" rather than
fabricate one: the synthesis prompt is explicitly instructed to decline.
"""

from __future__ import annotations

import json

from .. import config, llm
from ..models import ConflictPair

RERANK_PROMPT = """A developer selected a piece of code and wants to know which of
these engineering discussions actually explain why it is the way it is.

CODE DESCRIPTION:
{description}

CANDIDATE DISCUSSIONS:
{candidates}

For each candidate, decide whether it genuinely explains this code. Overlapping
vocabulary is not enough -- a discussion about retries in a different system is not
relevant to retries in this one. Be strict; marking everything relevant is useless.

Return a JSON object:
{{"items": [{{"index": 1, "relevant": true, "why": "at most 15 words"}}]}}

Order `items` by how strongly each candidate explains the code, strongest first.
Include every candidate index exactly once."""

SYNTHESIS_PROMPT = """A developer selected this code and asked why it is the way it
is.

CODE DESCRIPTION:
{description}

DISCUSSIONS, in the order they will be shown, numbered for citation:
{threads}

Write 2-3 sentences answering why the code is the way it is. Cite with [1], [2]
matching the numbers above. Name the specific reasons and numbers the discussions
give.

If two or more discussions give conflicting information (different numbers, different
decisions), say so explicitly and name which one the merged code and final PR
actually match -- the PR is ground truth; a proposal that was never implemented was
either rejected or superseded by a later change.

If the discussions do not actually explain this code, say so plainly in one sentence
instead of inventing a connection.

Return a JSON object: {{"synthesis": "...", "conflicts": [{{"a": 1, "b": 2, "kind":
"conflict"}}], "supersedes": [{{"a": 1, "b": 3, "kind": "supersede"}}]}}. Empty lists
if none apply."""

_EXACT_WHY = {
    5: "References the pull request that introduced these lines",
    4: "References the commit that introduced these lines",
    3: "Discusses the file these lines are in",
    2: "Names this file",
    1: "Names an identifier used in these lines",
}


def _render_candidates(hits: list[dict]) -> str:
    lines = []
    for i, hit in enumerate(hits, start=1):
        p = hit["payload"]
        lines.append(
            f"[{i}] #{p.get('channel_name', '?')} {p.get('date_str', '')}\n"
            f"    {p.get('summary', '')[:500]}"
        )
    return "\n".join(lines)


async def rerank(hits: list[dict], description: str) -> list[dict]:
    """Order hits by explanatory strength, dropping the genuinely irrelevant.

    Exact hits are always rescued and always ordered first. A total rerank failure
    falls back to raw score order with empty `why` lines -- it never drops the
    response (18 row 8).
    """
    if not hits:
        return []

    exact = [h for h in hits if h["match_type"] == "exact"]
    semantic = [h for h in hits if h["match_type"] != "exact"]
    for hit in exact:
        hit["why"] = _EXACT_WHY.get(hit.get("exact_strength", 1), _EXACT_WHY[1])


    verdicts: dict[int, dict] = {}
    try:
        raw = await llm.complete_json(
            RERANK_PROMPT.format(
                description=description, candidates=_render_candidates(hits)
            ),
            config.RERANK_MODEL,
        )
        for order, item in enumerate(raw.get("items", [])):
            try:
                index = int(item["index"])
            except (KeyError, TypeError, ValueError):
                continue
            if 1 <= index <= len(hits):
                verdicts[index] = {
                    "relevant": bool(item.get("relevant", True)),
                    "why": str(item.get("why", "")).strip(),
                    "order": order,
                }
    except Exception as exc:
        print(f"  ! rerank failed, falling back to score order: {exc}")
        for hit in semantic:
            hit.setdefault("why", "")
        return (exact + semantic)[:config.RERANK_OUTPUT]

    index_of = {id(hit): i + 1 for i, hit in enumerate(hits)}
    kept: list[dict] = []
    for hit in semantic:
        verdict = verdicts.get(index_of[id(hit)])
        if verdict is None:
            hit["why"] = ""
            hit["_order"] = len(hits)
            kept.append(hit)
            continue
        if not verdict["relevant"]:
            continue                      # respected -- 2.4
        hit["why"] = verdict["why"]
        hit["_order"] = verdict["order"]
        kept.append(hit)
    kept.sort(key=lambda h: h.get("_order", len(hits)))

    # Exact hits keep their own PR > SHA > path > basename > symbol order and lead
    # unconditionally.
    exact.sort(key=lambda h: -h.get("exact_strength", 0))
    return (exact + kept)[:config.RERANK_OUTPUT]


def _render_threads(hits: list[dict]) -> str:
    lines = []
    for i, hit in enumerate(hits, start=1):
        p = hit["payload"]
        tag = "EXACT" if hit["match_type"] == "exact" else "SEMANTIC"
        lines.append(
            f"[{i}] ({tag}) #{p.get('channel_name', '?')} on {p.get('date_str', '')}\n"
            f"    {p.get('summary', '')}\n"
            f"    raw: {p.get('raw_text', '')[:900]}"
        )
    return "\n".join(lines)


async def synthesize(hits: list[dict], description: str) -> tuple[str | None, list[ConflictPair]]:
    """-> (synthesis, conflicts). A parse or call failure returns (None, []) and the
    request still completes with evidence but no narrative (18 row 9)."""
    if not hits:
        return None, []

    try:
        raw = await llm.complete_json(
            SYNTHESIS_PROMPT.format(description=description, threads=_render_threads(hits)),
            config.SYNTHESIS_MODEL,
            max_tokens=900,
        )
    except Exception as exc:
        print(f"  ! synthesis failed: {exc}")
        return None, []

    if not isinstance(raw, dict):
        return None, []

    synthesis = str(raw.get("synthesis") or "").strip() or None

    conflicts: list[ConflictPair] = []
    seen: set[tuple[int, int, str]] = set()
    for key, kind in (("conflicts", "conflict"), ("supersedes", "supersede")):
        for entry in raw.get(key) or []:
            if not isinstance(entry, dict):
                continue
            try:
                a, b = int(entry["a"]), int(entry["b"])
            except (KeyError, TypeError, ValueError):
                continue
            if a == b or not (1 <= a <= len(hits) and 1 <= b <= len(hits)):
                continue
            # The model is told which key means which, but it does put `kind` in the
            # entry too -- trust the key it filed the entry under.
            marker = (a, b, kind)
            if marker in seen:
                continue
            seen.add(marker)
            conflicts.append(ConflictPair(a=a, b=b, kind=kind))

    return synthesis, conflicts
