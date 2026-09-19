"""FastAPI service: /health, /context/count, /context.

`POST /context` is the one backend contract all three surfaces consume -- the VS Code
extension, the MCP server, and the terminal CLI. There is deliberately no second
retrieval implementation anywhere (18 row 32).
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager, contextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .. import config, observability
from ..ingest import load
from ..models import (
    BlameInfo, ContextRequest, ContextResponse, CountResponse, Result,
)
from . import gitctx, graph as graph_mod, query_build, rerank, retrieve


@asynccontextmanager
async def lifespan(_app: FastAPI):
    config.require_api_key()      # 18 row 1: refuse to boot, not to fail first query
    observability.init()
    yield


app = FastAPI(title="Provenance", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

_es = None


def es_client():
    """Process-wide singleton. Safe under asyncio's single-threaded event loop with
    no locking (18 row 33)."""
    global _es
    if _es is None:
        _es = load.client()
    return _es


@contextmanager
def _timed(timings: dict[str, int], name: str):
    """One stage: a Sentry span and a wall-clock entry in `timing_ms`."""
    started = time.perf_counter()
    with observability.span(name):
        yield
    timings[name] = int((time.perf_counter() - started) * 1000)


@app.get("/health")
def health() -> dict:
    out = {"ok": False, "es": config.ES_URL, "api_key_present": bool(config.OPENAI_API_KEY)}
    try:
        es = es_client()
        out["index_exists"] = bool(es.indices.exists(index=config.INDEX))
        out["embedder_id"] = load.read_embedder_id(es)
        out["expected_embedder_id"] = config.EMBEDDER_ID
        out["ok"] = bool(out["index_exists"]) and out["embedder_id"] == config.EMBEDDER_ID
    except Exception as exc:
        out["error"] = str(exc)
    return out


def _to_results(hits: list[dict]) -> list[Result]:
    results = []
    for hit in hits:
        p = hit["payload"]
        results.append(Result(
            id=hit["id"],
            channel_name=p.get("channel_name", ""),
            permalink=p.get("permalink", ""),
            summary=p.get("summary", ""),
            why=hit.get("why", ""),
            participants=p.get("participants", []),
            date=p.get("date_str", ""),
            match_type=hit["match_type"],
            score=round(float(hit.get("score", 0.0)), 6),
            raw_text=p.get("raw_text", ""),
        ))
    return results


@app.post("/context", response_model=ContextResponse)
async def context(req: ContextRequest) -> ContextResponse:
    timings: dict[str, int] = {}

    # git blame is a subprocess and query_build is an LLM call -- neither needs the
    # other, so they overlap. blame goes to a thread so it does not block the loop.
    # Each gets its own span and its own timing entry; the two overlap in wall clock,
    # so they intentionally do not sum to the request total.
    async def _blame():
        with _timed(timings, "git.blame"):
            return await asyncio.to_thread(
                gitctx.blame, req.repo_root, req.file_path, req.line_start, req.line_end
            )

    async def _queries():
        with _timed(timings, "query_build"):
            return await query_build.build_queries(req.code, req.file_path, req.language)

    blame, queries = await asyncio.gather(_blame(), _queries())

    try:
        with _timed(timings, "retrieve"):
            found = await retrieve.retrieve(es_client(), queries, blame, req.file_path)
    except retrieve.EmbedderMismatch as exc:
        # 18 row 18: never silently compare incompatible vectors.
        return ContextResponse(
            blame=blame, timing_ms=timings,
            message="The search index was built with a different embedding model. "
                    "Re-run `make ingest` to rebuild it.",
            error=f"EmbedderMismatch: {exc}",
        )
    except retrieve.RetrievalUnavailable as exc:
        # 18 row 17: a diagnostic message, not a raw 500.
        return ContextResponse(
            blame=blame, timing_ms=timings,
            message="Could not reach the search index. Is Elasticsearch running?",
            error=f"RetrievalUnavailable: {exc}",
        )

    # 12.5: short-circuit before spending a single token on rerank or synthesis.
    if retrieve.is_null_result(found):
        observability.log_info(
            "null threshold fired",
            best_dense=found["best_dense"], threshold=config.NULL_THRESHOLD,
            file_path=req.file_path,
        )
        with _timed(timings, "resolve_graph"):
            graph = graph_mod.resolve(blame, [], [])
        return ContextResponse(
            blame=blame, graph=graph, timing_ms=timings,
            message="No relevant discussions found for this code.",
        )

    with _timed(timings, "rerank"):
        ranked = await rerank.rerank(found["hits"], queries["description"])

    if not ranked:
        with _timed(timings, "resolve_graph"):
            graph = graph_mod.resolve(blame, [], [])
        return ContextResponse(
            blame=blame, graph=graph, timing_ms=timings,
            message="No relevant discussions found for this code.",
        )

    with _timed(timings, "synthesis"):
        synthesis, conflicts = await rerank.synthesize(ranked, queries["description"])

    if conflicts:
        observability.log_info(
            "synthesis reported conflicting evidence",
            pairs=[c.model_dump() for c in conflicts], file_path=req.file_path,
        )

    # Graph resolution runs after synthesis, not before: the conflict/supersede edges
    # are built from what synthesis flagged. It is sync, deterministic, and has no
    # network or LLM call of its own.
    with _timed(timings, "resolve_graph"):
        graph = graph_mod.resolve(blame, ranked, conflicts)

    return ContextResponse(
        synthesis=synthesis,
        results=_to_results(ranked),
        blame=blame,
        graph=graph,
        conflicts=conflicts,
        timing_ms=timings,
    )


@app.post("/context/count", response_model=CountResponse)
async def context_count(req: ContextRequest) -> CountResponse:
    """Retrieval only -- no rerank, no synthesis, no graph.

    CodeLens calls this once per top-level symbol per file open, so it also skips the
    code-to-prose LLM call and queries on the symbols-only description instead. One
    cheap embedding per lens, rather than a chat completion plus an embedding.
    """
    try:
        blame = await asyncio.to_thread(
            gitctx.blame, req.repo_root, req.file_path, req.line_start, req.line_end
        )
        symbols = query_build.extract_symbols(req.code, req.language)
        description = (
            f"Code in {req.file_path} involving: {', '.join(symbols[:15]) or 'no extracted symbols'}."
        )
        queries = {
            "symbols": symbols, "description": description, "dense_text": description,
            "sparse_text": " ".join(symbols) + "\n" + description,
        }
        found = await retrieve.retrieve(es_client(), queries, blame, req.file_path)
    except Exception:
        # A lens must never surface an error in the gutter.
        return CountResponse(count=0, has_exact=False)

    if retrieve.is_null_result(found):
        return CountResponse(count=0, has_exact=False)
    return CountResponse(count=len(found["hits"]), has_exact=found["has_exact"])
