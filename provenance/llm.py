"""The only module that talks to OpenAI. One mode: real calls against a real key. No
offline fallback -- see 4."""

from __future__ import annotations

import asyncio
import json
import random
from functools import lru_cache
from typing import Any

from . import config


class LLMError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _client():
    from openai import AsyncOpenAI

    config.require_api_key()
    return AsyncOpenAI(api_key=config.OPENAI_API_KEY)


async def _with_retry(fn, *args, **kwargs):
    """Transient-failure resilience: OpenAI 429/5xx get retried with jittered
    exponential backoff. A 4xx that isn't 429 (bad request, e.g. malformed schema)
    is not retried -- retrying it just wastes the budget on a call that will never
    succeed."""
    from openai import APIStatusError

    last_exc = None
    for attempt in range(config.LLM_MAX_RETRIES):
        try:
            return await fn(*args, **kwargs)
        except APIStatusError as exc:
            last_exc = exc
            if exc.status_code not in (429, 500, 502, 503, 529):
                raise
            delay = config.LLM_RETRY_BASE_SECONDS * (2 ** attempt) * (1 + random.random() * 0.25)
            await asyncio.sleep(delay)
    raise LLMError(f"exhausted retries: {last_exc}")


async def complete(prompt: str, model: str, *, temperature: float = 0.2, max_tokens: int = 700) -> str:
    async def call():
        return await _client().chat.completions.create(
            model=model, temperature=temperature, max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )

    resp = await _with_retry(call)
    return (resp.choices[0].message.content or "").strip()


async def complete_json(prompt: str, model: str, *, temperature: float = 0.0, max_tokens: int = 1200) -> Any:
    """Strict-JSON completion. Every caller asks for a top-level object (e.g.
    {"items": [...]}) because response_format=json_object requires it."""

    async def call():
        return await _client().chat.completions.create(
            model=model, temperature=temperature, max_tokens=max_tokens,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
        )

    resp = await _with_retry(call)
    text = (resp.choices[0].message.content or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError(f"model returned non-JSON: {text[:200]}") from exc


async def complete_many(
    prompts: list[str], model: str, *, concurrency: int = config.SUMMARY_CONCURRENCY, **kw
) -> list[str]:
    """One bad prompt must never kill a batch run -- each failure is caught and
    returned as a sentinel string, never raised past this function."""
    sem = asyncio.Semaphore(concurrency)

    async def one(p: str) -> str:
        async with sem:
            try:
                return await complete(p, model, **kw)
            except Exception as exc:
                return f"__ERROR__ {exc}"

    return await asyncio.gather(*(one(p) for p in prompts))


async def embed_dense(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    out: list[list[float]] = []
    for i in range(0, len(texts), 128):
        async def call(batch=texts[i:i + 128]):
            return await _client().embeddings.create(model=config.DENSE_MODEL, input=batch)

        resp = await _with_retry(call)
        out.extend(d.embedding for d in resp.data)
    return out
