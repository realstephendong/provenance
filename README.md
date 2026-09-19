# Provenance

Select a block of code in VS Code and recover the full evidence chain behind it: the
commit that wrote it, the PR that commit belongs to, the Slack threads that discuss
that PR, the ticket that tracked it, the incident it may have caused — and, if the
evidence disagrees with itself, an explicit statement of what conflicts and what
superseded what.

This is not "search Slack for similar words". It is a join across independent systems
(git, Slack, GitHub, a ticket tracker, an error tracker) that becomes **exact**
wherever those systems agree by construction — a commit SHA, a PR number — and falls
back to ranked semantic search only where they don't.

Three surfaces, one backend contract (`POST /context`):

| Surface | Entry point |
|---|---|
| VS Code extension | select code → `cmd+alt+w` / `ctrl+alt+w` |
| MCP server | a coding agent calls `search_team_context` mid-task |
| Terminal CLI | `provenance explain webhooks/delivery.py:20-40` |

None of the three contains its own retrieval logic. They all consume the identical
`ContextResponse`.

---

## Quick start

```bash
make install          # python3.12 venv + deps   (override: make install PYTHON=python3.13)
cp .env.example .env  # then add your OPENAI_API_KEY
make es               # Elasticsearch 8.17 via docker compose
make seed             # generate seed/repo + seed/slack deterministically
make ingest           # Slack export -> Elasticsearch (uses the OpenAI API)
make serve            # FastAPI on :8000
```

Then, in another shell:

```bash
make eval                          # the objective signal — run this after any change
python scripts/demo_request.py     # one canned request, rendered
```

For the extension: `make extension`, then open `extension/` in VS Code and press F5.

### Requirements

- **Python 3.12+**, **Node.js**, **Docker** (Elasticsearch only).
- **`OPENAI_API_KEY` is required.** Every entrypoint refuses to boot without it.
  There is deliberately no offline or fake-LLM mode: it produces plausible-looking
  output with meaningless content, which silently corrupts every downstream quality
  signal. If you need to work without network access, the parts that make zero LLM
  calls are `service/gitctx.py`, the exact retrieval tier, `service/graph.py`, and
  the extension and CLI rendering layers.
