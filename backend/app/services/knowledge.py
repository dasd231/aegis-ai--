from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import (
    DocumentStatus,
    IngestionJobStatus,
    KnowledgeDocument,
    KnowledgeIngestionJob,
    utcnow,
)
from app.services.chunking import chunk_text
from app.services.document_parser import DocumentParseError
from app.services.multimodal import get_multimodal_service
from app.services.vector_store import configured_vector_pools, get_vector_store


logger = logging.getLogger(__name__)


async def _delete_from_all_indexes(document_id: str) -> None:
    results = await asyncio.gather(
        *(get_vector_store(pool).delete_document(document_id) for pool in configured_vector_pools()),
        return_exceptions=True,
    )
    failures = [result for result in results if isinstance(result, BaseException)]
    if failures:
        raise RuntimeError(f"vector cleanup failed in {len(failures)} index pool(s)") from failures[0]


async def _upsert_all_indexes(
    document_id: str,
    knowledge_base_id: str,
    original_name: str,
    chunks,
    *,
    pools: Sequence[str] | None = None,
    cleanup_successful_on_failure: bool = True,
) -> None:
    target_pools = tuple(pools or configured_vector_pools())
    results = await asyncio.gather(
        *(
            get_vector_store(pool).upsert_document(
                document_id,
                knowledge_base_id,
                original_name,
                chunks,
            )
            for pool in target_pools
        ),
        return_exceptions=True,
    )
    failures = [result for result in results if isinstance(result, BaseException)]
    if failures:
        if cleanup_successful_on_failure:
            # Initial ingestion is not visible until every configured pool is
            # complete. Remove only pools that definitely succeeded; deleting
            # every pool here would destroy a prior Stable generation during a
            # Canary-only repair.
            successful_pools = [
                pool
                for pool, result in zip(target_pools, results, strict=True)
                if not isinstance(result, BaseException)
            ]
            await asyncio.gather(
                *(
                    get_vector_store(pool).delete_document(document_id)
                    for pool in successful_pools
                ),
                return_exceptions=True,
            )
        raise RuntimeError(f"vector write failed in {len(failures)} index pool(s)") from failures[0]


@dataclass(frozen=True)
class IngestionFence:
    job_id: str
    lease_owner: str
    attempt: int


def _fence_matches(db: Session, fence: IngestionFence | None, *, lock: bool = False) -> bool:
    if fence is None:
        return True
    query = select(KnowledgeIngestionJob).where(KnowledgeIngestionJob.id == fence.job_id)
    if lock:
        query = query.with_for_update()
    job = db.scalar(query)
    if (
        job is None
        or job.status != IngestionJobStatus.RUNNING
        or job.lease_owner != fence.lease_owner
        or job.attempts != fence.attempt
        or job.lease_expires_at is None
    ):
        return False
    now = utcnow()
    comparable_now = now if job.lease_expires_at.tzinfo is not None else now.replace(tzinfo=None)
    return job.lease_expires_at >= comparable_now


async def process_document(document_id: str, fence: IngestionFence | None = None) -> bool:
    """Background ingestion with compensating vector cleanup on every failure."""
    with SessionLocal() as db:
        document = db.get(KnowledgeDocument, document_id)
        if document is None:
            return False
        if document.status == DocumentStatus.READY:
            return True
        if document.status != DocumentStatus.PROCESSING:
            return False
        if not _fence_matches(db, fence):
            logger.warning("ignored stale ingestion before processing document_id=%s", document_id)
            return False
        path = Path(document.storage_path)
        original_name = document.original_name
        knowledge_base_id = document.knowledge_base_id

    try:
        await _delete_from_all_indexes(document_id)
        text = await get_multimodal_service().parse(path, Path(original_name).suffix.lower())
        chunks = chunk_text(text)
        if not chunks:
            raise DocumentParseError("文档中没有可索引的有效文本")
        with SessionLocal() as db:
            if not _fence_matches(db, fence):
                logger.warning("ignored stale ingestion before vector write document_id=%s", document_id)
                return False
        await _upsert_all_indexes(
            document_id,
            knowledge_base_id,
            original_name,
            chunks,
        )
        indexed_but_obsolete = False
        with SessionLocal() as db:
            document = db.get(KnowledgeDocument, document_id)
            if (
                document is None
                or document.status != DocumentStatus.PROCESSING
                or not _fence_matches(db, fence, lock=True)
            ):
                indexed_but_obsolete = True
            else:
                document.status = DocumentStatus.READY
                document.chunk_count = len(chunks)
                document.error_message = None
                db.commit()
        if indexed_but_obsolete:
            await _delete_from_all_indexes(document_id)
            return False
        return True
    except Exception as exc:
        logger.exception("document ingestion failed: %s", document_id)
        try:
            await _delete_from_all_indexes(document_id)
        except Exception:
            logger.exception("compensating vector deletion failed: %s", document_id)
        with SessionLocal() as db:
            document = db.get(KnowledgeDocument, document_id)
            if (
                document
                and document.status == DocumentStatus.PROCESSING
                and _fence_matches(db, fence, lock=True)
            ):
                document.status = DocumentStatus.FAILED
                document.chunk_count = 0
                document.error_message = (
                    str(exc)[:500] if isinstance(exc, DocumentParseError) else "处理失败，请检查文档格式或模型配置"
                )
                db.commit()
        return False


async def reindex_ready_document(
    document_id: str,
    pools: Sequence[str] | None = None,
) -> bool:
    """Rebuild a ready document while its prior vectors remain available on failure."""
    with SessionLocal() as db:
        document = db.get(KnowledgeDocument, document_id)
        if document is None or document.status != DocumentStatus.READY:
            return False
        path = Path(document.storage_path)
        original_name = document.original_name
        knowledge_base_id = document.knowledge_base_id
    try:
        text = await get_multimodal_service().parse(path, Path(original_name).suffix.lower())
        chunks = chunk_text(text)
        if not chunks:
            raise DocumentParseError("文档中没有可索引的有效文本")
        await _upsert_all_indexes(
            document_id,
            knowledge_base_id,
            original_name,
            chunks,
            pools=pools,
            cleanup_successful_on_failure=False,
        )
        with SessionLocal() as db:
            document = db.get(KnowledgeDocument, document_id)
            if document is None or document.status != DocumentStatus.READY:
                return False
            document.chunk_count = len(chunks)
            document.error_message = None
            db.commit()
        return True
    except Exception:
        logger.exception("ready document reindex failed; previous vectors retained: %s", document_id)
        return False


async def delete_document(document_id: str) -> Path | None:
    with SessionLocal() as db:
        document = db.get(KnowledgeDocument, document_id)
        if document is None:
            return None
        document.status = DocumentStatus.DELETING
        db.commit()
        path = Path(document.storage_path)

    try:
        await _delete_from_all_indexes(document_id)
        with SessionLocal() as db:
            document = db.get(KnowledgeDocument, document_id)
            if document:
                db.delete(document)
                db.commit()
    except Exception:
        with SessionLocal() as db:
            document = db.get(KnowledgeDocument, document_id)
            if document:
                document.status = DocumentStatus.DELETE_FAILED
                document.error_message = "删除向量数据失败，元数据已保留以便重试"
                db.commit()
        raise
    return path
