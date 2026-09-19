"""Dense embeddings for the indexed corpus.

Dense-only by design: Elasticsearch's own `text` fields on summary/raw_text/symbols
are the lexical channel, so unlike the original Qdrant design there is no local
sparse embedding step (9.4).

The *summary* is embedded, never the raw thread -- it is the side of the pairing that
matches the code-to-prose description the query side produces (12.2).
"""

from __future__ import annotations

from .. import llm


async def embed_summaries(summaries: list[str]) -> list[list[float]]:
    return await llm.embed_dense(summaries)
