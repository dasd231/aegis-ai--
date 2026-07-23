from __future__ import annotations

import asyncio
from collections import OrderedDict
import hashlib
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from qdrant_client import QdrantClient, models

from app.core.config import get_settings
from app.services.chunking import TextChunk
from app.services.embeddings import EmbeddingService, create_embedding_service
from app.services.lexical_sparse import sparse_lexical_vector
from app.services.rag_strategy import RagStrategy, get_rag_strategy


INDEX_SCHEMA_VERSION = 4


@dataclass(frozen=True)
class SearchHit:
    chunk_id: str
    document_id: str
    knowledge_base_id: str
    source_name: str
    content: str
    summary: str
    distance: float
    priority: int
    parent_id: str = ""
    parent_content: str = ""
    section_path: str = ""
    chunk_index: int = 0
    schema_version: int = 1
    lexical_retrieval_score: float = 0.0
    hybrid_fusion_score: float = 0.0
    child_start: int = 0
    child_end: int = 0


class VectorStore:
    def __init__(
        self,
        path: Path,
        embeddings: EmbeddingService | None = None,
        *,
        url: str | None = None,
        api_key: str | None = None,
        collection_name: str | None = None,
        dimensions: int | None = None,
    ):
        settings = get_settings()
        if url:
            self.client = QdrantClient(
                url=url,
                api_key=api_key or None,
                timeout=settings.qdrant_timeout_seconds,
                prefer_grpc=settings.qdrant_prefer_grpc,
            )
        else:
            path.mkdir(parents=True, exist_ok=True)
            self.client = QdrantClient(path=str(path))
        self.embeddings = embeddings or create_embedding_service()
        self.dimensions = dimensions or settings.embedding_dimensions
        self.collection_name = collection_name or settings.qdrant_collection
        self.query_cache_size = settings.embedding_query_cache_size
        self.query_cache_ttl_seconds = settings.embedding_query_cache_ttl_seconds
        self._query_cache: OrderedDict[bytes, tuple[float, list[float]]] = OrderedDict()
        self._query_inflight: dict[bytes, asyncio.Task[list[float]]] = {}
        self._query_cache_lock = Lock()
        self._query_cache_hits = 0
        self._query_cache_misses = 0
        self._query_cache_coalesced = 0
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        if not self.client.collection_exists(self.collection_name):
            self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config={
                    "dense": models.VectorParams(
                        size=self.dimensions,
                        distance=models.Distance.COSINE,
                    )
                },
                sparse_vectors_config={
                    "lexical": models.SparseVectorParams(
                        index=models.SparseIndexParams(on_disk=False),
                        modifier=models.Modifier.IDF,
                    )
                },
            )
            return
        collection = self.client.get_collection(self.collection_name)
        vector_config = collection.config.params.vectors
        existing_dimensions = (
            getattr(vector_config.get("dense"), "size", None)
            if isinstance(vector_config, dict)
            else getattr(vector_config, "size", None)
        )
        if existing_dimensions != self.dimensions:
            raise RuntimeError(
                f"Qdrant collection uses {existing_dimensions} dimensions, but configuration requires "
                f"{self.dimensions}; rebuild the vector collection before changing embedding models"
            )
        sparse_config = collection.config.params.sparse_vectors or {}
        lexical_config = sparse_config.get("lexical") if isinstance(sparse_config, dict) else None
        if getattr(lexical_config, "modifier", None) != models.Modifier.IDF:
            raise RuntimeError(
                "Qdrant lexical vector does not use IDF; point this deployment at a new "
                "schema-v4 collection and run the controlled reindex job"
            )

    @staticmethod
    def _point_id(document_id: str, chunk_index: int) -> str:
        return str(uuid.uuid5(uuid.UUID(document_id), f"chunk:{chunk_index}"))

    async def upsert_document(
        self,
        document_id: str,
        knowledge_base_id: str,
        source_name: str,
        chunks: list[TextChunk],
    ) -> None:
        if not chunks:
            raise ValueError("cannot index an empty document")
        # Compute embeddings before touching the live index. During a reindex the
        # previous generation therefore remains queryable until all new vectors
        # are ready.
        index_texts = [
            "\n".join(part for part in (source_name, chunk.section_path, chunk.content) if part)
            for chunk in chunks
        ]
        vectors = await self.embeddings.embed_documents(index_texts)
        points = [
            models.PointStruct(
                id=self._point_id(document_id, chunk.index),
                vector={
                    "dense": vector,
                    "lexical": sparse_lexical_vector(index_text),
                },
                payload={
                    "chunk_id": f"{document_id}:{chunk.index}",
                    "document_id": document_id,
                    "knowledge_base_id": knowledge_base_id,
                    "source_name": source_name,
                    "chunk_index": chunk.index,
                    "content": chunk.content,
                    "summary": chunk.summary,
                    "priority": chunk.priority,
                    "parent_id": chunk.parent_id,
                    "parent_content": chunk.parent_content,
                    "section_path": chunk.section_path,
                    "child_start": chunk.child_start,
                    "child_end": chunk.child_end,
                    "schema_version": INDEX_SCHEMA_VERSION,
                },
            )
            for chunk, vector, index_text in zip(chunks, vectors, index_texts, strict=True)
        ]
        old_ids = await self._document_point_ids(document_id)
        await asyncio.to_thread(
            self.client.upsert,
            collection_name=self.collection_name,
            points=points,
            wait=True,
        )
        new_ids = {str(point.id) for point in points}
        stale_ids = sorted(old_ids - new_ids)
        if stale_ids:
            await asyncio.to_thread(
                self.client.delete,
                collection_name=self.collection_name,
                points_selector=models.PointIdsList(points=stale_ids),
                wait=True,
            )

    async def _document_point_ids(self, document_id: str) -> set[str]:
        def scroll_all() -> set[str]:
            point_ids: set[str] = set()
            offset = None
            while True:
                records, offset = self.client.scroll(
                    collection_name=self.collection_name,
                    scroll_filter=self._field_filter("document_id", document_id),
                    limit=256,
                    offset=offset,
                    with_payload=False,
                    with_vectors=False,
                )
                point_ids.update(str(record.id) for record in records)
                if offset is None:
                    return point_ids

        return await asyncio.to_thread(scroll_all)

    async def contains_document(self, document_id: str) -> bool:
        def find_one() -> bool:
            records, _ = self.client.scroll(
                collection_name=self.collection_name,
                scroll_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="document_id", match=models.MatchValue(value=document_id)
                        ),
                        models.FieldCondition(
                            key="schema_version",
                            match=models.MatchValue(value=INDEX_SCHEMA_VERSION),
                        ),
                    ]
                ),
                limit=1,
                with_payload=False,
                with_vectors=False,
            )
            return bool(records)

        return await asyncio.to_thread(find_one)

    @staticmethod
    def _field_filter(key: str, value: str) -> models.Filter:
        return models.Filter(
            must=[models.FieldCondition(key=key, match=models.MatchValue(value=value))]
        )

    async def delete_document(self, document_id: str) -> None:
        await asyncio.to_thread(
            self.client.delete,
            collection_name=self.collection_name,
            points_selector=models.FilterSelector(filter=self._field_filter("document_id", document_id)),
            wait=True,
        )

    def _finish_query_embedding(self, key: bytes, task: asyncio.Task[list[float]]) -> None:
        try:
            vector = task.result()
        except BaseException:
            vector = None
        with self._query_cache_lock:
            if self._query_inflight.get(key) is task:
                self._query_inflight.pop(key, None)
            if vector is None or self.query_cache_size == 0:
                return
            self._query_cache[key] = (time.monotonic() + self.query_cache_ttl_seconds, vector)
            self._query_cache.move_to_end(key)
            while len(self._query_cache) > self.query_cache_size:
                self._query_cache.popitem(last=False)

    async def _query_embedding(self, question: str) -> list[float]:
        """Use a bounded hashed cache and single-flight for repeated FAQ queries."""
        if self.query_cache_size == 0:
            return await self.embeddings.embed_query(question)
        key = hashlib.sha256(question.encode("utf-8")).digest()
        now = time.monotonic()
        loop = asyncio.get_running_loop()
        with self._query_cache_lock:
            cached = self._query_cache.get(key)
            if cached is not None:
                expires_at, vector = cached
                if expires_at > now:
                    self._query_cache_hits += 1
                    self._query_cache.move_to_end(key)
                    return vector
                self._query_cache.pop(key, None)
            task = self._query_inflight.get(key)
            if task is None or task.get_loop() is not loop:
                self._query_cache_misses += 1
                task = loop.create_task(self.embeddings.embed_query(question))
                self._query_inflight[key] = task
                task.add_done_callback(
                    lambda completed, cache_key=key: self._finish_query_embedding(cache_key, completed)
                )
            else:
                self._query_cache_coalesced += 1
        return await asyncio.shield(task)

    async def _query_embeddings(self, questions: list[str]) -> list[list[float]]:
        """Batch cache misses while retaining per-query single-flight semantics."""
        if len(questions) == 1:
            return [await self._query_embedding(questions[0])]
        if self.query_cache_size == 0:
            return await self.embeddings.embed_documents(questions)

        now = time.monotonic()
        loop = asyncio.get_running_loop()
        tasks: dict[bytes, asyncio.Task[list[float]]] = {}
        vectors: dict[bytes, list[float]] = {}
        missing: list[tuple[bytes, str]] = []
        keys = [hashlib.sha256(question.encode("utf-8")).digest() for question in questions]
        with self._query_cache_lock:
            for key, question in zip(keys, questions, strict=True):
                cached = self._query_cache.get(key)
                if cached is not None and cached[0] > now:
                    self._query_cache_hits += 1
                    self._query_cache.move_to_end(key)
                    vectors[key] = cached[1]
                    continue
                if cached is not None:
                    self._query_cache.pop(key, None)
                inflight = self._query_inflight.get(key)
                if inflight is not None and inflight.get_loop() is loop:
                    self._query_cache_coalesced += 1
                    tasks[key] = inflight
                    continue
                self._query_cache_misses += 1
                missing.append((key, question))

            if missing:
                batch_task = loop.create_task(
                    self.embeddings.embed_documents([question for _, question in missing])
                )

                async def select_vector(index: int) -> list[float]:
                    batch = await batch_task
                    if len(batch) != len(missing):
                        raise ValueError("embedding provider returned an unexpected vector count")
                    return batch[index]

                for index, (key, _) in enumerate(missing):
                    task = loop.create_task(select_vector(index))
                    self._query_inflight[key] = task
                    task.add_done_callback(
                        lambda completed, cache_key=key: self._finish_query_embedding(
                            cache_key, completed
                        )
                    )
                    tasks[key] = task

        if tasks:
            completed = await asyncio.gather(
                *(asyncio.shield(task) for task in tasks.values())
            )
            vectors.update(zip(tasks.keys(), completed, strict=True))
        return [vectors[key] for key in keys]

    def query_cache_snapshot(self) -> dict[str, int | float]:
        with self._query_cache_lock:
            hits = self._query_cache_hits
            misses = self._query_cache_misses
            return {
                "entries": len(self._query_cache),
                "limit": self.query_cache_size,
                "ttl_seconds": self.query_cache_ttl_seconds,
                "hits": hits,
                "misses": misses,
                "coalesced": self._query_cache_coalesced,
                "hit_rate": round(hits / max(1, hits + misses), 4),
            }

    async def query(self, question: str, top_k: int, knowledge_base_id: str | None = None) -> list[SearchHit]:
        settings = get_settings()
        strategy = get_rag_strategy(settings.rag_stable_version, settings)
        return await self.query_many(
            [question],
            top_k,
            knowledge_base_id,
            lexical_query=question,
            strategy=strategy,
        )

    async def query_many(
        self,
        semantic_queries: list[str] | tuple[str, ...],
        top_k: int,
        knowledge_base_id: str | None = None,
        *,
        lexical_query: str | None = None,
        strategy: RagStrategy | None = None,
    ) -> list[SearchHit]:
        """Run all dense/sparse channels in one Qdrant round trip and fuse ranks."""
        # Qdrant naturally returns fewer than top_k points for a small/empty
        # collection. Avoiding an exact COUNT removes another remote round trip.
        queries = list(dict.fromkeys(item.strip() for item in semantic_queries if item.strip()))[:3]
        if not queries:
            return []
        settings = get_settings()
        strategy = strategy or get_rag_strategy(settings.rag_stable_version, settings)
        vectors = await self._query_embeddings(queries)
        query_filter = (
            self._field_filter("knowledge_base_id", knowledge_base_id) if knowledge_base_id else None
        )
        requests = [
            models.QueryRequest(
                query=vector,
                using="dense",
                filter=query_filter,
                limit=top_k,
                with_payload=True,
                with_vector=False,
            )
            for vector in vectors
        ]
        sparse_query = sparse_lexical_vector(lexical_query or queries[0])
        if sparse_query.indices:
            requests.append(
                models.QueryRequest(
                    query=sparse_query,
                    using="lexical",
                    filter=query_filter,
                    limit=top_k,
                    with_payload=True,
                    with_vector=False,
                )
            )
        responses = await asyncio.to_thread(
            self.client.query_batch_points,
            collection_name=self.collection_name,
            requests=requests,
        )

        expansion_channels = max(1, len(vectors) - 1)
        channel_weights = [
            strategy.dense_weight
            if index == 0
            else strategy.expansion_weight / expansion_channels
            for index in range(len(vectors))
        ]
        if sparse_query.indices:
            channel_weights.append(strategy.sparse_weight)

        merged: dict[str, dict[str, object]] = {}
        active_weight = 0.0
        for channel_index, (response, weight) in enumerate(
            zip(responses, channel_weights, strict=True)
        ):
            points = list(response.points)
            if not points:
                continue
            active_weight += weight
            sparse_channel = channel_index >= len(vectors)
            max_sparse = (
                max((float(point.score) for point in points), default=1.0)
                if sparse_channel
                else 1.0
            )
            for rank, point in enumerate(points, start=1):
                point_id = str(point.id)
                item = merged.setdefault(
                    point_id,
                    {"point": point, "dense": None, "lexical": 0.0, "rrf": 0.0},
                )
                item["rrf"] = float(item["rrf"]) + weight / (
                    strategy.rrf_k + rank
                )
                if sparse_channel:
                    item["lexical"] = max(
                        float(item["lexical"]),
                        max(0.0, min(1.0, float(point.score) / max(1e-9, max_sparse))),
                    )
                else:
                    prior_dense = item["dense"]
                    item["dense"] = max(
                        float(point.score),
                        float(prior_dense) if prior_dense is not None else -1.0,
                    )

        max_rrf = active_weight / (strategy.rrf_k + 1) if active_weight else 1.0
        hits: list[SearchHit] = []
        for item in merged.values():
            point = item["point"]
            dense_score = item["dense"]
            sparse_score = float(item["lexical"])
            payload = point.payload or {}
            content = str(payload.get("content", ""))
            if not content:
                continue
            hits.append(
                SearchHit(
                    chunk_id=str(payload["chunk_id"]),
                    document_id=str(payload["document_id"]),
                    knowledge_base_id=str(payload["knowledge_base_id"]),
                    source_name=str(payload["source_name"]),
                    content=content,
                    summary=str(payload.get("summary", content[:180])),
                    distance=(
                        max(0.0, 1.0 - float(dense_score))
                        if dense_score is not None
                        else 0.62
                    ),
                    priority=int(payload.get("priority", 0)),
                    parent_id=str(payload.get("parent_id", "")),
                    parent_content=str(payload.get("parent_content", "")),
                    section_path=str(payload.get("section_path", "")),
                    chunk_index=int(payload.get("chunk_index", 0)),
                    schema_version=int(payload.get("schema_version", 1)),
                    lexical_retrieval_score=sparse_score,
                    hybrid_fusion_score=max(
                        0.0, min(1.0, float(item["rrf"]) / max(1e-9, max_rrf))
                    ),
                    child_start=int(payload.get("child_start", 0)),
                    child_end=int(payload.get("child_end", 0)),
                )
            )
        hits.sort(key=lambda item: item.hybrid_fusion_score, reverse=True)
        return hits[:top_k]

    def clear_all(self) -> None:
        with self._query_cache_lock:
            self._query_cache.clear()
        if not self.client.collection_exists(self.collection_name):
            self._ensure_collection()
            return
        # Qdrant local mode can retain the old in-process collection object when
        # a collection is deleted and immediately recreated on the same client.
        # Enumerating IDs and waiting for deletion is deterministic in local and
        # remote modes while preserving the validated vector schema.
        point_ids: list[str] = []
        offset = None
        while True:
            records, offset = self.client.scroll(
                collection_name=self.collection_name,
                limit=512,
                offset=offset,
                with_payload=False,
                with_vectors=False,
            )
            point_ids.extend(str(record.id) for record in records)
            if offset is None:
                break
        for start in range(0, len(point_ids), 512):
            self.client.delete(
                collection_name=self.collection_name,
                points_selector=models.PointIdsList(points=point_ids[start : start + 512]),
                wait=True,
            )

    def healthcheck(self) -> None:
        self.client.get_collection(self.collection_name)

    async def close(self) -> None:
        await self.embeddings.close()
        await asyncio.to_thread(self.client.close)


