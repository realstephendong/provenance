"""Two-tier retrieval, fusion, and score adjustment.

The exact tier cannot fail on stage: it is a filter query over indexed PR
numbers, SHAs and file paths, with no vector involved. The semantic tier
fills the remaining slots.
"""

from __future__ import annotations

import math

from qdrant_client import models

from .. import config, llm
from ..ingest import load


class EmbedderMismatch(RuntimeError):
    pass


def check_embedder(qc) -> None:
    """Trap #3, caught loudly instead of silently returning garbage."""
    built = load.read_embedder_id(qc)
    if built is None:
        raise EmbedderMismatch(
            f"collection '{config.COLLECTION}' has no embedder stamp. Re-run "
            f"ingest with --recreate."
        )
    if built != config.EMBEDDER_ID:
        raise EmbedderMismatch(
            f"collection was built with embedder '{built}' but this process "
            f"uses '{config.EMBEDDER_ID}'. Vectors are not comparable. "
            f"Re-run ingest with --recreate to rebuild it."
        )


# --- Tier 1: exact --------------------------------------------------------


def exact_hits(qc, pr_numbers: list[int], short_shas: list[str], file_path: str) -> list[dict]:
    conditions = []
    if pr_numbers:
        conditions.append(
            models.FieldCondition(key="pr_refs", match=models.MatchAny(any=pr_numbers))
        )
    if short_shas:
        conditions.append(
            models.FieldCondition(key="commit_shas", match=models.MatchAny(any=short_shas))
        )
    if file_path:
        conditions.append(
            models.FieldCondition(key="file_paths", match=models.MatchAny(any=[file_path]))
        )
    if not conditions:
        return []

    points, _ = qc.scroll(
        collection_name=config.COLLECTION,
        scroll_filter=models.Filter(should=conditions),
        limit=20,
        with_payload=True,
    )

    out = []
    for p in points:
        payload = p.payload or {}
        # A PR match is a far stronger claim than a file-path match: the file
        # says "this thread mentions this file", the PR says "this thread is
        # about the commit that wrote these exact lines".
        if pr_numbers and set(payload.get("pr_refs") or []) & set(pr_numbers):
            strength, reason = 3, "pr"
        elif short_shas and set(payload.get("commit_shas") or []) & set(short_shas):
            strength, reason = 2, "sha"
        else:
            strength, reason = 1, "path"
        out.append({"id": str(p.id), "payload": payload, "strength": strength, "reason": reason})

    out.sort(key=lambda h: (-h["strength"], -float(h["payload"].get("ts_start") or 0)))
    return out[: config.EXACT_TIER_CAP]


# --- Tier 2: hybrid semantic ---------------------------------------------


async def semantic_hits(qc, dense_text: str, sparse_text: str) -> list[dict]:
    dense_vec = (await llm.embed_dense([dense_text]))[0]
    idx, val = llm.embed_sparse([sparse_text])[0]
    sparse_vec = models.SparseVector(indices=idx, values=val)

    resp = qc.query_points(
        collection_name=config.COLLECTION,
        prefetch=[
            models.Prefetch(query=dense_vec, using="dense", limit=config.PREFETCH_LIMIT),
            models.Prefetch(query=sparse_vec, using="sparse", limit=config.PREFETCH_LIMIT),
        ],
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        limit=config.FUSION_LIMIT,
        with_payload=True,
    )

    # RRF is rank-based and scale-free: its scores say who won, never whether
    # anyone deserved to. Null handling needs an absolute signal, so take the
    # raw cosine from a dense-only pass. One extra local Qdrant round trip.
    cosine = qc.query_points(
        collection_name=config.COLLECTION,
        query=dense_vec,
        using="dense",
        limit=config.PREFETCH_LIMIT,
        with_payload=False,
    )
    dense_scores = {str(p.id): float(p.score) for p in cosine.points}

    return [
        {
            "id": str(p.id),
            "payload": p.payload or {},
            "fused": float(p.score),
            "dense_score": dense_scores.get(str(p.id), 0.0),
        }
        for p in resp.points
    ]


# --- Score adjustment -----------------------------------------------------


def time_weight(ts_start: float, commit_ts: float | None) -> float:
    """Two-sided Gaussian around the commit date.

    Deliberately two-sided, and worth defending in the pitch: threads *before*
    the commit explain intent, threads *after* explain consequences, and the
    post-hoc "this broke prod" thread is usually the most valuable result. A
    hard cutoff at commit time throws those away. The floor keeps an
    old-but-perfect match alive.
    """
    if not commit_ts:
        return 1.0
    dt = ts_start - commit_ts
    return max(
        config.TIME_WEIGHT_FLOOR,
        math.exp(-(dt * dt) / (2 * config.TIME_SIGMA_SECONDS ** 2)),
    )


def adjust(hit: dict, base: float, blame_authors: list[str]) -> dict:
    p = hit["payload"]
    w_time = time_weight(float(p.get("ts_start") or 0), hit.get("commit_ts"))
    w_author = (
        config.AUTHOR_MATCH_BOOST
        if set(blame_authors) & set(p.get("participants") or [])
        else 1.0
    )
    w_channel = config.CHANNEL_TIER_WEIGHT.get(int(p.get("channel_tier") or 2), 1.0)
    w_react = config.REACTION_BOOST if p.get("is_bookmarked") else 1.0

    hit["weights"] = {
        "time": round(w_time, 3),
        "author": w_author,
        "channel": w_channel,
        "react": w_react,
    }
    hit["score"] = base * w_time * w_author * w_channel * w_react
    return hit


async def retrieve(qc, *, queries: dict, blame, file_path: str) -> tuple[list[dict], list[dict]]:
    """Returns (exact, semantic), both scored, semantic sorted best-first."""
    check_embedder(qc)

    exact = exact_hits(qc, blame.pr_numbers, blame.all_shas, file_path)
    exact_ids = {h["id"] for h in exact}

    semantic = await semantic_hits(qc, queries["dense_text"], queries["sparse_text"])
    semantic = [h for h in semantic if h["id"] not in exact_ids]

    # Normalize the fused score for ranking only. The absolute cosine on each
    # hit, not this, is what decides whether anything is relevant at all.
    top = max((h["fused"] for h in semantic), default=0.0) or 1.0
    for h in semantic:
        h["commit_ts"] = blame.commit_ts
        adjust(h, h["fused"] / top, blame.authors)

    for h in exact:
        h["commit_ts"] = blame.commit_ts
        h.setdefault("dense_score", 1.0)  # an exact hit needs no similarity
        adjust(h, 1.0, blame.authors)
        h["match_type"] = "exact"

    semantic.sort(key=lambda h: -h["score"])
    for h in semantic:
        h["match_type"] = "semantic"

    return exact, semantic