- **`SENTRY_DSN` is optional.** Unset, `observability.py` no-ops every call.
- **`SLACK_USER_TOKEN` is only needed for live Slack** — see [Connecting to Slack](#connecting-to-slack).
  The seed demo needs no Slack account.

---

## How a request works

```
POST /context
  ├─ git.blame      ─┐  concurrent
  ├─ query_build    ─┘  code → engineering prose (LLM) + symbols (regex)
  ├─ retrieve          exact tier (PR/SHA/path) + semantic tier (kNN ⊕ BM25, RRF)
  │                    └─ null threshold: short-circuit before spending rerank tokens
  ├─ rerank            LLM relevance pass — exact hits rescued unconditionally
  ├─ synthesis         cited answer + conflict/supersede detection (one JSON call)
  └─ resolve_graph     deterministic entity resolution, no LLM, no network
```

Those six names are also the Sentry span names, so the trace matches the diagram.

### The four ideas that matter

1. **Elasticsearch is the only context/vector store.** It supplies BM25, dense kNN,
   RRF fusion, filtering, and the relevance-weighting math. No second store.
2. **Code is never embedded directly against Slack prose.** Code is rewritten into
   engineering prose by an LLM first, and *that* is embedded. Identifiers are
   extracted separately and carry the lexical channel. Both sides of the comparison
   are rewritten into the same register before they ever meet.
3. **Structural evidence outranks inferred evidence, unconditionally.** If git proves
   a commit belongs to PR #4821 and a Slack thread names PR #4821, that relationship
   is asserted, not scored. If the reranker calls it irrelevant, it is kept anyway.
4. **The system says "no relevant context" rather than fabricate one.** Enforced
   independently at three layers: the retrieval null threshold, the rerank pass, and
   the synthesis prompt.

---

## Ingest modes

```bash
make ingest              # backfill  (--recreate drops and rebuilds the index)
make ingest-incremental  # only what changed since the checkpoint
make reconcile           # drift detection between export and index
```

`--mode incremental` is the one with a real correctness trap in it. Filtering
messages to `ts > checkpoint` and segmenting only those is **wrong**: a reply arriving
today on a three-week-old thread would be segmented in isolation and would overwrite
the real, larger thread with a truncated summary. So the incremental run expands new
messages to *affected* ones — the full thread, or the full burst — and rebuilds those
completely. Cost stays proportional to affected threads, not corpus size, and the
deterministic document id makes the re-index a transparent overwrite.

`--mode reconcile` classifies every unit as `missing` / `changed` / `stale` by content
hash, reprocesses the first two, and deletes the third.

Each mode reads its source **before** it writes, deletes or checkpoints anything, so a
failed read (a Slack outage, a rate limit) leaves the index as it was.

Ingest has two sources: a static export directory (`--source export`, the seed demo
above) and the live Slack channel (`--source slack`, next section). Both feed the same
pipeline. Still **not** built: an OAuth login flow and an Events API webhook receiver —
you paste a token, and `--mode incremental` / `--mode reconcile` are the sync mechanism.

---

## Connecting to Slack

Each person indexes the channel **with their own Slack login**, into their **own local**
Elasticsearch. So what you can index is exactly what Slack lets you see, and there is no
shared index or service-side auth to configure. The channel is `C0C34DY037B` in workspace
`T0C34UQUW68` (both pre-filled in `.env.example`).

Slack's API does not accept a username and password, so your "login" is a **User OAuth
Token** (`xoxp-…`). It is the only value you fill in.

**One-time setup**

1. In workspace `T0C34UQUW68`, go to <https://api.slack.com/apps> → **Create New App** →
   **From a manifest**, and paste [`slack_app_manifest.yml`](slack_app_manifest.yml).
   It asks only for read-only *user* scopes. **Keep it an internal app — never
   distribute it.** Since 2025-05-29 Slack limits `conversations.history` and
   `conversations.replies` to 1 request/minute (15 messages each) for distributed
   non-Marketplace apps, which makes a full-history backfill impractical. Internal apps
   keep the normal limits.
2. **Install to Workspace.** If your workspace requires it, an admin has to approve the
   app first.
3. Copy the **User OAuth Token** from *OAuth & Permissions* into `.env`:
   `SLACK_USER_TOKEN=xoxp-...`

**Check access, then ingest**

```bash
make slack-check      # do I have access to the channel? prints ACCESS OK or NO ACCESS + why
make es
make ingest-slack     # entire channel history -> Elasticsearch (drops and rebuilds the index)
make serve
```

`make ingest-slack` runs the same check first and stops with the verdict if you don't have
access, before any OpenAI spend. On Windows without `make`, run the commands directly:

```bash
python -m provenance.ingest.slack_check
python -m provenance.ingest --source slack --mode backfill --recreate
python -m provenance.ingest --source slack --mode incremental      # later, to catch up
python -m provenance.ingest --source slack --mode reconcile        # edits/deletes/old threads
```

`slack-check` tells you which of these is wrong: no token, a bad or revoked token, a token
for the wrong workspace, a private channel you are not a member of, or a missing scope.

**Keeping it in sync.** `incremental` re-reads the last few days and threads whose parent is
newer than `SLACK_THREAD_LOOKBACK_DAYS` (default 14). A reply to an older thread, an edit,
a deletion, or a channel newly added to `SLACK_CHANNEL_IDS` is picked up by `reconcile`.

**Things to know before you paste a token**

- `.env` is gitignored, but this folder may live in OneDrive or another synced location,
  which uploads it. Treat the token like a password; revoke it from the Slack app page if
  it leaks.
- Message text is sent to OpenAI (summaries and embeddings) and stored in your local
  Elasticsearch, which runs with security disabled (`docker-compose.yml`). That is fine
  for your own machine; do not point it at a shared server.

---

## The seed corpus

`make seed` regenerates `seed/repo` (a real git repo with backdated commits) and
`seed/slack` (a Slack export) from `seed/_repo_files.py` and `seed/_slack_data.py`.
Both are gitignored and neither is ever hand-edited — delete and regenerate freely.

The story it encodes:

| When | What |
|---|---|
| 2026-01-12 | `#eng-payments` — Jordan proposes a 5s retry backoff |
| 2026-01-18 | PR #4100 merges: `RETRY_BACKOFF_SECONDS = 5` |
| 2026-01-25 | WEBHOOK-184 fires; ENG-4821 opens |
| 2026-01-28 | `#eng-incidents` — "5s was still inside the failover window … went with 7s (#4821)" |
| 2026-02-11 | PR #4821 merges: `RETRY_BACKOFF_SECONDS = 7` |
| 2026-04-02 | `#eng-incidents` — 7s held through the April failover |

Selecting `webhooks/delivery.py:20-40` should recover all of it, *and* report that the
2026-01-12 proposal was superseded by the 2026-01-28 decision.

The corpus also contains two distractors with deliberately overlapping vocabulary
(`search/indexer.py` retry logic, `webhooks/signing.py` secret rotation) and one file
with no Slack evidence at all — `utils/strings.py`, the null case. Without those, the
null-threshold and exact-vs-semantic evals would mean nothing.

Two PR numbers referenced in Slack (#3902, #3455) are intentionally absent from the
mock fixtures, which exercises the "adapter has no match" path: the adapters return
empty, and the graph simply omits the node.

```bash
python seed/build_seed.py --append           # a late reply, to test incremental
python seed/build_seed.py --with-malformed   # an unreadable day file, to test resilience
```

---

## Evals

```bash
make eval        # the full suite against a running service
make calibrate   # suggest a NULL_THRESHOLD for your actual corpus
```

The suite checks that expected evidence appears and at what rank, that the null case
returns zero results and a message, and that the conflict case produces both a
non-empty `conflicts` list and a `CONFLICTS_WITH`/`SUPERSEDES` edge in the graph.

`NULL_THRESHOLD` in `config.py` ships as a **starting point, not a fact**. Calibrate
it against your own ingested corpus before trusting it: `make calibrate` prints the
best null score and the worst still-relevant score and suggests their midpoint.

> Any change to a prompt, a weight in `config.py`, or the Elasticsearch query shape
> must be verified against this harness before being trusted. It is the only
> objective signal in the project.

---

## Configuration

Everything tunable lives in `provenance/config.py`. Two flags pick between
implementation strategies without touching any caller:

- **`ES_USE_NATIVE_RRF`** — `True` uses Elasticsearch's native `retriever`/`rrf`
  combinator (8.16+, licence-gated). `False` issues a `knn` search and a `match`
  search separately and fuses them with Python-side RRF (`k=60`). Both produce
  identical output shapes.
- **`ES_USE_NATIVE_FUNCTION_SCORE`** — `True` applies the relevance weights
  server-side; `False` applies the identical weights in Python to the fused score.
  See the note at the top of `service/retrieve.py`: Elasticsearch cannot nest a
  `retriever` inside a `function_score`, nor apply one to a `knn` query, so the
  server-side path weights the lexical channel and the Python path weights both.
  The weights are never applied twice on either path.

The relevance weights themselves: Gaussian time decay around the commit
(**symmetric on purpose** — a "this broke prod" thread from *after* the commit is
often the most valuable evidence there is, and must not be penalised more than a
pre-commit thread the same distance away), an author-match boost when a blame author
participated in the thread, a channel-tier weight, and a bookmark-reaction boost.

---

## Layout

```
provenance/
  config.py          every tunable constant
  models.py          the frozen contract shared by all four surfaces
  llm.py             the only module that calls OpenAI
  observability.py   Sentry, no-op when unconfigured
  integrations/      mocked GitHub / tickets / incidents, as real adapter interfaces
  ingest/            export -> Elasticsearch, three modes
  service/           the live /context pipeline
  mcp_server/        MCP stdio server
  cli/               terminal surface
extension/           VS Code extension (TypeScript)
seed/                deterministic demo corpus generator
evals/               the eval harness
```

External systems beyond git and Slack are mocked — but as **real adapter interfaces
with fake data behind them**, not special-cased inline logic. Every one exposes
`lookup_by_pr(pr_number)`. Swapping in a live API means replacing one function body;
no caller changes.

## Non-goals

A Slack OAuth login flow and Events API webhooks (live Slack is read with a pasted user
token instead); per-user filtering on a shared index; a channel-approval UI; real
GitHub/ticket/error-tracker APIs (mocked by design, not by omission); multi-repository
support; auth on the FastAPI service; embedding-based topic-shift segmentation;
an offline/fake-LLM mode; a second retrieval implementation for the terminal or MCP
path; production-scale reconciliation sharding; fuzzy cross-source identity resolution
for `Person` nodes (name-string matching only).
