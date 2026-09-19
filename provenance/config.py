"""Every tunable constant, in one place. Nothing in the pipeline hardcodes a number
that isn't imported from here."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent

# --- Infrastructure --------------------------------------------------------
ES_URL = os.environ.get("ELASTICSEARCH_URL", "http://localhost:9200")
INDEX = "slack_threads"
META_INDEX = "provenance_meta"          # one document: embedder id + build info
SERVICE_URL = os.environ.get("PROVENANCE_SERVICE_URL", "http://127.0.0.1:8000")
SENTRY_DSN = os.environ.get("SENTRY_DSN", "").strip()

# --- Credentials ------------------------------------------------------------
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()


class MissingAPIKey(RuntimeError):
    pass


def require_api_key() -> None:
    if not OPENAI_API_KEY:
        raise MissingAPIKey(
            "OPENAI_API_KEY is not set.\n"
            "  cp .env.example .env   # then add your key\n"
            "or: export OPENAI_API_KEY=sk-..."
        )


# --- Mock vs live data ---------------------------------------------------------
# The single switch between the demo corpus and a real workspace. True (the default)
# serves every integration from the JSON fixtures under seed/mock_integrations and
# points ingest at a Slack export directory; False runs GitHub, Jira, Sentry and
# Slack for real.
#
# There is deliberately no fixture fallback when this is False. An answer that
# silently substitutes a fabricated PR title or an invented incident is worse than
# one missing a node -- it is the same failure mode the README rejects for a
# fake-LLM mode, and provenance is the one product that cannot afford invented
# evidence. A live lookup that fails contributes nothing instead, which every caller
# already handles (18 row 23).
USE_MOCK_DATA = os.environ.get("USE_MOCK_DATA", "true").strip().lower() not in {
    "0", "false", "no", "off",
}

# --- Integration adapter backends ---------------------------------------------
# Which credentials each adapter needs to answer for real. They are only consulted
# when USE_MOCK_DATA is False; an adapter whose variables are blank then returns
# nothing rather than reaching for a fixture.
#
# NB: SENTRY_DSN above is unrelated -- that is where we *send* our own traces.
# SENTRY_API_TOKEN below is what we *read* issues with.
GITHUB_API = os.environ.get("GITHUB_API", "https://api.github.com").rstrip("/")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.environ.get("GITHUB_REPO", "").strip()            # "owner/name"

SENTRY_API = os.environ.get("SENTRY_API", "https://sentry.io/api/0").rstrip("/")
SENTRY_API_TOKEN = os.environ.get("SENTRY_API_TOKEN", "").strip()
SENTRY_ORG = os.environ.get("SENTRY_ORG", "").strip()
SENTRY_PROJECT = os.environ.get("SENTRY_PROJECT", "").strip()

JIRA_BASE_URL = os.environ.get("JIRA_BASE_URL", "").strip().rstrip("/")
JIRA_EMAIL = os.environ.get("JIRA_EMAIL", "").strip()
JIRA_TOKEN = os.environ.get("JIRA_TOKEN", "").strip()


class MissingCredentials(RuntimeError):
    pass


def require_live_integrations() -> None:
    """Refuse to boot a live deployment with no forge behind it (18 row 1).

    Jira and Sentry stay optional: a PR with no ticket and no incident is an ordinary
    outcome the graph already handles. GitHub is not optional, because with
    USE_MOCK_DATA off it is the only thing that can put a title, an author or a merge
    date on the PR chain -- without it every PR node is a bare number.
    """
    if USE_MOCK_DATA or (GITHUB_TOKEN and GITHUB_REPO):
        return
    raise MissingCredentials(
        "USE_MOCK_DATA is false but GITHUB_TOKEN / GITHUB_REPO are not set.\n"
        "  add both to .env, or set USE_MOCK_DATA=true to run on the seed fixtures"
    )

# These run on the request path inside `resolve_graph`, so the timeout is deliberately
# short: a slow tracker should cost the answer one enrichment, not the whole request.
INTEGRATION_TIMEOUT_SECONDS = 4.0
INTEGRATION_CACHE_TTL_SECONDS = 300      # successful lookups
INTEGRATION_FAILURE_COOLDOWN_SECONDS = 60  # don't re-dial an API that just failed

# --- git history ---------------------------------------------------------------
# `git log -L` walks every commit that ever touched the selected lines, not just the
# ones still owning them. It runs on the request path and its cost grows with the
# depth of the file's history, so the walk is capped. 25 is far past any range a
# person selects by hand and still bounds the worst case on a much-edited file.
GIT_HISTORY_MAX_COMMITS = 25

# --- Models ------------------------------------------------------------------
DENSE_MODEL = "text-embedding-3-small"
DENSE_DIM = 1536
SUMMARY_MODEL = "gpt-4o-mini"
HOT_PATH_MODEL = "gpt-4o-mini"     # code -> prose, on the request path
RERANK_MODEL = "gpt-4o-mini"
SYNTHESIS_MODEL = "gpt-4o"
EMBEDDER_ID = f"{DENSE_MODEL}:{DENSE_DIM}"

# --- LLM call resilience ------------------------------------------------------
LLM_MAX_RETRIES = 3
LLM_RETRY_BASE_SECONDS = 1.5       # exponential backoff: 1.5s, 3s, 6s
MAX_CODE_CHARS = 8000              # code sent to the code-to-prose prompt is truncated here

# --- Ingest tuning --------------------------------------------------------
SEGMENT_GAP_SECONDS = 45 * 60
MIN_MESSAGES_PER_UNIT = 3
MAX_MESSAGES_PER_UNIT = 60
TRIGGER_EMOJI = {"bookmark", "pushpin", "memo", "warning", "fire", "rotating_light"}
SUMMARY_CONCURRENCY = 8
UPSERT_BATCH = 64

# --- Retrieval tuning ------------------------------------------------------
EXACT_TIER_CAP = 3   # raised from the spec's 2: when blame resolves multiple merged PRs (a superseding commit), 2 is not enough to keep every structurally-exact thread out of the reranker's discretion
PREFETCH_LIMIT = 30
FUSION_LIMIT = 20
RERANK_INPUT = 20
RERANK_OUTPUT = 5

TIME_SIGMA_SECONDS = 60 * 24 * 3600.0
TIME_WEIGHT_FLOOR = 0.3
AUTHOR_MATCH_BOOST = 1.25
CHANNEL_TIER_WEIGHT = {1: 1.5, 2: 1.0, 3: 0.7}
REACTION_BOOST = 1.2

# ES's dense_vector cosine similarity score is normalized to (1 + cosine) / 2, i.e.
# always in [0, 1] -- this is NOT the same scale Qdrant returns raw cosine on, and it
# is NOT the same scale as an RRF rank-fusion score (which is rank-based and
# meaningless as an absolute number; never threshold on it). NULL_THRESHOLD below is
# a starting point, not a fact -- calibrate it with `python evals/run_eval.py
# --calibrate` (see 17) against your actual ingested corpus before trusting it. The
# calibration procedure: run the eval suite's known-null query and its known-good-hit
# queries, and set the threshold to the midpoint between the best-null score and the
# worst-still-relevant score.
NULL_THRESHOLD = 0.7121  # calibrated via `make calibrate` against the seed corpus: best-null=0.576, worst-hit=0.848

# --- Elasticsearch retriever strategy --------------------------------------
# The native `retriever`/`rrf` combinator requires ES 8.16+ with a license tier that
# includes it (this has moved across ES versions -- verify against whatever cluster
# you actually provision). If it's unavailable, set this False: retrieve.py falls
# back to two separate queries (a `knn` search and a `match` BM25 search) fused with
# Python-side RRF, identical in shape to how the original Qdrant implementation
# worked. Both code paths are implemented in retrieve.py; this flag picks between
# them without touching any caller.
ES_USE_NATIVE_RRF = False   # this deployment runs on a basic ES license, which does not include RRF
ES_USE_NATIVE_FUNCTION_SCORE = True   # if False, retrieve.py applies the same weights in Python instead

# RRF constant used by the Python-side fallback fusion. Standard value.
RRF_K = 60

# --- Seed data ---------------------------------------------------------------
SEED_DIR = REPO_ROOT / "seed"
SEED_SLACK = SEED_DIR / "slack"
SEED_REPO = SEED_DIR / "repo"

# --- Workspace-local state (created inside the *target* repo being analyzed, not
# this repo) ------------------------------------------------------------------
WORKSPACE_STATE_DIR = ".provenance"
CONTEXT_FILE = f"{WORKSPACE_STATE_DIR}/context.md"
INGEST_CHECKPOINT_FILE = f"{WORKSPACE_STATE_DIR}/ingest_checkpoint.json"

# --- Slack export rendering ----------------------------------------------------
# Only used to build permalinks for an export that carries no permalink of its own.
# The live reader (ingest/slack_live.py) overwrites this from `auth.test` at run time.
SLACK_WORKSPACE = os.environ.get("SLACK_WORKSPACE", "acme")

# --- Live Slack (ingest --source slack) ------------------------------------------
# The only value a person has to fill in is SLACK_USER_TOKEN. Team and channel default
# to the workspace this project indexes. Blank token = live Slack is off; it never
# falls back to seed data, because `--source slack` is an explicit request.
SLACK_API = os.environ.get("SLACK_API", "https://slack.com/api").rstrip("/")
SLACK_USER_TOKEN = os.environ.get("SLACK_USER_TOKEN", "").strip()
SLACK_TEAM_ID = os.environ.get("SLACK_TEAM_ID", "T0C34UQUW68").strip()
SLACK_CHANNEL_IDS = [
    c.strip()
    for c in os.environ.get("SLACK_CHANNEL_IDS", "C0C34DY037B").split(",")
    if c.strip()
]
SLACK_CHANNEL_TIER = int(os.environ.get("SLACK_CHANNEL_TIER", "2"))
# Per-channel override: "eng-incidents:1,eng-payments:1,social:3". A live workspace
# has the same spread of signal and noise the seed corpus encoded in
# channel_tiers.json, and one flat tier throws that signal away -- w_channel is
# 1.5 / 1.0 / 0.7, so an incident channel and a watercooler channel rank the same
# without this. Names are matched without the leading '#'.
SLACK_CHANNEL_TIERS = {
    name.strip().lstrip("#"): int(tier)
    for name, _, tier in (
        pair.partition(":")
        for pair in os.environ.get("SLACK_CHANNEL_TIERS", "").split(",")
    )
    if name.strip() and tier.strip().isdigit()
}


def slack_tier_for(channel_name: str) -> int:
    """Tier for a live Slack channel: explicit override, else the flat default."""
    return SLACK_CHANNEL_TIERS.get(channel_name.lstrip("#"), SLACK_CHANNEL_TIER)
# Incremental sync re-reads threads whose parent is this recent, to catch new replies.
# Older-thread replies are picked up by `--mode reconcile`.
SLACK_THREAD_LOOKBACK_DAYS = int(os.environ.get("SLACK_THREAD_LOOKBACK_DAYS", "14"))
SLACK_TIMEOUT_SECONDS = 30.0
SLACK_MAX_RETRIES = 5
SLACK_PAGE_SIZE = 200
