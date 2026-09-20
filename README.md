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

| Surface | Entry point | What it adds |
|---|---|---|
| VS Code extension | select code → `cmd+alt+w` / `ctrl+alt+w` | evidence sidebar, interactive timeline graph, CodeLens counts, status bar, "Send to agent" |
| MCP server | a coding agent calls `search_team_context` mid-task | the same answer rendered as markdown, straight into a prompt |
| Terminal CLI | `provenance explain webhooks/delivery.py:20-40` | plus `provenance graph <sha>` for a commit's chain alone |

None of the three contains its own retrieval logic. They all consume the identical
`ContextResponse`.

How the pieces fit together, and why: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Quick start: `provenance-start`

One command brings everything up (Windows, macOS and Linux; needs Node 20+, Docker,
Python 3.12+ and the VS Code `code` command):

```bash
cp .env.example .env     # add OPENAI_API_KEY (the only required value)
npm link                 # once: puts `provenance-start` on your PATH
cd /path/to/any/git/repo
provenance-start         # starts everything, then opens VS Code on this folder
```

After `npm link`, open a new terminal so the command is on `PATH` (on Windows it lands in
`%APPDATA%\npm`). If you use a Node version manager (nvm-windows, fnm, Volta), global
commands are per Node version: run `npm link` again after switching.

What it does, in order. Each step checks whether its work is already done, so a re-run
takes seconds:

| Step | Skipped when |
|---|---|
| Configuration: reads `provenance/.env`, fails at once if `OPENAI_API_KEY` is empty | never (instant) |
| Elasticsearch: starts Docker Desktop if needed, `docker compose up -d`, waits for health | ES already answers |
| Python: creates `.venv`, `pip install -r requirements.txt` | `.venv` exists and `requirements.txt` is unchanged |
| Ingest: live Slack if `SLACK_USER_TOKEN` is set, otherwise the seed demo data | `.provenance/ingest_checkpoint.json` exists and the index has documents |
| Extension: `npm install`, compile, package a `.vsix`, `code --install-extension` | source unchanged and already installed |
| API service: `uvicorn` on `127.0.0.1:8000`, detached, waits for `/health` | a healthy Provenance service already answers |
| Open VS Code on the directory you ran the command from | `--no-open` |

```bash
provenance-start --no-open    # everything except opening VS Code
provenance-start --logs       # follow the API service log (Ctrl-C leaves the service running)
provenance-start --reingest   # drop and rebuild the index (spends OpenAI tokens)
provenance-start --stop       # stop the service and the Elasticsearch container
provenance-start --help
```

Things to know:

- **The first run spends tokens.** Ingest sends every thread to OpenAI (embeddings and
  summaries), from live Slack when `SLACK_USER_TOKEN` is set, from the seed data otherwise.
  It happens once; later runs skip it while the ingest checkpoint exists and the index has
  documents. A failed live-Slack ingest stops the launcher; it never falls back to seed
  data. To use the seed data, leave `SLACK_USER_TOKEN` blank.
- **Adding a Slack token after a seed ingest replaces the index.** The launcher records
  which source built the index in `.run/state.json`, so seed threads are never left
  sitting under a live workspace. Conversely, it never drops an index that has documents
  unless the checkpoint says it built them.
- **New Slack messages are the Slack bot's job, not the launcher's.** `provenance-start`
  only backfills once. The bot writes through the `provenance.ingest` pipeline into the
  `slack_threads` index using the current embedder (`text-embedding-3-small:1536`);
  `/health` fails on an embedder mismatch. The service reads the index live, so new
  documents show up without a restart.
- **Only `provenance/.env` (and real environment variables) are read.** A `.env` in the
  folder you run the command from is ignored: it belongs to the repo you're inspecting.
  Environment variables win over `.env`, like `load_dotenv()`. A Slack token pasted into
  `OPENAI_API_KEY` is caught at the configuration step, before anything is installed, and
  the error never echoes the value.
- **Changed `.env` or pulled new code?** A running service keeps the old settings. The
  launcher warns when `.env` is newer than the running service; run
  `provenance-start --stop`, then `provenance-start`.
- **One launcher at a time.** A lock file in `.run/` stops two runs racing on venv
  creation, ingest or the service.
- **Files it writes** (all under `.run/`, gitignored): `service.log`, `service.pid`,
  `launcher.log` (everything the launcher printed) and `state.json` (hashes that let it
  skip work).
