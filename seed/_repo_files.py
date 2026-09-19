"""File contents for the seed repo, one entry per revision.

Built backwards from the demo moment: a developer selects the retry loop in
webhooks/delivery.py, sees RETRY_BACKOFF_SECONDS = 7 and an enterprise-tier
special case, and has no idea why either is there.

Deliberately, no comment in this code explains the 7. That is the whole point
of the product -- the reason lives in Slack.
"""

README = """# acme-platform

Payments and merchant integration services.

- `webhooks/` outbound webhook delivery
- `payments/` settlement and payout
- `search/` merchant-facing search indexing
"""

SIGNING = '''"""Webhook payload signing."""

import hashlib
import hmac
import json
import time


def sign_payload(payload: dict, secret: str) -> bytes:
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ts = str(int(time.time())).encode()
    mac = hmac.new(secret.encode(), ts + b"." + body, hashlib.sha256)
    return body + b"\\n" + mac.hexdigest().encode()
'''

# --- webhooks/delivery.py, v1: the original 30-second flat backoff ---------

DELIVERY_V1 = '''"""Outbound webhook delivery."""

import logging

from .signing import sign_payload

log = logging.getLogger(__name__)

RETRY_BACKOFF_SECONDS = 30
MAX_ATTEMPTS = 5


class TransientError(Exception):
    pass


def deliver(endpoint, payload, merchant, session, sleep):
    """Deliver one webhook, retrying transient failures."""
    body = sign_payload(payload, endpoint.secret)
    attempt = 0
    while attempt < MAX_ATTEMPTS:
        attempt += 1
        try:
            resp = session.post(endpoint.url, data=body, timeout=10)
        except TransientError:
            log.warning("transient failure for %s", endpoint.url)
            sleep(RETRY_BACKOFF_SECONDS)
            continue
        if resp.status_code < 500:
            return resp
        sleep(RETRY_BACKOFF_SECONDS)
    raise TransientError(endpoint.url)
'''

# --- webhooks/delivery.py, v2: PR #4821, the commit the demo lands on ------
# This rewrite owns the retry loop, so `git blame -L` over the selection
# resolves to it, which resolves to #4821, which is what Slack references.

DELIVERY_V2 = '''"""Outbound webhook delivery."""

import logging

from .signing import sign_payload

log = logging.getLogger(__name__)

RETRY_BACKOFF_SECONDS = 7
MAX_ATTEMPTS = 5
ENTERPRISE_TIER = "enterprise"
RETRYABLE_STATUS = {429, 502, 503, 504}


class TransientError(Exception):
    pass


class PermanentError(Exception):
    pass


def _backoff_for(merchant, attempt: int) -> int:
    if merchant.tier == ENTERPRISE_TIER:
        return RETRY_BACKOFF_SECONDS * attempt
    return RETRY_BACKOFF_SECONDS


def deliver(endpoint, payload, merchant, session, sleep):
    """Deliver one webhook, retrying transient failures.

    Returns the first non-retryable response. Raises TransientError if every
    attempt was retryable, PermanentError on a 4xx that is not 429.
    """
    body = sign_payload(payload, endpoint.secret)
    attempt = 0
    last_status = None

    while attempt < MAX_ATTEMPTS:
        attempt += 1
        delay = _backoff_for(merchant, attempt)

        try:
            resp = session.post(endpoint.url, data=body, timeout=10)
        except TransientError:
            log.warning(
                "transient failure endpoint=%s merchant=%s attempt=%d",
                endpoint.url, merchant.id, attempt,
            )
            sleep(delay)
            continue

        last_status = resp.status_code

        if resp.status_code in RETRYABLE_STATUS:
            log.info(
                "retryable status=%d endpoint=%s attempt=%d delay=%ds",
                resp.status_code, endpoint.url, attempt, delay,
            )
            sleep(delay)
            continue

        if 400 <= resp.status_code < 500:
            raise PermanentError(f"{endpoint.url} returned {resp.status_code}")

        return resp

    raise TransientError(f"{endpoint.url} exhausted {MAX_ATTEMPTS} attempts, last={last_status}")
'''

# --- decoy 1: a different constant, same vocabulary -----------------------

SETTLEMENT = '''"""Settlement batch runner."""

import logging

log = logging.getLogger(__name__)

SETTLEMENT_TIMEOUT_SECONDS = 45
BATCH_SIZE = 500


def run_batch(acquirer, rows, clock):
    """Settle a batch against the acquirer, one request per BATCH_SIZE rows."""
    deadline = clock.now() + SETTLEMENT_TIMEOUT_SECONDS
    settled = []
    for i in range(0, len(rows), BATCH_SIZE):
        if clock.now() > deadline:
            log.error("settlement batch timed out after %ds", SETTLEMENT_TIMEOUT_SECONDS)
            break
        settled.extend(acquirer.settle(rows[i : i + BATCH_SIZE]))
    return settled
'''

# --- decoy 2: retry logic for an entirely different system ----------------

INDEXER = '''"""Merchant search index refresh."""

import logging

log = logging.getLogger(__name__)

INDEX_RETRY_BASE_SECONDS = 2
INDEX_MAX_RETRIES = 6


def reindex(merchant_id, client, sleep):
    """Push one merchant document, exponential backoff on failure."""
    for attempt in range(INDEX_MAX_RETRIES):
        try:
            return client.upsert(merchant_id)
        except client.Unavailable:
            delay = INDEX_RETRY_BASE_SECONDS * (2 ** attempt)
            log.warning("index unavailable, retrying in %ds", delay)
            sleep(delay)
    raise RuntimeError(f"reindex failed for {merchant_id}")
'''

# --- null case: no Slack thread anywhere mentions this ---------------------
# "No relevant discussions found" is a demo beat, not a failure. It needs a
# selection that genuinely has no discussion behind it.

STRINGS = '''"""Small string helpers."""

import re
import unicodedata

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def slugify(value: str, max_length: int = 64) -> str:
    """Lowercase ASCII slug, collapsing runs of non-alphanumerics to hyphens."""
    normalized = unicodedata.normalize("NFKD", value)
    ascii_only = normalized.encode("ascii", "ignore").decode()
    slug = _NON_ALNUM.sub("-", ascii_only.lower()).strip("-")
    return slug[:max_length].rstrip("-")


def truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "\u2026"
'''

INIT = ""
