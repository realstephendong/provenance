# Hindsight

Highlight a block of code in VSCode and get back the Slack conversations that
explain why it is the way it is.

The architectural idea that makes this more than a RAG demo: **we join git
history to Slack.** `git blame` gives the commit that touched the selected
lines, the commit gives a PR number, and Slack messages mention PR numbers.
That is an exact match, not a similarity score. Semantic search fills the
remaining slots.

## Quick start

```bash
make install          # venv on python3.12 + deps
cp .env.example .env  # then put your OPENAI_API_KEY in it -- required
make qdrant           # Qdrant in Docker on :6333
make seed             # generate the Slack export + backdated git repo
make ingest           # index the corpus
make serve            # FastAPI on :8000
```

Then in another shell:

```bash
python scripts/demo_request.py   # the canonical demo query, as text
python evals/run_eval.py         # 5 fixed queries, pass/fail
```

For the extension: `make extension`, open `extension/` in VSCode, press F5.
The launch config opens `seed/repo` in the dev host. Select lines 23-68 of
`webhooks/delivery.py` and press `cmd+alt+w`.

### An API key is required

`OPENAI_API_KEY` is mandatory. The ingest CLI, the service and the MCP server
each check for it at startup and refuse to run without one, rather than
failing on the first query.

There was briefly a fake-LLM mode for working offline. It was removed on
purpose: it let the whole pipeline go green while the summaries, the rerank
and the synthesis were all placeholder text, which made every eval number a
lie. If you need to work without network access, work on the parts that need
no model — the exact-match tier, `git blame`, PR resolution and the panel all
run without one.

## The demo path

The one sequence that has to work, end to end, offline-safe, under 8 seconds:

1. Open `seed/repo` in VSCode, go to `webhooks/delivery.py`.
2. Select lines 23-68 — the retry loop with `RETRY_BACKOFF_SECONDS = 7` and
   the enterprise-tier special case. Nothing in the code says why either
   exists.
3. `cmd+alt+w`. The panel opens beside the editor.
4. Header: `Last touched by Priya Raman, 2026-02-11, PR #4821 (130e156)`.
5. Synthesis paragraph with `[1]`/`[2]` citations, then result cards.
6. At least one card carries an **Exact match** badge, because that thread
   references PR #4821 — the PR that wrote those exact lines.
7. "Send to agent" copies the context to the clipboard and appends it to
   `.hindsight/context.md`.

The corpus is built backwards from that moment. The story it tells, which no
code comment states: 7 seconds is pinned between a 6.2s merchant load-balancer
failover p99 and the 8s implied by a 40s contractual delivery ceiling. Move it
either way and something breaks.

Second demo beat: run the same query through the MCP server so a coding agent
pulls the context autonomously, mid-task, without anyone clicking anything.

## Layout

```
hindsight/
  config.py          every tunable, in one place
  llm.py             the only module that talks to a provider
  models.py          the frozen /context contract
  ingest/            export reader, segmentation, extraction, summary, embed, load
  service/           gitctx, query_build, retrieve, rerank, FastAPI
  mcp_server/        stdio MCP, one tool, wraps /context
extension/           VSCode extension (thin: selection -> POST -> webview)
seed/                build_seed.py generates the Slack export and the git repo
evals/               5 fixed queries with expected threads
```

## How retrieval works

**Ingest.** Threads group by `thread_ts`; leftover top-level messages split on
a 45-minute gap. Units under 3 messages are dropped unless a trigger emoji
marks them. Regex pulls PR numbers, SHAs, tickets, file paths and symbols. One
LLM call per thread writes the summary that gets dense-embedded; BM25 covers
summary + raw text + symbols.

**Query.** Never embed raw code against Slack prose — different modalities in
one vector space produce thematically adjacent noise. Instead: rewrite the
code into prose with an LLM (dense channel), and extract identifiers (sparse
channel). `git blame` runs concurrently and resolves to PR numbers via two
strategies (squash subject `(#1234)`, then merge-commit ancestry).

**Retrieval.** Tier 1 is a filter-only Qdrant query on `pr_refs`,
`commit_shas` and `file_paths` — no vector involved, capped at 2 so it cannot
crowd out the rest. Tier 2 is a hybrid dense+sparse query fused with RRF.
Scores are then adjusted:

```
score_final = score_fused × w_time × w_author × w_channel × w_react
```

`w_time` is a **two-sided** Gaussian around the commit date, σ = 60 days,
floored at 0.3. Deliberately two-sided: threads *before* the commit explain
intent, threads *after* explain consequences, and the post-hoc "this broke
prod" thread is often the most valuable result. A hard cutoff at commit time
throws those away.

## Deviations from the handoff

Four, all forced or defensive:

1. **`mcp/` is `mcp_server/`.** A local package named `mcp` shadows the `mcp`
   PyPI package and breaks the import.
2. **`FastMCP` is `MCPServer`.** The handoff targets mcp 1.x. On the installed
   2.x that class was renamed. Same decorator, same stdio transport.
3. **Null handling keys on absolute cosine, not the normalized fused score.**
   RRF is rank-based and scale-free: normalizing it makes the top hit always
   1.0, so a threshold on it can never fire. `semantic_hits` takes one extra
   dense-only Qdrant pass to get a real cosine, and `NULL_THRESHOLD = 0.35`
   applies to that.
4. **CodeLens calls `/context/count`, not `/context`.** Retrieval only, no
   rerank and no synthesis. Routing it at `/context` would triple the LLM
   calls for one file open, which is exactly what makes CodeLens feel slow.
   It is still off by default (`hindsight.codeLens`).

## Traps, and what was done about them

| Trap | Status |
|---|---|
| Slack API rate limits | Export path only. The API reader is not built; see the note in `slack_source.py` before building one. |
| Tree-sitter setup | Regex fallback ships behind `extract_symbols(code, language)`. Tree-sitter is a drop-in swap. |
| Embedding dimension mismatch | The collection records its embedder id; `retrieve.check_embedder` refuses to query a collection built by a different one. |
| `git blame` on uncommitted lines | Detected, reported as `blame.uncommitted`, falls back to pure semantic with no time anchor. |
| VSCode dev loop | `node extension/scripts/render_preview.js` renders the real panel HTML to a file, no extension host needed. |
| Tuning by vibes | `evals/run_eval.py` — 5 fixed queries, expected threads, hit positions, under a second. |

## Non-goals

Live Slack OAuth and the channel-approval UI, incremental re-indexing,
reaction webhooks, multi-repo support, auth on the service, embedding-based
topic-shift segmentation, persistence beyond Qdrant.
