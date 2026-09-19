"""Dense and sparse vectors for a batch of units.

Dense embeds the summary alone -- the summary is prose about code, which is
the same modality as the query-side code-to-prose rewrite.

Sparse embeds summary + raw text + symbols, because that is the channel that
has to catch a bare identifier like RETRY_BACKOFF_SECONDS appearing once, in
one message, three replies deep.
"""

from __future__ import annotations

from .. import llm


def sparse_text(summary: str, raw_text: str, symbols: list[str]) -> str:
    return summary + "\n" + raw_text + "\n" + " ".join(symbols)


async def embed_units(
    summaries: list[str], raw_texts: list[str], symbol_lists: list[list[str]]
) -> tuple[list[list[float]], list[tuple[list[int], list[float]]]]:
    dense = await llm.embed_dense(summaries)
    sparse = llm.embed_sparse(
        [sparse_text(s, r, sym) for s, r, sym in zip(summaries, raw_texts, symbol_lists)]
    )
    return dense, sparse
