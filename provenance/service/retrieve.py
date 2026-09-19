"""Retrieval: the exact tier, the semantic tier, relevance weighting, and the null
decision.

Two independent strategy flags (5) pick between code paths without any caller
knowing which one ran. Both converge on the same in-memory shape:

    {"id": str, "payload": dict, "score": float, "dense_score": float,
     "match_type": "exact"|"semantic", "exact_strength": int}

One deviation from 9.5 worth stating plainly: Elasticsearch cannot nest a
`retriever`/`rrf` block inside a `function_score` query, and `knn` does not accept
`function_score` either -- the native-weighting JSON in the spec is not valid ES.
So `ES_USE_NATIVE_FUNCTION_SCORE=True` applies the weights server-side in the one
place ES does support them (the lexical `standard`/BM25 query, which shifts that
channel's ranks before fusion), and `False` applies the identical weights in Python
to the fused score, covering both channels. The False path is the more faithful
implementation of the spec's intent; the True path is the one that does real
server-side work. They are single-application either way -- the weights are never
counted twice.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from .. import config, llm
from ..models import BlameInfo


class EmbedderMismatch(RuntimeError):
    """The index was built with a different embedder than this process uses."""


class RetrievalUnavailable(RuntimeError):
    """Elasticsearch is unreachable or the index does not exist."""


_LEXICAL_FIELDS = ["summary^2", "raw_text", "symbols^1.5"]


# --- guards -------------------------------------------------------------------


def check_embedder(es) -> None:
    """9.3: refuse to compare vectors produced by different models.

    A transport failure is deliberately *not* folded into "missing stamp": an
    unreachable cluster is 18 row 17 and must say so, not send the caller off to
    re-run an ingest that was never the problem.
    """
    from elastic_transport import TransportError
    from elasticsearch import ApiError, NotFoundError

    try:
        stamped = es.get(index=config.META_INDEX, id="embedder")["_source"]["embedder_id"]
    except NotFoundError:
        stamped = None                       # no index, or no stamp in it
    except (ApiError, TransportError) as exc:
        raise RetrievalUnavailable(str(exc)) from exc
    except Exception:
        stamped = None

    if stamped is None:
        raise EmbedderMismatch(
            f"index '{config.INDEX}' has no embedder stamp -- run `make ingest` to build it"
        )
    if stamped != config.EMBEDDER_ID:
        raise EmbedderMismatch(
            f"index was built with embedder '{stamped}' but this process uses "
            f"'{config.EMBEDDER_ID}' -- re-run `make ingest` with --recreate"
        )


# --- weighting ------------------------------------------------------------------


def time_weight(ts_start: float, commit_ts: float | None) -> float:
    """Gaussian decay around the commit, deliberately symmetric.

    No offset truncates the post-commit side: a "this broke prod" thread from after
    the commit is frequently the most valuable evidence there is, and must not be
    penalised more than a pre-commit thread the same distance away.
    """
    if not commit_ts:
        return 1.0
    dt = ts_start - commit_ts
    return max(
        config.TIME_WEIGHT_FLOOR,
        math.exp(-(dt * dt) / (2 * config.TIME_SIGMA_SECONDS ** 2)),
    )


def adjust(hit: dict, blame_authors: list[str], commit_ts: float | None) -> dict:
    p = hit["payload"]
    w_time = time_weight(p.get("ts_start", 0.0), commit_ts)
    w_author = config.AUTHOR_MATCH_BOOST if set(blame_authors) & set(p.get("participants", [])) else 1.0
    w_channel = config.CHANNEL_TIER_WEIGHT.get(p.get("channel_tier", 2), 1.0)
    w_react = config.REACTION_BOOST if p.get("is_bookmarked") else 1.0
    hit["score"] = hit["fused_score"] * w_time * w_author * w_channel * w_react
    return hit


def _score_functions(blame: BlameInfo) -> list[dict]:
    """The 9.5 function list, used only on the native weighting path."""
    functions: list[dict] = []
    if blame.commit_ts:
        functions.append({"gauss": {"ts_start": {
            "origin": int(blame.commit_ts), "scale": "60d", "decay": 0.3,
        }}})
    if blame.authors:
        functions.append({
            "filter": {"terms": {"participants": blame.authors}},
            "weight": config.AUTHOR_MATCH_BOOST,
        })
    functions.append({"filter": {"term": {"channel_tier": 1}}, "weight": config.CHANNEL_TIER_WEIGHT[1]})
    functions.append({"filter": {"term": {"channel_tier": 3}}, "weight": config.CHANNEL_TIER_WEIGHT[3]})
    functions.append({"filter": {"term": {"is_bookmarked": True}}, "weight": config.REACTION_BOOST})
    return functions


def _lexical_query(queries: dict, blame: BlameInfo) -> dict:
    base = {"multi_match": {"query": queries["sparse_text"], "fields": _LEXICAL_FIELDS}}
    if not config.ES_USE_NATIVE_FUNCTION_SCORE:
        return base
    return {"function_score": {
        "query": base,
        "functions": _score_functions(blame),
        "score_mode": "multiply",
        "boost_mode": "multiply",
    }}


# --- payload helpers -------------------------------------------------------------


def _payload(source: dict) -> dict:
    p = dict(source)
    p.pop("dense_vec", None)   # never ship 1536 floats to a caller
    ts = p.get("ts_start")
    if ts:
        p["date_str"] = datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d")
    else:
        p["date_str"] = ""
    return p


def _search(es, **kwargs) -> dict:
    from elastic_transport import TransportError
    from elasticsearch import ApiError

    try:
        return es.search(index=config.INDEX, **kwargs)
    except (ApiError, TransportError) as exc:
        raise RetrievalUnavailable(str(exc)) from exc


# --- tiers -------------------------------------------------------------------------


def exact_tier(es, blame: BlameInfo, file_path: str) -> list[dict]:
    """Structural evidence: a thread that names the PR, commit, or file by identity.

    This is asserted, not scored (2.3). No vector math is involved and no later
    stage may discard it -- rerank.py rescues anything the reranker rejects here.
    """
    should: list[dict] = []
    if blame.pr_numbers:
        should.append({"terms": {"pr_refs": blame.pr_numbers}})
    if blame.all_shas:
        should.append({"terms": {"commit_shas": blame.all_shas}})
    if file_path:
        should.append({"term": {"file_paths": file_path}})
    if not should:
        return []

    resp = _search(
        es,
        query={"bool": {"should": should, "minimum_should_match": 1}},
        size=config.PREFETCH_LIMIT,
        source_excludes=["dense_vec"],
    )

    pr_set, sha_set = set(blame.pr_numbers), set(blame.all_shas)
    hits: list[dict] = []
    for h in resp["hits"]["hits"]:
        payload = _payload(h["_source"])
        if pr_set & set(payload.get("pr_refs", [])):
            strength = 3
        elif sha_set & set(payload.get("commit_shas", [])):
            strength = 2
        elif file_path in payload.get("file_paths", []):
            strength = 1
        else:
            continue
        hits.append({
            "id": h["_id"], "payload": payload, "match_type": "exact",
            "exact_strength": strength, "fused_score": float(h["_score"]),
            "score": float(h["_score"]), "dense_score": 0.0,
        })

    # PR-match before SHA-match before path-match; newest first inside a strength.
    hits.sort(key=lambda h: (-h["exact_strength"], -h["payload"].get("ts_end", 0)))
    return hits[:config.EXACT_TIER_CAP]


def _knn_search(es, vector: list[float]) -> dict[str, dict]:
    """Raw kNN, always run.

    Its `_score` is ES's cosine similarity normalised to (1 + cosine) / 2, i.e.
    always in [0, 1]. That -- never the rank-based RRF score -- is what
    NULL_THRESHOLD compares against (9.4).
    """
    resp = _search(
        es,
        knn={
            "field": "dense_vec", "query_vector": vector,
            "k": config.PREFETCH_LIMIT, "num_candidates": 100,
        },
        size=config.PREFETCH_LIMIT,
        source_excludes=["dense_vec"],
    )
    return {h["_id"]: h for h in resp["hits"]["hits"]}


def _bm25_search(es, queries: dict, blame: BlameInfo) -> dict[str, dict]:
    resp = _search(
        es,
        query=_lexical_query(queries, blame),
        size=config.PREFETCH_LIMIT,
        source_excludes=["dense_vec"],
    )
    return {h["_id"]: h for h in resp["hits"]["hits"]}


def _native_rrf(es, vector: list[float], queries: dict, blame: BlameInfo) -> list[dict]:
    """ES-side reciprocal rank fusion (8.16+ with the RRF entitlement)."""
    resp = _search(
        es,
        retriever={"rrf": {
            "retrievers": [
                {"knn": {
                    "field": "dense_vec", "query_vector": vector,
                    "k": config.PREFETCH_LIMIT, "num_candidates": 100,
                }},
                {"standard": {"query": _lexical_query(queries, blame)}},
            ],
            "rank_window_size": config.PREFETCH_LIMIT,
            "rank_constant": 20,
        }},
        size=config.FUSION_LIMIT,
        source_excludes=["dense_vec"],
    )
    return [
        {"id": h["_id"], "payload": _payload(h["_source"]), "fused_score": float(h["_score"])}
        for h in resp["hits"]["hits"]
    ]


def _python_rrf(ranked_lists: list[dict[str, dict]]) -> list[dict]:
    """score(d) = sum over lists of 1 / (k + rank_i(d)), k=60. Identical in shape to
    the fusion the original Qdrant implementation used."""
    scores: dict[str, float] = {}
    sources: dict[str, dict] = {}
    for hits in ranked_lists:
        for rank, (doc_id, hit) in enumerate(hits.items(), start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (config.RRF_K + rank)
            sources.setdefault(doc_id, hit["_source"])
    ordered = sorted(scores.items(), key=lambda kv: -kv[1])[:config.FUSION_LIMIT]
    return [
        {"id": doc_id, "payload": _payload(sources[doc_id]), "fused_score": score}
        for doc_id, score in ordered
    ]


# --- orchestration -------------------------------------------------------------------


async def retrieve(es, queries: dict, blame: BlameInfo, file_path: str) -> dict:
    """-> {"hits": [...], "best_dense": float, "has_exact": bool}

    `hits` is exact-tier hits first (in PR > SHA > path order), then semantic hits by
    weighted score, deduplicated by document id with the exact classification winning.
    """
    check_embedder(es)

    vectors = await llm.embed_dense([queries["dense_text"]])
    if not vectors:
        raise RetrievalUnavailable("embedding the query returned nothing")
    vector = vectors[0]

    knn_hits = _knn_search(es, vector)

    if config.ES_USE_NATIVE_RRF:
        fused = _native_rrf(es, vector, queries, blame)
    else:
        fused = _python_rrf([knn_hits, _bm25_search(es, queries, blame)])

    for hit in fused:
        knn_hit = knn_hits.get(hit["id"])
        # A BM25-only match never appeared in the kNN list; treat it as cosine 0.0
        # for the null decision (9.4).
        hit["dense_score"] = float(knn_hit["_score"]) if knn_hit else 0.0
        hit["match_type"] = "semantic"
        hit["exact_strength"] = 0
        if config.ES_USE_NATIVE_FUNCTION_SCORE:
            hit["score"] = hit["fused_score"]      # already weighted server-side
        else:
            adjust(hit, blame.authors, blame.commit_ts)

    fused.sort(key=lambda h: -h["score"])

    exact = exact_tier(es, blame, file_path)
    for hit in exact:
        knn_hit = knn_hits.get(hit["id"])
        hit["dense_score"] = float(knn_hit["_score"]) if knn_hit else 0.0

    seen = {h["id"] for h in exact}
    hits = exact + [h for h in fused if h["id"] not in seen]

    best_dense = max((h["dense_score"] for h in fused), default=0.0)
    return {"hits": hits[:config.RERANK_INPUT], "best_dense": best_dense, "has_exact": bool(exact)}


def is_null_result(result: dict) -> bool:
    """12.5: no structural hit and nothing semantically close enough. Short-circuits
    before any rerank/synthesis tokens are spent."""
    return not result["has_exact"] and result["best_dense"] < config.NULL_THRESHOLD
