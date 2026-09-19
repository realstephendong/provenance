"""Qdrant collection setup and upsert.

Two named vectors so hybrid search is a config change rather than a rewrite,
keyword payload indexes so the exact-match filter is an index lookup rather
than a full scan, and deterministic point ids so re-ingesting is idempotent.
You will re-ingest a lot while tuning.
"""

from __future__ import annotations

import time
import uuid

from qdrant_client import QdrantClient, models

from .. import config

META_COLLECTION = "hindsight_meta"
META_POINT_ID = 0

# Trap #3 lives here. A silent 1536-vs-3072 mismatch produces confusing
# garbage rather than a clean error, so the collection records which embedder
# built it and the query side refuses to run against a different one.
_KEYWORD_INDEXES = [
    "pr_refs",
    "commit_shas",
    "ticket_refs",
    "file_paths",
    "participants",
    "channel_name",
]


def client() -> QdrantClient:
    return QdrantClient(url=config.QDRANT_URL, timeout=30)


def point_id(channel_id: str, thread_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{channel_id}/{thread_id}"))


def ensure_collection(qc: QdrantClient, recreate: bool = False) -> None:
    exists = qc.collection_exists(config.COLLECTION)
    if exists and recreate:
        qc.delete_collection(config.COLLECTION)
        exists = False

    if not exists:
        qc.create_collection(
            collection_name=config.COLLECTION,
            vectors_config={
                "dense": models.VectorParams(size=config.DENSE_DIM, distance=models.Distance.COSINE),
            },
            sparse_vectors_config={
                "sparse": models.SparseVectorParams(modifier=models.Modifier.IDF),
            },
        )

    for field in _KEYWORD_INDEXES:
        try:
            qc.create_payload_index(
                collection_name=config.COLLECTION,
                field_name=field,
                field_schema=models.PayloadSchemaType.KEYWORD
                if field != "pr_refs"
                else models.PayloadSchemaType.INTEGER,
            )
        except Exception:
            pass  # already indexed

    try:
        qc.create_payload_index(
            collection_name=config.COLLECTION,
            field_name="ts_start",
            field_schema=models.PayloadSchemaType.FLOAT,
        )
    except Exception:
        pass

    _stamp_embedder(qc)


def _stamp_embedder(qc: QdrantClient) -> None:
    if not qc.collection_exists(META_COLLECTION):
        qc.create_collection(
            collection_name=META_COLLECTION,
            vectors_config=models.VectorParams(size=1, distance=models.Distance.COSINE),
        )
    qc.upsert(
        collection_name=META_COLLECTION,
        points=[
            models.PointStruct(
                id=META_POINT_ID,
                vector=[1.0],
                payload={
                    "embedder_id": config.EMBEDDER_ID,
                    "dense_dim": config.DENSE_DIM,
                    "built_at": time.time(),
                },
            )
        ],
    )


def read_embedder_id(qc: QdrantClient) -> str | None:
    if not qc.collection_exists(META_COLLECTION):
        return None
    pts = qc.retrieve(collection_name=META_COLLECTION, ids=[META_POINT_ID], with_payload=True)
    return pts[0].payload.get("embedder_id") if pts else None


def upsert(qc: QdrantClient, payloads: list[dict], dense, sparse) -> int:
    points = []
    for payload, d, (idx, val) in zip(payloads, dense, sparse):
        points.append(
            models.PointStruct(
                id=point_id(payload["channel_id"], payload["thread_id"]),
                vector={
                    "dense": d,
                    "sparse": models.SparseVector(indices=idx, values=val),
                },
                payload=payload,
            )
        )

    for i in range(0, len(points), config.UPSERT_BATCH):
        qc.upsert(collection_name=config.COLLECTION, points=points[i : i + config.UPSERT_BATCH], wait=True)
    return len(points)