_stores: dict[str, VectorStore] = {}
_store_lock = Lock()


def get_vector_store(pool: str = "stable") -> VectorStore:
    if pool not in {"stable", "canary"}:
        raise ValueError(f"unknown vector pool: {pool}")
    store = _stores.get(pool)
    if store is None:
        with _store_lock:
            store = _stores.get(pool)
            if store is None:
                settings = get_settings()
                canary = pool == "canary"
                collection = (
                    settings.qdrant_canary_collection if canary else settings.qdrant_collection
                )
                if not collection:
                    raise RuntimeError("canary vector pool selected without QDRANT_CANARY_COLLECTION")
                dimensions = (
                    settings.embedding_canary_dimensions if canary else settings.embedding_dimensions
                )
                store = VectorStore(
                    settings.qdrant_path / "canary" if canary and not settings.qdrant_url else settings.qdrant_path,
                    create_embedding_service(pool),
                    url=settings.qdrant_url,
                    api_key=settings.qdrant_api_key.get_secret_value(),
                    collection_name=collection,
                    dimensions=dimensions,
                )
                _stores[pool] = store
    return store


def configured_vector_pools() -> tuple[str, ...]:
    settings = get_settings()
    if settings.qdrant_canary_collection and settings.embedding_canary_model:
        return ("stable", "canary")
    return ("stable",)


async def close_vector_store() -> None:
    with _store_lock:
        stores = list(_stores.values())
        _stores.clear()
    await asyncio.gather(*(store.close() for store in stores), return_exceptions=True)


def vector_cache_snapshot() -> dict[str, dict[str, int | float]]:
    return {pool: get_vector_store(pool).query_cache_snapshot() for pool in configured_vector_pools()}