- **Overrides:** `PROVENANCE_PYTHON` (interpreter), `PROVENANCE_CODE_BIN` (path to the
  `code` launcher, e.g. for Insiders/VSCodium), `PROVENANCE_SERVICE_URL` (port; the
  extension's `provenance.serviceUrl` must match).
- **Tests:** `npm test` runs the launcher's unit tests — dotenv parsing and precedence,
  content hashing, the process and HTTP helpers, the pipeline runner's skip/fail
  reporting, and every branch of the ingest step's "should I re-ingest, and am I allowed
  to drop the index?" decision.

### Manual steps (macOS/Linux with `make`)

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
python scripts/demo_request.py     # one canned request, rendered (--raw for the JSON)
```

For the extension: `make extension`, then open `extension/` in VS Code and press F5.

Every target:

| Target | What it does |
|---|---|
| `make install` | `.venv` + `requirements.txt` |
| `make es` | Elasticsearch via docker compose, waits for health |
| `make seed` | regenerate `seed/repo` and `seed/slack` |
| `make ingest` / `ingest-incremental` / `reconcile` | the three modes, against the seed export |
| `make slack-check` | can my token read the channel? |
| `make ingest-slack` / `ingest-slack-incremental` / `reconcile-slack` | the same three modes, against live Slack |
| `make slackbot` | the on-demand Slack bot (long-running) |
| `make serve` | `uvicorn` with `--reload` on :8000 |
| `make mcp` | the MCP stdio server |
| `make eval` / `make calibrate` | the eval suite; a `NULL_THRESHOLD` suggestion |
| `make extension` | `npm install` + `tsc` in `extension/` |
| `make demo` | `es` + `seed` + `ingest` |
| `make clean` | drop generated seed data, the ES volume, `extension/out` |

### Requirements

- **Python 3.12+**, **Node.js 20+**, **Docker** (Elasticsearch only).
- **`OPENAI_API_KEY` is required.** Every entrypoint refuses to boot without it.
  There is deliberately no offline or fake-LLM mode: it produces plausible-looking
  output with meaningless content, which silently corrupts every downstream quality
  signal. If you need to work without network access, the parts that make zero LLM
  calls are `service/gitctx.py`, the exact retrieval tier, `service/graph.py`, and
  the extension and CLI rendering layers.
- **`USE_MOCK_DATA` defaults to `true`**, which is what makes the quick start above
  need no credentials at all. See [Mock or real](#mock-or-real).
- **`SENTRY_DSN` is optional.** Unset, `observability.py` no-ops every call.
- **Live shared Slack ingest uses `SLACK_BOT_TOKEN`** and its public-channel
  allowlist. The seed demo needs no Slack account. Browser OAuth is only for the
  private, on-device index; it never gives the shared service your personal token.

---

## How a request works

```
POST /context
  ├─ git.blame      ─┐  concurrent   + `git log -L`: the range's full history
  ├─ query_build    ─┘  code → engineering prose (LLM) + symbols (regex)
  ├─ retrieve          exact tier (PR/SHA/path/basename/symbol) + semantic (kNN ⊕ BM25, RRF)
  │                    └─ null threshold: short-circuit before spending rerank tokens
  ├─ rerank            LLM relevance pass — exact hits rescued unconditionally
  ├─ synthesis         cited answer + conflict/supersede detection (one JSON call)
  └─ resolve_graph     deterministic entity resolution, no LLM
                       └─ fixture-backed by default; live mode dials the trackers here
```

Those six names are also the Sentry span names, so the trace matches the diagram, and
they come back per request in `timing_ms`.

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
   Five strengths, in order: PR, commit SHA, repo-relative path, filename, identifier.
   The last two exist because uncommitted code has no SHA and no PR, so they are the
   only structural join available while you are still writing it.
   This also extends backwards in time: `git log -L` walks every commit that ever
   touched the selected lines, not just the ones still owning them, so the PR behind a
   value that has since been replaced is on the map too — carrying a `SUPERSEDES` edge,
   so its thread reads as settled history rather than as a live constraint.
4. **The system says "no relevant context" rather than fabricate one.** Enforced
   independently at three layers: the retrieval null threshold, the rerank pass, and
   the synthesis prompt.

### Resolving a commit to its PR

The join is only "exact" if a commit can actually be tied to a pull request, and no
single convention covers every repository. `gitctx.sha_to_pr` tries three, in order:

1. the squash-merge subject convention (`… (#4821)`);
2. merge-commit ancestry (`Merge pull request #4821 …`), walking `sha..HEAD`. A commit
   reachable from a merge's *first* parent was already on main before that PR branched,
   so it is rejected — without that guard a repository's initial commit is attributed to
   whichever PR merged first, and the history walk surfaces exactly those old commits;
3. the GitHub adapter's sha→PR endpoint, which catches rebase-merged commits that keep
   no PR marker and that no merge commit is an ancestor of.

The first two read only local git: no network, no credentials. The third is skipped
entirely on the `git log -L` history walk, so a deep history never becomes one network
round-trip per commit. Plenty of commits genuinely have no PR, and that is a normal
answer, not a failure.

---

## The HTTP API

The service binds `127.0.0.1` and has no authentication — it is a local tool, and that
is a deliberate non-goal.

| Endpoint | Purpose |
|---|---|
| `POST /context` | the whole pipeline: synthesis, evidence, blame, graph, conflicts, `timing_ms` |
| `POST /context/count` | retrieval only — no rerank, no synthesis, no graph, and no code-to-prose LLM call; it queries on the extracted symbols instead. Answers `{count, has_exact}`, so ambient discovery costs one embedding rather than a chat completion plus an embedding. |
| `GET /health` | `ok`, the ES URL, whether the index exists and how many documents it holds, the embedder stamp against the expected one, and `source` — whether the index was built from live Slack or a seed export, so "am I querying the real workspace?" is answerable without eyeballing permalinks. |

Both POST bodies are the same shape: `code`, `file_path` (repo-relative), `repo_root`,
`line_start`, `line_end` (1-indexed, inclusive) and an optional `language`. The full
contract lives in [`provenance/models.py`](provenance/models.py) and is mirrored in
[`extension/src/types.ts`](extension/src/types.ts); Python is the source of truth.

Recoverable failures come back as a populated `message` (user-facing) plus `error`
(diagnostic) rather than as a 500 — an unreachable Elasticsearch and an embedder
mismatch each get their own wording and their own fix.

---

## In the editor

The extension is one webview in the activity bar plus three ambient affordances. It
holds no retrieval logic — it POSTs and renders.

**Commands**

| Command | How it is reached |
|---|---|
| Provenance: Explain Selected Code | `ctrl+alt+w` / `cmd+alt+w`, or the editor context menu |
| Provenance: Re-explain Selected Code (bypass cache) | command palette |
| Provenance: Open Timeline in Editor | command palette, or the expand button on the graph |
| Provenance: Explain Range | invoked by CodeLens, not by hand |

**The sidebar** shows the synthesis with `[1]`/`[2]` citations that scroll to the
matching evidence card; the evidence list (channel, date, participants, why it matched,
permalink); any conflict or supersede pairs; the resolved graph; and the per-stage
timings. A breadcrumb of recent selections sits at the top. Explanations are cached by
*selection identity* — the same lines **and** the same bytes — so revisiting a range is
instant rather than re-paying query build, rerank and synthesis; `refresh` forces a
re-run.

**The graph** renders as a vertical dated timeline rather than a layered DAG: in a
~340px column a DAG scatters into unrelated boxes, whereas the evidence is inherently
chronological. Person and Ticket nodes are attributes of an event, not events, so they
render as chips inside their parent's card. The real edges are routed as orthogonal
connectors in the right gutter, unlabelled until you hover. Edges proven by git or by an
identity match are solid (`confidence: "exact"`); the conflict and supersede edges the
synthesis flagged are dashed (`llm-flagged`). Pan, zoom, hover-to-trace and
click-for-details all work, and "Open Timeline in Editor" reopens the same graph
full-width with wider cards, more lanes and labels always on.

**CodeLens** puts an "N discussions · exact match" lens above each top-level
declaration, via `/context/count`. On by default (`provenance.codeLens`), cached per
document, invalidated on edit, and it never surfaces an error in the gutter — a failed
probe simply shows nothing.

**The status bar** does the same for whatever is selected, after a 700ms idle debounce,
and clicking it explains the selection.

**Send to agent** copies the findings as markdown to the clipboard and appends them to
`.provenance/context.md` in the workspace — the hand-off for a coding agent with no MCP
connection.

**Settings:** `provenance.serviceUrl` (default `http://127.0.0.1:8000`; must match
`PROVENANCE_SERVICE_URL` if you moved the port) and `provenance.codeLens`.

### The MCP server

```bash
make mcp                                # or: python -m provenance.mcp_server.server
```

An MCP stdio server exposing one tool, `search_team_context`, which takes the same
selection fields and returns the `ContextResponse` rendered as markdown — agents read
prose better than JSON, and it pastes straight into a prompt. Point any MCP client at
that command. If the service is unreachable the tool returns "Provenance is unavailable
… proceed without team context" rather than failing the agent's turn.

### The CLI

```bash
pip install -e .                                    # puts `provenance` on PATH
provenance explain webhooks/delivery.py:20-40       # --repo <path>, --json
provenance graph 130e156                            # one commit's chain
```

`explain` POSTs to the same service and renders the response: the origin line, the
synthesis, the evidence, the conflicts, and an indented walk of the graph (`-EDGE->` for
proven edges, `~EDGE~>` for inferred ones). `graph` is local and deterministic — it
resolves the SHA to its PR and draws that chain, with no retrieval and no LLM call.

---

## Mock or real

One flag decides whether the whole system runs on the seed corpus or on your actual
systems:

```bash
USE_MOCK_DATA=true    # default: fixture PRs/tickets/incidents, Slack from an export
USE_MOCK_DATA=false   # GitHub, Jira and Sentry for real; ingest defaults to live Slack
```

| | `true` | `false` |
|---|---|---|
| GitHub / Jira / Sentry | `seed/mock_integrations/*.json` | their live APIs |
| `python -m provenance.ingest` default source | `--source export` | `--source slack` |
| [The Slack bot](#the-slack-bot) | always live Slack -- it indexes a real conversation either way | |
| Credentials needed | none beyond `OPENAI_API_KEY` | `GITHUB_TOKEN` + `GITHUB_REPO`; Jira and Sentry optional |

**The two backends never mix.** With the flag off there is no fixture fallback: an
adapter with no credentials, or one whose API is down, contributes *no node* rather
than a fabricated one. Answering a real repository's PR with a seed fixture's title
would invent the one thing this tool exists to establish — and a PR with no ticket
and no incident is an outcome the graph already draws correctly.

Jira and Sentry stay optional because of that. GitHub does not: with it unset the
service refuses to boot, since nothing else can put a title, an author or a merge date
on the PR chain, and every PR node would render as a bare number.

Live lookups run inside `resolve_graph`, on the request path, so they are kept cheap and
failure-tolerant: a 4s timeout, a 5-minute cache on successes, and a 60-second per-host
cooldown after a failure so a dead API is not re-dialled once per node on every request.
A 404 is a legitimate miss (that PR has no ticket), not an outage, and does not trip the
cooldown. Nothing in that layer raises.

### Connecting Sentry

Two different things share the name, and they are unrelated:

| | What it is | Variable |
|---|---|---|
| **Sending** | Provenance's own traces — the six span names in the diagram above | `SENTRY_DSN` |
| **Reading** | the incidents the analysed repo's code caused, which become graph nodes | `SENTRY_API_TOKEN`, `SENTRY_ORG`, `SENTRY_PROJECT` |

Reading is the interesting one, because "which incidents did PR #4821 cause" is not a
relation Sentry models. The fixtures fake it with a `pr_number` field. Live, it is
resolved through the one identifier GitHub and Sentry already share:

```
PR #4821  --GitHub-->  merge_commit_sha  --Sentry-->  firstRelease:<sha>
```

`firstRelease:` and not `release:`, on purpose: `release:` returns every issue *seen* in
a release, so a long-lived error would be inherited by every PR since it first appeared.
The query also sets an empty `statsPeriod`, overriding Sentry's 14-day default — the
whole point of asking is that the PR is older than anyone's memory of it — and overrides
the implicit `is:unresolved`, because a resolved incident is still the reason the code
looks the way it does.

All of that works only if the repository being analysed names its Sentry releases after
the merge commit they shipped. [`getsentry/action-release`](https://github.com/getsentry/action-release)
does by default, so the requirement is one workflow in *that* repo, not in this one:

```yaml
- uses: actions/checkout@v4
  with: { fetch-depth: 0 }        # action-release needs history; depth 1 fails
- uses: getsentry/action-release@v3
  env:
    SENTRY_AUTH_TOKEN: ${{ secrets.SENTRY_AUTH_TOKEN }}
    SENTRY_ORG: ${{ secrets.SENTRY_ORG }}
    SENTRY_PROJECT: ${{ secrets.SENTRY_PROJECT }}
  with:
    set_commits: auto             # attaches the PR's commits to the release
```

A complete, working version of that workflow — plus the SDK wiring it needs — lives in
[Awais-H/provenance-test](https://github.com/Awais-H/provenance-test), the repo this
demo analyses in live mode.

Then here: `SENTRY_API_TOKEN` (scopes `event:read`, `org:read`), `SENTRY_ORG`,
`SENTRY_PROJECT`, and `GITHUB_TOKEN` / `GITHUB_REPO` — GitHub is part of the chain, so
incidents need it even though Sentry is nominally independent.

Version releases some other way (semver, a build number) and this finds *nothing*
rather than something wrong. The one function to change is `_release_for_pr` in
`provenance/integrations/sentry_issues.py`.

### Connecting Jira

`JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_TOKEN`. The same caveat as Sentry applies, and it
is stated at the call site rather than hidden: PR-to-ticket is not a plain field in
Jira. The proper source is the dev-status API, which is undocumented, needs the
GitHub-for-Jira app installed, and keys on Jira's internal issue id rather than the key
— so the default live implementation is a JQL text search for the PR number, and is a
**heuristic**. Lookup by ticket key is exact on both backends.

`lookup_by_pr` returns a *list* on both the ticket and the incident adapters: nothing in
the real world guarantees a 1:1 mapping, and a PR can close two tickets or touch two
incidents.

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
messages to *affected* ones — the whole thread, or the whole burst, computed to a
fixpoint so a chain of new messages pulls in everything it reaches — and rebuilds those
completely. Cost stays proportional to affected threads, not corpus size, and the
deterministic document id makes the re-index a transparent overwrite.

`--mode reconcile` classifies every unit as `missing` / `changed` / `stale` by content
hash, reprocesses the first two, and deletes the third. It stands in for "webhooks got
missed" in a system with no webhooks to miss, and it is what catches edits, deletions,
replies to old threads and newly added channels. Its known scaling limit is stated
rather than solved: it re-segments the entire source on every run.

Each mode reads its source **before** it writes, deletes or checkpoints anything, so a
failed read (a Slack outage, a rate limit) leaves the index as it was.

Segmentation is four fixed, deliberately un-ML rules: anything with a `thread_ts` groups
by thread; what is left splits into bursts on a gap over `SEGMENT_GAP_SECONDS` (45 min);
units over `MAX_MESSAGES_PER_UNIT` (60) split recursively at their widest internal gap;
units under `MIN_MESSAGES_PER_UNIT` (3) are dropped unless a trigger reaction says a
human flagged them. A unit's id derives from its first message's timestamp, which is
what makes every mode idempotent across runs.

Ingest has two sources: a static export directory (`--source export`, the seed demo
above) and the live Slack channel (`--source slack`, next section). Both feed the same
pipeline, and which one is the default follows `USE_MOCK_DATA`. Shared Slack reads use
the installed workspace bot; browser OAuth is reserved for private local indexing.
pipeline — and the same per-message filtering and text cleaning — so they index
identical text. Which one is the default follows `USE_MOCK_DATA`. Still **not** built:
an OAuth login flow and an Events API webhook receiver — you paste a token, and
`--mode incremental` / `--mode reconcile` are the sync mechanism.

---

## Connecting to Slack

Shared backfill indexes approved **public** Slack channels into the shared Elasticsearch
index using the workspace bot, not a developer's personal Slack credential. The bot
allowlist is the policy boundary: only channels in `SLACK_BOT_CHANNEL_IDS` may enter
the shared index.

**One-time setup**

1. In workspace `T0C34UQUW68`, go to <https://api.slack.com/apps> → **Create New App** →
   **From a manifest**, and paste [`slack_app_manifest.yml`](slack_app_manifest.yml).
   The manifest declares the bot scopes for shared ingestion. **Keep it an internal
   app — never distribute it.** Since 2025-05-29 Slack limits `conversations.history` and
   `conversations.replies` to 1 request/minute (15 messages each) for distributed
   non-Marketplace apps, which makes a full-history backfill impractical. Internal apps
   keep the normal limits.
2. **Install to Workspace.** If your workspace requires it, an admin has to approve the
   app first.
3. Copy the **Bot User OAuth Token** from *OAuth & Permissions* into `.env`:
   `SLACK_BOT_TOKEN=xoxb-...`
4. Set `SLACK_BOT_CHANNEL_IDS` to the public channel IDs that may be shared, or `*`
   to discover and join every public channel.

**Check access, then ingest**

```bash
make slack-check      # can the workspace bot read the approved public channels?
make es
make ingest-slack     # entire channel history -> Elasticsearch (drops and rebuilds the index)
make serve
```

`make ingest-slack` runs the same check first and stops before any OpenAI spend if the
bot cannot read its allowlist. On Windows without `make`, run the commands directly:

```bash
python -m provenance.ingest.slack_check
python -m provenance.ingest --source slack --mode backfill --recreate
python -m provenance.ingest --source slack --mode incremental      # later, to catch up
python -m provenance.ingest --source slack --mode reconcile        # edits/deletes/old threads
```

`slack-check` tells you which of these is wrong: no bot token, a bad or revoked token,
the wrong workspace, a channel the bot cannot join, or a missing scope.

**Which channels.** `SLACK_BOT_CHANNEL_IDS=*` discovers every public channel and joins
it before reading. A pasted allowlist is safer when the shared retrieval audience is
narrower than the workspace; it prevents a public channel from entering Elasticsearch
by accident. Private channels are excluded from shared backfill even if the bot was
invited to them.
`slack-check` makes four real calls rather than inferring anything from scopes — token
present, token valid and for the right workspace, channel visible, channel readable — so
its answer is right for public and private channels alike. It names which of these is
wrong, and what to do: no token, a bad or revoked token, a token for the wrong
workspace, a private channel you are not a member of, or a missing scope.

**Channel tiers.** A live workspace has the same spread of signal and noise the seed
corpus encodes, and one flat tier throws that away — the channel weight is 1.5 / 1.0 /
0.7, so an incident channel and a watercooler channel would rank the same.
`SLACK_CHANNEL_TIER` sets the default; `SLACK_CHANNEL_TIERS` overrides per channel by
name, e.g. `incidents:1,eng-backend:2,watercooler:3`.

**Keeping it in sync.** `incremental` re-reads the last few days and threads whose parent is
newer than `SLACK_THREAD_LOOKBACK_DAYS` (default 14). A reply to an older thread, an edit,
a deletion, or a channel newly added to `SLACK_BOT_CHANNEL_IDS` is picked up by `reconcile`.

Treat the bot token like a password. `.env` is gitignored, but it may still be synced by
other software. Shared-ingest message text is sent to OpenAI for summarization and
embeddings, then stored in the shared Elasticsearch index.
- `.env` is gitignored, but this folder may live in OneDrive or another synced location,
  which uploads it. Treat the token like a password; revoke it from the Slack app page if
  it leaks.
- The token is only ever sent in an `Authorization` header. It is never logged, and no
  exception message contains it — Slack errors carry the error code only.
- Message text is sent to OpenAI (summaries and embeddings) and stored in your local
  Elasticsearch, which runs with security disabled (`docker-compose.yml`). That is fine
  for your own machine; do not point it at a shared server.

---

## The Slack bot

`make ingest-slack` reads whole channels in a batch. The bot indexes **one conversation
on demand, from inside Slack** — so a discussion that finished two minutes ago can
already be matched against code you are about to write. That is the live loop: talk it
through in Slack, index the thread, write the code, select it, see the thread come back.

It runs over **Socket Mode**, so it needs no public URL and no tunnel — the process
dials out to Slack from your machine and writes straight to your local Elasticsearch.

**Two ways to trigger it**

| | How it knows which conversation | |
| --- | --- | --- |
| **Index in Provenance** (a message's `...` menu) | the payload carries `thread_ts` | one click, unambiguous — use this one |
| `/provenance` | it can't — Slack does **not** put `thread_ts` in a slash-command payload, so it offers the channel's recent conversations as buttons | type, then click |
| `/provenance <message link>` | parses the link | for a conversation further back |

`/provenance help` prints the same summary inside Slack.

Un-threaded conversations work too. `/provenance` re-segments recent history with the
same burst rule the batch ingest uses, so a run of messages straight in the channel is
offered as one conversation and indexed as one unit. The picker reports a thread's real
size and sorts on its latest reply, so the conversation you just finished is at the top.

**Setup** (on top of [Connecting to Slack](#connecting-to-slack))

1. Paste [`slack_app_manifest.yml`](slack_app_manifest.yml) into your app's **App
   Manifest** page and **reinstall**. It adds a bot user, Socket Mode, the
   `/provenance` command and the message shortcut.
2. Copy three values into `.env` — reinstalling reissues the user token, so re-copy
   that one even if you already had it:

   ```bash
   SLACK_USER_TOKEN=xoxp-...   # OAuth & Permissions -> User OAuth Token
   SLACK_BOT_TOKEN=xoxb-...    # OAuth & Permissions -> Bot User OAuth Token
   SLACK_APP_TOKEN=xapp-...    # Basic Information -> App-Level Tokens (connections:write)
   ```

3. `make slackbot`. It verifies the tokens before accepting a single command, and
   prints which workspace permalinks will point at.

**Getting an exact match, not a hopeful one**

The reply tells you what the conversation can be matched on. Retrieval's exact tier
keys on PR numbers, commit SHAs and file paths — and brand-new code has no PR and no
SHA, so **name the file in the conversation**:

> "let's cap the retries at 5 in `webhooks/delivery.py`"

That one path turns the match from semantic-and-hopeful into structural-and-certain.
If nothing structural is found, the bot says so and tells you what to add.

**What it does to the conversation**

Indexing adds a :pushpin: in Slack. That is not decoration: `segment` drops units under
`MIN_MESSAGES_PER_UNIT` (3) unless a trigger reaction says a human flagged it, so
without the pin the next `make ingest-slack` would re-segment the channel, not rebuild
a short conversation, and `--mode reconcile` would delete it as stale. The pin also
earns the thread `REACTION_BOOST` at query time. It is added with *your* user token, so
it reads as yours and needs no bot membership of the channel.

Everything else is shared with the batch path rather than reimplemented. The document
id is a UUID5 of `channel_id/thread_id`, and `thread_id` is the first message's
timestamp — so the bot and a later `make ingest-slack` write **the same `_id`** for the
same conversation. They overwrite each other; neither duplicates. A thread longer than
`MAX_MESSAGES_PER_UNIT` splits into exactly the pieces a batch ingest would produce, all
of them are written, and the reply says so.

**Limits worth knowing**

- Reads use `SLACK_USER_TOKEN`, so the bot sees exactly what you see. The bot token
  only carries command plumbing, and replies go over each interaction's
  `response_url` — it never posts into a channel.
- The picker reads one page of history (`SLACK_BOT_HISTORY_MESSAGES`, 200 messages,
  never paginated). It answers "what was just being talked about", not "search".
- Indexing a conversation in a channel outside `SLACK_CHANNEL_IDS` works, and the reply
  warns you that a full re-ingest won't cover it.
- Slack gives a listener three seconds to acknowledge, and an index costs a summary plus
  an embedding, so every trigger acks immediately and reports back over `response_url`
  once the work is done.

---

## The seed corpus

`make seed` regenerates `seed/repo` (a real git repo with backdated commits) and
`seed/slack` (a Slack export) from `seed/_repo_files.py` and `seed/_slack_data.py`.
Both are gitignored and neither is ever hand-edited — delete and regenerate freely.
It also rewrites `seed/mock_integrations/github_prs.json` so its `commit_shas` carry the
SHAs git actually produced: content-addressed SHAs cannot be forced to fixed values, so
that one field is regenerated while the PR numbers, titles, authors and dates — what
every join actually keys on — stay literal.

The story it encodes is one constant revised four times, each revision driven by an
incident, and each revision *still true* — which is what makes the chain worth
recovering rather than just the latest value:

| When | What |
|---|---|
| 2025-08-14 | `#eng-payments` — settlement batches time out; `SETTLEMENT_TIMEOUT_SECONDS` → 90s |
| 2025-08-15 | PR #3902 merges the 90s timeout |
| 2026-01-12 | `#eng-payments` — Jordan proposes a 5s retry backoff, off a stale runbook number |
| 2026-01-18 | PR #4100 merges: `RETRY_BACKOFF_SECONDS = 5` |
| 2026-01-25 | WEBHOOK-184 fires; ENG-4821 opens |
| 2026-01-28 | `#eng-incidents` — "5s was still inside the failover window … went with 7s (#4821)" |
| 2026-02-11 | PR #4821 merges: `RETRY_BACKOFF_SECONDS = 7` |
| 2026-03-02 | `#eng-incidents` — WEBHOOK-201: a fixed 7s puts every queued delivery back on the wire at once |
| 2026-03-09 | PR #5012 merges: `RETRY_JITTER_SECONDS = 2.5`, floor stays at 7s |
| 2026-03-30 | `#eng-incidents` — WEBHOOK-233: four attempts can pin a worker for ~40s |
| 2026-04-02 | `#eng-incidents` — 7s held through the April failover |
| 2026-04-06 | PR #5233 merges: `MAX_RETRY_WINDOW_SECONDS = 45` |

Selecting `webhooks/delivery.py:20-40` spans all three surviving constants. Blame
resolves #4821, #5012 and #5233; `git log -L` reaches back to #4100, whose 5s the code
no longer reflects — so that PR reaches the graph carrying a `SUPERSEDES` edge, and the
2026-01-12 proposal is reported as superseded by the 2026-01-28 decision rather than as
a live constraint. The fixtures hang ENG-4821 / ENG-5012 / ENG-5233 and
WEBHOOK-184 / -201 / -233 off those PRs.

The corpus also contains three distractors — `search/indexer.py` retry logic in
`#eng-search` (deliberately overlapping vocabulary: retries, backoff, jitter, and a
message saying outright it is a different failure mode), `webhooks/signing.py` secret
rotation in `#eng-payments`, and office chatter in `#eng-general` (tier 3) — plus one
file discussed nowhere at all: `utils/strings.py`, the null case. Without those, the
null-threshold and exact-vs-semantic evals would mean nothing.

Three PR numbers referenced in Slack (#3902, #3455, #4150) are intentionally absent from
the mock fixtures, which exercises the "adapter has no match" path: the adapters return
empty and the graph simply omits the node.

Two details in the corpus are deliberate, not incidental. Message texts are written so
`ingest/extract.py`'s regexes fire on the *conversation unit* rather than on any single
message — a bare `#4821` is only trusted in code-adjacent context, so every thread
naming a PR by number also carries a github.com URL or the literal token "PR" somewhere
in the same unit. And the Slack display names match the git commit author names, which
is what lets `AUTHOR_MATCH_BOOST` actually fire in the demo.

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

Three cases: the retry-backoff chain (expects the original proposal, the decision that
replaced it and the incident follow-up, plus a `CONFLICTS_WITH`/`SUPERSEDES` edge in the
graph), the settlement timeout, and the null case. The suite checks that expected
evidence appears and at what rank, that a synthesis came back, that the null case
returns zero results and a `message`, and that the conflict case produces both a
non-empty `conflicts` list and the matching graph edge.

Expected evidence is pinned to a marker phrase from the **thread's own words**, never
from its summary. A summary is regenerated on every ingest, and a model that writes
"five-second retry" one run writes "5-second retry" the next; that is not hypothetical —
it is how this suite once went red, reporting a missing thread that was in fact ranking
first. Slack text only changes when a person edits it.

`NULL_THRESHOLD` in `config.py` ships as a **starting point, not a fact**. Calibrate
it against your own ingested corpus before trusting it: `make calibrate` prints the
best null score and the worst still-relevant score and suggests their midpoint. It runs
in-process rather than over HTTP, because the absolute dense score it compares against
is an internal retrieval value and is deliberately not part of the `/context` contract.
If the two overlap it says so: no threshold separates them, and the corpus or the
prompts need work before the number can mean anything.

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
  identical output shapes. It ships `False`, because this deployment runs on a basic
  ES licence, which does not include RRF.
- **`ES_USE_NATIVE_FUNCTION_SCORE`** — `True` applies the relevance weights
  server-side; `False` applies the identical weights in Python to the fused score.
  See the note at the top of `service/retrieve.py`: Elasticsearch cannot nest a
  `retriever` inside a `function_score`, nor apply one to a `knn` query, so the
  server-side path weights the lexical channel and the Python path weights both.
  The weights are never applied twice on either path.

**The semantic floor.** RRF is rank-based, so the fused score has no magnitude worth
thresholding — which leaves the dense cosine as the only interpretable relevance number
in the pipeline. It used to be read exactly once, by the null gate, which judges the
*whole request*: one strong hit therefore let every weak one in behind it. A
"print Hello World" selection matched its own thread at 0.768 and an unrelated thread
about wanting to understand decisions at 0.580, and showed both. `SEMANTIC_FLOOR_RATIO`
now also applies it per result, at the weaker of `best × 0.85` and `NULL_THRESHOLD` —
never discarding a hit that would have been reported as relevant had it arrived alone.
Exact-tier hits are exempt; they are asserted, not scored.

**What may enter the exact tier.** A symbol match is asserted evidence that no later
stage may discard, so it has to be distinctive: at least `EXACT_SYMBOL_MIN_CHARS` (4)
characters *and* shaped like an identifier — carrying an underscore, a dot, or an
internal capital after a lowercase. A live corpus's extracted symbols include
`Provenance`, `Elasticsearch` and `Explain`, ordinary words the summarizer capitalised,
and matching code against the word "Elasticsearch" would be unfilterable noise.
`RETRY_BACKOFF_SECONDS`, `print_hello_world`, `settlement.py` and `SlackThread` all
pass; PR and ticket references fail the shape test and are matched by `pr_refs` and
`ticket_refs`, where they belong. Filenames are indexed as bare basenames alongside full
paths, because engineers say "settlement.py" in Slack far more often than
"payments/settlement.py" — at a lower exact strength, since a basename is not unique.

The relevance weights themselves: Gaussian time decay around the commit
(**symmetric on purpose** — a "this broke prod" thread from *after* the commit is
often the most valuable evidence there is, and must not be penalised more than a
pre-commit thread the same distance away), an author-match boost when a blame author
participated in the thread, a channel-tier weight, and a bookmark-reaction boost.

Other constants worth knowing: `GIT_HISTORY_MAX_COMMITS` (25) caps the `git log -L`
walk, which runs on the request path and grows with the file's history;
`INTEGRATION_TIMEOUT_SECONDS`, `INTEGRATION_CACHE_TTL_SECONDS` and
`INTEGRATION_FAILURE_COOLDOWN_SECONDS` bound the live adapters; `LLM_MAX_RETRIES` and
`LLM_RETRY_BASE_SECONDS` govern the jittered backoff on OpenAI 429/5xx (a non-429 4xx is
never retried — it will not succeed); `MAX_CODE_CHARS` truncates a long selection rather
than rejecting it; `EXACT_TIER_CAP` (3) bounds how many asserted hits bypass the
reranker.

Models: `text-embedding-3-small` (1536 dims) for embeddings, `gpt-4o-mini` for
summaries, code-to-prose and rerank, `gpt-4o` for synthesis. The embedder id is stamped
into the index at build time and checked on every request, so vectors from two different
models are never compared.

---

## Layout

```
bin/provenance-start.js   the one-command launcher's entry point
launcher/                 its steps, pipeline runner, probes and unit tests
provenance/
  config.py          every tunable constant
  models.py          the frozen contract shared by all four surfaces
  llm.py             the only module that calls OpenAI
  observability.py   Sentry, no-op when unconfigured
  integrations/      GitHub / tickets / incidents: fixtures or live, one adapter each
  ingest/            Slack -> Elasticsearch: export and live readers, three batch modes
  slackbot/          the on-demand bot: one conversation, indexed from Slack
  service/           the live /context pipeline
  mcp_server/        MCP stdio server
  cli/               terminal surface
extension/src/       VS Code extension: view (sidebar + timeline panel), graph (the SVG
                     timeline renderer), codelens, api, types
seed/                deterministic demo corpus generator + integration fixtures
evals/               the eval harness and its fixed query set
scripts/             one canned request, for checking the service by hand
docs/ARCHITECTURE.md how it all fits together, and where it is fragile
```

External systems beyond git and Slack ship **mocked by default** — but as real adapter
interfaces with fixture data behind them, not special-cased inline logic. Every one
exposes `lookup_by_pr(pr_number)`, and `USE_MOCK_DATA=false` switches all of them to
their live APIs at once. No caller changes either way; see
[Mock or real](#mock-or-real).

## Non-goals

A Slack OAuth login flow and Events API webhooks (live Slack is read with a pasted user
token instead); per-user filtering on a shared index; a channel-approval UI; MCP-client
connectors for the trackers (they are plain REST adapters behind `USE_MOCK_DATA`);
multi-repository
support; auth on the FastAPI service; embedding-based topic-shift segmentation;
an offline/fake-LLM mode; a second retrieval implementation for the terminal or MCP
path; production-scale reconciliation sharding; fuzzy cross-source identity resolution
for `Person` nodes (name-string matching only); tree-sitter symbol extraction (regex
today, behind a deliberately swappable seam).
