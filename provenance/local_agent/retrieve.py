"""Retrieval over the private on-device store.

Deliberately the same *shape* as `service/retrieve.py` -- an exact tier of asserted
structural matches, a dense tier scored on the identical (1 + cosine) / 2 scale, the
same time/author/channel/reaction weights, the same semantic floor -- because the
extension merges the two lists into one. Two planes ranked on two scales would
produce an order that means nothing, and the person would have no way to tell.

What is *not* here, and will not be:

  * **No reranker and no synthesis.** Both are LLM calls over the full text of the
    top hits. The shared plane runs them centrally over shared threads; running them
    over private threads would be a second remote round-trip with private content in
    it, beyond what indexing consent covers. Instead each private hit gets a
    deterministic `why` derived from what actually matched -- no tokens, no network,
    and it says something true rather than something fluent.
  * **No BM25.** A lexical index needs plaintext, and the store keeps none. Exact
    structural matching is preserved through keyed term hashes (see `store.py`),
    which is the part of the lexical channel that was carrying real weight anyway.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .. import config, llm, models
from ..models import BlameInfo
from ..service import retrieve as shared
from .store import LocalStore


def exact_terms(blame: BlameInfo, file_path: str, symbols: list[str] | None) -> dict:
    """The structural handles a selection offers, in the store's term vocabulary."""
    basename = file_path.rsplit("/", 1)[-1] if file_path else ""
    return {
        "pr": [str(n) for n in blame.pr_numbers],
        "sha": list(blame.all_shas),
        "path": [file_path] if file_path else [],
        "basename": [basename] if basename else [],
        "symbol": shared.distinctive_symbols(symbols),
    }


def _date_str(payload: dict) -> str:
    ts = payload.get("ts_start")
    if not ts:
        return ""
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d")


def why_for(hit: dict, blame: BlameInfo, file_path: str) -> str:
    """A short, true explanation of why this private thread is here.

    Built from the match itself rather than generated. "Names settlement.py" is worth
    more to a reader than a fluent sentence that might be wrong, and it costs nothing.
    """
    payload = hit["payload"]
    basename = file_path.rsplit("/", 1)[-1] if file_path else ""
    shared_prs = sorted(set(payload.get("pr_refs", [])) & set(blame.pr_numbers))
    if shared_prs:
        return f"Names PR #{shared_prs[0]}, which these lines belong to."
    shared_shas = sorted(set(payload.get("commit_shas", [])) & set(blame.all_shas))
    if shared_shas:
        return f"Names commit {shared_shas[0]}, which touched these lines."
    if file_path and file_path in payload.get("file_paths", []):
        return f"Names {file_path}."
    if basename and basename in payload.get("file_basenames", []):
        return f"Names {basename}."
    participants = set(payload.get("participants", [])) & set(blame.authors)
    if participants:
        return f"{sorted(participants)[0]} took part, and also wrote these lines."
    return "Semantically close to this code; nothing in it names the file directly."


async def retrieve(store: LocalStore, queries: dict, blame: BlameInfo,
                   file_path: str) -> dict:
    """-> the same dict `service.retrieve.retrieve` returns, scoped `user_private`."""
    vectors = await llm.embed_dense([queries["dense_text"]])
    if not vectors:
        return {"hits": [], "best_dense": 0.0, "has_exact": False,
                "semantic_floor": 0.0, "scope": config.SCOPE_PRIVATE}

    dense = store.search_dense(vectors[0], config.PREFETCH_LIMIT)
    for hit in dense:
        hit["id"] = hit["doc_id"]
        hit["dense_score"] = hit["score"]
        hit["fused_score"] = hit["score"]
        hit["match_type"] = "semantic"
        hit["exact_strength"] = 0
        shared.adjust(hit, blame.authors, blame.commit_ts)
        hit["payload"]["date_str"] = _date_str(hit["payload"])
    dense.sort(key=lambda h: -h["score"])

    best_dense = max((h["dense_score"] for h in dense), default=0.0)
    floor = shared.semantic_floor(best_dense)
    near = [h for h in dense if h["dense_score"] >= floor]

    dense_by_id = {h["id"]: h for h in dense}
    exact = store.search_exact(exact_terms(blame, file_path, queries.get("symbols")))
    for hit in exact:
        hit["id"] = hit["doc_id"]
        hit["match_type"] = "exact"
        known = dense_by_id.get(hit["id"])
        hit["dense_score"] = known["dense_score"] if known else 0.0
        hit["fused_score"] = hit["dense_score"]
        # Asserted, not scored (2.3): an exact hit is never filtered by the floor.
        hit["score"] = hit["dense_score"] or 1.0
        hit["payload"]["date_str"] = _date_str(hit["payload"])
    exact.sort(key=lambda h: (-h["exact_strength"], -h["payload"].get("ts_end", 0)))
    exact = exact[:config.EXACT_TIER_CAP]

    seen = {h["id"] for h in exact}
    hits = exact + [h for h in near if h["id"] not in seen]
    for hit in hits:
        hit["why"] = why_for(hit, blame, file_path)

    return {"hits": hits[:config.RERANK_OUTPUT], "best_dense": best_dense,
            "has_exact": bool(exact), "semantic_floor": floor,
            "scope": config.SCOPE_PRIVATE}


def to_results(hits: list[dict]) -> list[models.Result]:
    out = []
    for hit in hits:
        payload = hit["payload"]
        out.append(models.Result(
            id=hit["id"],
            channel_name=payload.get("channel_name", ""),
            permalink=payload.get("permalink", ""),
            summary=payload.get("summary", ""),
            why=hit.get("why", ""),
            participants=payload.get("participants", []),
            date=payload.get("date_str", ""),
            match_type=hit["match_type"],
            score=round(float(hit.get("score", 0.0)), 6),
            raw_text=payload.get("raw_text", ""),
            scope=config.SCOPE_PRIVATE,
            display_scope=config.display_scope(config.SCOPE_PRIVATE),
        ))
    return out
