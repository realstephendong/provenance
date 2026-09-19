"""Every tunable in one place.

The handoff calls for a constants block you tune once against seed data and
then leave alone. Anything you would otherwise be tempted to hardcode in a
pipeline module belongs here.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent

# --- Infrastructure -------------------------------------------------------

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
COLLECTION = "slack_threads"
SERVICE_URL = os.environ.get("HINDSIGHT_SERVICE_URL", "http://127.0.0.1:8000")

# --- Credentials ----------------------------------------------------------
# There is one mode: real calls against a real key. A fake-LLM mode was tried
# and removed -- it let every code path run while quietly making retrieval
# quality meaningless, which is the most expensive kind of green build.

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()


class MissingAPIKey(RuntimeError):
    pass


def require_api_key() -> None:
    """Fail at startup, not on the first query. Call from every entrypoint."""
    if not OPENAI_API_KEY:
        raise MissingAPIKey(
            "OPENAI_API_KEY is not set.\n\n"
            "Hindsight needs it for thread summaries, the code-to-prose "
            "rewrite, reranking and synthesis.\n"
            "  cp .env.example .env   # then add your key\n"
            "or export it for this shell:\n"
            "  export OPENAI_API_KEY=sk-..."
        )


# --- Models ---------------------------------------------------------------
# Trap #3: the embedding dimension lives here and nowhere else. Changing the
# model without dropping the collection produces confusing garbage instead of
# a clean error, so load.py stamps EMBEDDER_ID into the collection and
# retrieve.py refuses to query a collection built by a different embedder.

DENSE_MODEL = "text-embedding-3-small"
DENSE_DIM = 1536
SPARSE_MODEL = "Qdrant/bm25"

# One small wrapper, one constant per job.
SUMMARY_MODEL = "gpt-4o-mini"   # offline, throughput matters more than latency
HOT_PATH_MODEL = "gpt-4o-mini"  # code->prose, on the hot path
RERANK_MODEL = "gpt-4o-mini"
SYNTHESIS_MODEL = "gpt-4o"      # one call, quality shows on stage

EMBEDDER_ID = f"{DENSE_MODEL}:{DENSE_DIM}"

# --- Ingest tuning --------------------------------------------------------

SEGMENT_GAP_SECONDS = 45 * 60  # split top-level runs on a gap this large
MIN_MESSAGES_PER_UNIT = 3      # below this, drop unless bookmarked
MAX_MESSAGES_PER_UNIT = 60     # above this, split at the largest internal gap
TRIGGER_EMOJI = {"bookmark", "pushpin", "memo", "warning", "fire", "rotating_light"}
SUMMARY_CONCURRENCY = 8
UPSERT_BATCH = 64

# --- Retrieval tuning -----------------------------------------------------

EXACT_TIER_CAP = 2       # exact hits cannot crowd out the semantic tier
PREFETCH_LIMIT = 30      # per named vector, before fusion
FUSION_LIMIT = 20        # fused candidates handed to scoring
RERANK_INPUT = 20
RERANK_OUTPUT = 5

TIME_SIGMA_SECONDS = 60 * 24 * 3600.0  # 60 days
TIME_WEIGHT_FLOOR = 0.3
AUTHOR_MATCH_BOOST = 1.25
CHANNEL_TIER_WEIGHT = {1: 1.5, 2: 1.0, 3: 0.7}
REACTION_BOOST = 1.2

# Below this normalized adjusted score, with no exact match, return nothing.
# "No relevant discussions found" reads as credible; five weak links do not.
NULL_THRESHOLD = 0.35

# --- Seed data ------------------------------------------------------------

SEED_DIR = REPO_ROOT / "seed"
SEED_SLACK = SEED_DIR / "slack"
SEED_REPO = SEED_DIR / "repo"
