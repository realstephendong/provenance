"""POST /context -- the contract the extension and the MCP server both call.

Budget: under 8 seconds end to end. Steps that do not depend on each other
run concurrently; the local git work overlaps the code-to-prose LLM call.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .. import config
from ..ingest import load
from ..models import BlameInfo, ContextRequest, ContextResponse, Result
from . import gitctx, query_build, rerank as rerank_mod, retrieve as retrieve_mod

@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Refuse to start without a key rather than failing on the first query,
    # which on stage would look like the product is broken.
    config.require_api_key()
    yield


app = FastAPI(title="Hindsight", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

_qc = None


def qdrant():
    global _qc
    if _qc is None:
        _qc = load.client()
    return _qc


def _date_str(ts: float | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d")


@app.get("/health")
def health() -> dict:
    out: dict = {
        "ok": False,
        "qdrant": config.QDRANT_URL,
        "api_key_present": bool(config.OPENAI_API_KEY),
    }
    try:
        qc = qdrant()
        out["collection_exists"] = qc.collection_exists(config.COLLECTION)
        if out["collection_exists"]:
            out["points"] = qc.count(config.COLLECTION).count
        out["embedder_id"] = load.read_embedder_id(qc)
        out["expected_embedder_id"] = config.EMBEDDER_ID
        out["ok"] = bool(out["collection_exists"]) and out["embedder_id"] == config.EMBEDDER_ID
    except Exception as exc:
        out["error"] = str(exc)
    return out


class CountResponse(BaseModel):
    count: int
    has_exact: bool


@app.post("/context/count", response_model=CountResponse)
async def context_count(req: ContextRequest) -> CountResponse:
    """Retrieval only: no rerank, no synthesis.

    The CodeLens provider calls this once per top-level symbol. Routing it at
    /context would triple the LLM calls for a single file open, which is
    exactly what makes CodeLens feel slow.
    """
    blame_info, queries = await asyncio.gather(
        asyncio.to_thread(
            gitctx.blame, req.repo_root, req.file_path, req.line_start, req.line_end
        ),
        query_build.build_queries(req.code, req.file_path, req.language),
    )
    try:
        exact, semantic = await retrieve_mod.retrieve(
            qdrant(), queries=queries, blame=blame_info, file_path=req.file_path
        )
    except retrieve_mod.EmbedderMismatch:
        return CountResponse(count=0, has_exact=False)

    relevant = [h for h in semantic if h.get("dense_score", 0.0) >= config.NULL_THRESHOLD]
    return CountResponse(
        count=len(exact) + len(relevant[: config.RERANK_OUTPUT]),
        has_exact=bool(exact),
    )


@app.post("/context", response_model=ContextResponse)
async def context(req: ContextRequest) -> ContextResponse:
    timing: dict[str, int] = {}
    t_all = time.perf_counter()

    async def timed(name, coro):
        t = time.perf_counter()
        result = await coro
        timing[name] = int((time.perf_counter() - t) * 1000)
        return result

    # Steps 1-3 are pure local work and overlap the code-to-prose call.
    blame_info, queries = await asyncio.gather(
        timed(
            "git",
            asyncio.to_thread(
                gitctx.blame, req.repo_root, req.file_path, req.line_start, req.line_end
            ),
        ),
        timed("query_build", query_build.build_queries(req.code, req.file_path, req.language)),
    )

    if blame_info.uncommitted and not blame_info.dominant_sha:
        # Trap #4: unstaged lines have no commit and therefore no time anchor.
        blame_info = BlameInfo(uncommitted=True)

    try:
        exact, semantic = await timed(
            "retrieve",
            retrieve_mod.retrieve(
                qdrant(), queries=queries, blame=blame_info, file_path=req.file_path
            ),
        )
    except retrieve_mod.EmbedderMismatch as exc:
        return ContextResponse(
            blame=blame_info,
            message=str(exc),
            timing_ms={**timing, "total": int((time.perf_counter() - t_all) * 1000)},
        )

    for h in exact + semantic:
        h["payload"]["date_str"] = _date_str(h["payload"].get("ts_start"))

    # Absolute cosine, not the rank-fused score: see retrieve.semantic_hits.
    best_semantic = max((h.get("dense_score", 0.0) for h in semantic), default=0.0)

    # A tool that says "no relevant discussions found" reads as far more
    # credible than one that returns five weak links.
    if not exact and best_semantic < config.NULL_THRESHOLD:
        timing["total"] = int((time.perf_counter() - t_all) * 1000)
        return ContextResponse(
            blame=blame_info,
            timing_ms=timing,
            message="No relevant discussions found for this code.",
        )

    candidates = exact + semantic
    kept = await timed(
        "rerank",
        rerank_mod.rerank(
            candidates,
            description=queries["description"],
            file_path=req.file_path,
            symbols=queries["symbols"],
        ),
    )

    if not kept:
        timing["total"] = int((time.perf_counter() - t_all) * 1000)
        return ContextResponse(
            blame=blame_info,
            timing_ms=timing,
            message="No relevant discussions found for this code.",
        )

    synthesis = await timed(
        "synthesis", rerank_mod.synthesize(kept, description=queries["description"])
    )

    results = []
    for h in kept:
        p = h["payload"]
        results.append(
            Result(
                id=h["id"],
                channel_name=p.get("channel_name", ""),
                permalink=p.get("permalink", ""),
                summary=p.get("summary", ""),
                why=h.get("why", ""),
                participants=p.get("participants", []) or [],
                date=p.get("date_str", ""),
                match_type=h.get("match_type", "semantic"),
                score=round(float(h.get("score", 0.0)), 4),
                raw_text=p.get("raw_text", "") or "",
            )
        )

    timing["total"] = int((time.perf_counter() - t_all) * 1000)
    return ContextResponse(
        synthesis=synthesis, results=results, blame=blame_info, timing_ms=timing
    )
