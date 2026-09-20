"""FastAPI service: /health, /context/count, /context, /ingest/*.

`POST /context` is the one backend contract all three surfaces consume -- the VS Code
extension, the MCP server, and the terminal CLI. There is deliberately no second
retrieval implementation anywhere (18 row 32).
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .. import config, observability
from ..ingest import load, sync
from ..ingest.slack_client import SlackError
from ..integrations import github
from ..models import (
    BlameInfo, ContextRequest, ContextResponse, CountResponse, Result,
)
from . import gitctx, graph as graph_mod, query_build, rerank, retrieve


@asynccontextmanager
async def lifespan(_app: FastAPI):
    config.require_api_key()      # 18 row 1: refuse to boot, not to fail first query
    config.require_live_integrations()
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
        if out["index_exists"]:
            out["docs"] = int(es.count(index=config.INDEX)["count"])
        # Which corpus is actually being served: live Slack or a seed export.
        out["source"] = load.read_source(es)
        out["ok"] = bool(out["index_exists"]) and out["embedder_id"] == config.EMBEDDER_ID
    except Exception as exc:
        out["error"] = str(exc)
    return out


# --- ingest ----------------------------------------------------------------------
# The Provenance panel's Backfill button. It drives `ingest.sync`, the same library
# `python -m provenance.ingest` drives, against the same checkpoint file -- so a sync
# started from the panel and one started from a terminal share one notion of what has
# already been indexed, and neither re-pays for the other's work.

# One sync at a time. A second concurrent run would read the same window, re-summarize
# the same threads at full LLM cost, and race the first one to write the checkpoint.
_ingest_lock = asyncio.Lock()


def _checkpoint_path() -> Path:
    return Path(config.INGEST_CHECKPOINT_FILE)


@app.get("/ingest/status")
def ingest_status() -> dict:
    """How far the index is caught up, without touching Slack.

    Cheap on purpose: the panel asks for this on every open, so it reads the
    checkpoint file and a document count and nothing else.
    """
    out: dict = {
        **sync.coverage(_checkpoint_path()),
        "source": "export" if config.USE_MOCK_DATA else "slack",
        "running": _ingest_lock.locked(),
        # What ingest is *scoped* to, which is not the same as what it has indexed.
        # A channel list narrower than the workspace is the one failure this whole
        # feature had no way to show: backfill reported success having looked at one
        # channel of fourteen. `*` resolves at ingest time, so the count is unknown
        # here and the panel says so rather than guessing.
        "scope": "*" if config.SLACK_DISCOVER_CHANNELS else len(config.SLACK_CHANNEL_IDS),
    }
    try:
        es = es_client()
        out["docs"] = (
            int(es.count(index=config.INDEX)["count"])
            if es.indices.exists(index=config.INDEX) else 0
        )
    except Exception as exc:
        out["error"] = str(exc)
    return out


@app.post("/ingest/sync")
async def ingest_sync() -> dict:
    """Index everything posted since the last sync, and nothing else.

    `run_incremental` reads back further than the checkpoint -- a thread can gain a
    reply weeks after its parent -- but only messages newer than the checkpoint count
    as new, and only the units they belong to are rebuilt. The document id is derived
    from the conversation, so a rebuilt unit overwrites in place.

    With no checkpoint at all every message is new, so the first press is a full
    backfill and every press after it is the delta. That is the same code path either
    way; there is no separate "first run".
    """
    if _ingest_lock.locked():
        raise HTTPException(status_code=409, detail="a sync is already running")

    async with _ingest_lock:
        try:
            config.require_api_key()
        except config.MissingAPIKey as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None

        # Refuse to read one corpus into an index built from the other. The checkpoint
        # and the document ids are both per-conversation, so nothing *collides* -- the
        # seed and the live workspace would simply both be in there, answering queries
        # together, while the source stamp and /health still named one of them. A
        # person pressing a button in a panel has no way to see that happen, so it is
        # refused rather than reported (the same stance as `retrieve.check_embedder`).
        intended = "export" if config.USE_MOCK_DATA else "slack"
        conflict = sync.corpus_conflict(es_client(), intended)
        if conflict:
            raise HTTPException(status_code=409, detail=(
                f"{conflict} (USE_MOCK_DATA={str(config.USE_MOCK_DATA).lower()} "
                f"selects {intended!r}.)"
            ))

        log: list[str] = []
        try:
            # Building a live source calls auth.test and one history read per channel.
            source = await asyncio.to_thread(sync.default_source, log.append)
        except sync.SourceUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None

        try:
            result = await sync.run_incremental(
                source.load, _checkpoint_path(), say=log.append,
                source_label=source.label, source_detail=source.detail,
            )
        except SlackError as exc:
            # The read happens before anything is written or checkpointed, so the
            # index and the checkpoint are exactly as they were.
            raise HTTPException(status_code=502, detail=f"Slack read failed: {exc}") from None
        finally:
            source.close()

        # `checkpoint` is the raw per-channel state; `coverage` says the same thing
        # in the shape the panel renders, so only one of them ships.
        counts = {k: v for k, v in result.items() if k != "checkpoint"}
        return {"ok": True, **counts, **sync.coverage(_checkpoint_path()), "log": log}


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
    github.set_repository(req.github_repo)
    timings: dict[str, int] = {}
    supplied_blame = (
        BlameInfo.model_validate(req.precomputed_blame)
        if req.precomputed_blame is not None else None
    )

    # git blame is a subprocess and query_build is an LLM call -- neither needs the
    # other, so they overlap. blame goes to a thread so it does not block the loop.
    # Each gets its own span and its own timing entry; the two overlap in wall clock,
    # so they intentionally do not sum to the request total.
    async def _blame():
        if supplied_blame is not None:
            return supplied_blame
        with _timed(timings, "git.blame"):
            # with_history: the full `git log -L` walk of the range, so a PR the code
            # no longer reflects still reaches the graph -- marked as superseded
            # rather than passed off as a live constraint. The CodeLens path below
            # deliberately does not ask for it.
            return await asyncio.to_thread(
                gitctx.blame, req.repo_root, req.file_path, req.line_start, req.line_end,
                True,
            )

    async def _queries():
        with _timed(timings, "query_build"):
            return await query_build.build_queries(req.code, req.file_path, req.language)

    blame, queries = await asyncio.gather(_blame(), _queries())

    # A shared API cannot read a developer's checkout. When the extension supplied
    # local blame, resolve its commit -> PR joins through the GitHub App instead.
    if req.precomputed_blame:
        for commit in blame.commits:
            if commit.pr_number is None:
                pr = github.lookup_by_sha(commit.sha)
                if pr and isinstance(pr.get("number"), int):
                    commit.pr_number = pr["number"]
        blame.pr_numbers = list(dict.fromkeys(
            c.pr_number for c in blame.commits if c.pr_number is not None
        ))
        blame.pr_number = blame.pr_numbers[0] if blame.pr_numbers else None

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
    github.set_repository(req.github_repo)
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
