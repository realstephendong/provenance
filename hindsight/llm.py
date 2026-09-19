"""The only module that talks to a model provider.

One mode: real calls against a real key. Model names live in config.py, never
here.

There was briefly a fake-LLM mode for running offline. It was removed on
purpose: it let the whole pipeline go green while the summaries, the rerank
and the synthesis were all placeholder text, which made every eval number a
lie. If you need to work without network access, work on the parts that do
not need a model -- the exact-match tier, git blame, PR resolution and the
panel all run without one.
"""

from __future__ import annotations

import asyncio
import json
from functools import lru_cache
from typing import Any

from . import config


class LLMError(RuntimeError):
    pass


# --- client ---------------------------------------------------------------


@lru_cache(maxsize=1)
def _client():
    from openai import AsyncOpenAI

    config.require_api_key()
    return AsyncOpenAI(api_key=config.OPENAI_API_KEY)


# --- completions ----------------------------------------------------------


async def complete(prompt: str, model: str, *, temperature: float = 0.2, max_tokens: int = 700) -> str:
    resp = await _client().chat.completions.create(
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return (resp.choices[0].message.content or "").strip()


async def complete_json(prompt: str, model: str, *, temperature: float = 0.0, max_tokens: int = 1200) -> Any:
    """Strict-JSON completion. Returns parsed JSON, or raises LLMError."""
    resp = await _client().chat.completions.create(
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
        # response_format=json_object requires an object at the top level, so
        # every caller asks for {"items": [...]}.
        messages=[{"role": "user", "content": prompt}],
    )
    text = (resp.choices[0].message.content or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:  # pragma: no cover - provider drift
        raise LLMError(f"model returned non-JSON: {text[:200]}") from exc


async def complete_many(
    prompts: list[str], model: str, *, concurrency: int = config.SUMMARY_CONCURRENCY, **kw
) -> list[str]:
    sem = asyncio.Semaphore(concurrency)

    async def one(p: str) -> str:
        async with sem:
            try:
                return await complete(p, model, **kw)
            except Exception as exc:  # one bad thread must not kill the run
                return f"__ERROR__ {exc}"

    return await asyncio.gather(*(one(p) for p in prompts))


# --- embeddings -----------------------------------------------------------


async def embed_dense(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    out: list[list[float]] = []
    # OpenAI caps batch size well above this; 128 keeps payloads sane.
    for i in range(0, len(texts), 128):
        resp = await _client().embeddings.create(
            model=config.DENSE_MODEL, input=texts[i : i + 128]
        )
        out.extend(d.embedding for d in resp.data)
    return out


@lru_cache(maxsize=1)
def _sparse_model():
    from fastembed import SparseTextEmbedding

    return SparseTextEmbedding(model_name=config.SPARSE_MODEL)


def embed_sparse(texts: list[str]) -> list[tuple[list[int], list[float]]]:
    """BM25 sparse vectors as (indices, values) pairs. Local, no network."""
    if not texts:
        return []
    return [(list(e.indices), list(e.values)) for e in _sparse_model().embed(texts)]
