from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.database import SessionLocal
from app.models import (
    DocumentStatus,
    IngestionJobStatus,
    KnowledgeDocument,
    KnowledgeIngestionJob,
    utcnow,
)
from app.services.knowledge import IngestionFence, process_document


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClaimedJob:
    id: str
    document_id: str
    attempts: int
    max_attempts: int
    lease_owner: str


@dataclass
class DocumentAdvisoryLock:
    db: Session
    name: str
    mysql: bool


def try_acquire_document_lock(document_id: str) -> DocumentAdvisoryLock | None:
    """Serialize one document across workers; MySQL owns the distributed lock."""
    db = SessionLocal()
    mysql = db.get_bind().dialect.name == "mysql"
    name = f"aegis-ingest:{document_id}"[:64]
    if not mysql:
        return DocumentAdvisoryLock(db, name, False)
    try:
        acquired = db.scalar(text("SELECT GET_LOCK(:name, 0)"), {"name": name})
        if acquired != 1:
            db.close()
            return None
        return DocumentAdvisoryLock(db, name, True)
    except Exception:
        db.close()
        raise


def release_document_lock(lock: DocumentAdvisoryLock) -> None:
    try:
        if lock.mysql:
            lock.db.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": lock.name})
    finally:
        lock.db.close()


def enqueue_ingestion(db: Session, document_id: str, *, reset_attempts: bool = False) -> None:
    job = db.scalar(select(KnowledgeIngestionJob).where(KnowledgeIngestionJob.document_id == document_id))
    if job is None:
        db.add(KnowledgeIngestionJob(document_id=document_id))
        return
    job.status = IngestionJobStatus.QUEUED
    job.available_at = utcnow()
    job.lease_owner = None
    job.lease_expires_at = None
    job.last_error = None
    if reset_attempts:
        job.attempts = 0


def _expire_exhausted_jobs(db: Session, now) -> None:
    exhausted = list(
        db.scalars(
            select(KnowledgeIngestionJob)
            .where(
                KnowledgeIngestionJob.status == IngestionJobStatus.RUNNING,
                KnowledgeIngestionJob.lease_expires_at.is_not(None),
                KnowledgeIngestionJob.lease_expires_at < now,
                KnowledgeIngestionJob.attempts >= KnowledgeIngestionJob.max_attempts,
            )
            .limit(100)
            .with_for_update(skip_locked=True)
        ).all()
    )
    for job in exhausted:
        job.status = IngestionJobStatus.DEAD
        job.lease_owner = None
        job.lease_expires_at = None
        job.last_error = job.last_error or "处理服务在最后一次尝试中超时"
        document = db.get(KnowledgeDocument, job.document_id)
        if document is not None and document.status != DocumentStatus.READY:
            document.status = DocumentStatus.FAILED
            document.error_message = job.last_error[:500]


def claim_ingestion_job(worker_id: str) -> ClaimedJob | None:
    settings = get_settings()
    now = utcnow()
    with SessionLocal() as db:
        _expire_exhausted_jobs(db, now)
        job = db.scalar(
            select(KnowledgeIngestionJob)
            .where(
                KnowledgeIngestionJob.attempts < KnowledgeIngestionJob.max_attempts,
                or_(
                    and_(
                        KnowledgeIngestionJob.status.in_([IngestionJobStatus.QUEUED, IngestionJobStatus.RETRY]),
                        KnowledgeIngestionJob.available_at <= now,
                    ),
                    and_(
                        KnowledgeIngestionJob.status == IngestionJobStatus.RUNNING,
                        KnowledgeIngestionJob.lease_expires_at.is_not(None),
                        KnowledgeIngestionJob.lease_expires_at < now,
                    ),
                )
            )
            .order_by(KnowledgeIngestionJob.available_at.asc(), KnowledgeIngestionJob.created_at.asc())
            .with_for_update(skip_locked=True)
        )
        if job is None:
            db.commit()
            return None
        document = db.get(KnowledgeDocument, job.document_id)
        if document is None:
            db.delete(job)
            db.commit()
            return None
        job.status = IngestionJobStatus.RUNNING
        job.attempts += 1
        job.lease_owner = worker_id
        job.lease_expires_at = now + timedelta(seconds=settings.ingestion_lease_seconds)
        if document.status == DocumentStatus.FAILED:
            document.status = DocumentStatus.PROCESSING
            document.error_message = None
        db.commit()
        return ClaimedJob(job.id, job.document_id, job.attempts, job.max_attempts, worker_id)


def renew_ingestion_lease(claim: ClaimedJob) -> bool:
    settings = get_settings()
    with SessionLocal() as db:
        result = db.execute(
            update(KnowledgeIngestionJob)
            .where(
                KnowledgeIngestionJob.id == claim.id,
                KnowledgeIngestionJob.status == IngestionJobStatus.RUNNING,
                KnowledgeIngestionJob.lease_owner == claim.lease_owner,
                KnowledgeIngestionJob.attempts == claim.attempts,
            )
            .values(lease_expires_at=utcnow() + timedelta(seconds=settings.ingestion_lease_seconds))
        )
        db.commit()
        return result.rowcount == 1


def finish_ingestion_job(claim: ClaimedJob, succeeded: bool) -> None:
    with SessionLocal() as db:
        job = db.get(KnowledgeIngestionJob, claim.id)
        document = db.get(KnowledgeDocument, claim.document_id)
        # The attempt number plus lease owner is a fencing token. A slow worker
        # whose lease was reclaimed must never overwrite the newer attempt.
        if (
            job is None
            or job.status != IngestionJobStatus.RUNNING
            or job.lease_owner != claim.lease_owner
            or job.attempts != claim.attempts
        ):
            logger.warning("ignored stale ingestion completion job_id=%s worker=%s", claim.id, claim.lease_owner)
            return
        job.lease_owner = None
        job.lease_expires_at = None
        if succeeded:
            job.status = IngestionJobStatus.COMPLETED
            job.last_error = None
        else:
            job.last_error = ((document.error_message or "文档入库失败") if document else "文档不存在")[:500]
            if job.attempts >= job.max_attempts or document is None:
                job.status = IngestionJobStatus.DEAD
                if document is not None and document.status != DocumentStatus.READY:
                    document.status = DocumentStatus.FAILED
                    document.error_message = job.last_error
            else:
                job.status = IngestionJobStatus.RETRY
                job.available_at = utcnow() + timedelta(seconds=min(300, 2**job.attempts))
                document.status = DocumentStatus.PROCESSING
        db.commit()


def ingestion_queue_snapshot() -> dict[str, int | float | None]:
    now = utcnow()
    with SessionLocal() as db:
        counts = {
            status.value: 0
            for status in IngestionJobStatus
        }
        for job_status, count in db.execute(
            select(KnowledgeIngestionJob.status, func.count()).group_by(KnowledgeIngestionJob.status)
        ).all():
            counts[job_status.value] = int(count)
        oldest = db.scalar(
            select(func.min(KnowledgeIngestionJob.available_at)).where(
                KnowledgeIngestionJob.status.in_([IngestionJobStatus.QUEUED, IngestionJobStatus.RETRY])
            )
        )
        expired_leases = db.scalar(
            select(func.count()).select_from(KnowledgeIngestionJob).where(
                KnowledgeIngestionJob.status == IngestionJobStatus.RUNNING,
                KnowledgeIngestionJob.lease_expires_at.is_not(None),
                KnowledgeIngestionJob.lease_expires_at < now,
            )
        ) or 0
    oldest_age: float | None = None
    if oldest is not None:
        comparable_now = now if oldest.tzinfo is not None else now.replace(tzinfo=None)
        oldest_age = round(max(0.0, (comparable_now - oldest).total_seconds()), 3)
    return {
        **counts,
        "pending": counts[IngestionJobStatus.QUEUED.value] + counts[IngestionJobStatus.RETRY.value],
        "expired_leases": int(expired_leases),
        "oldest_pending_age_seconds": oldest_age,
    }


async def run_worker() -> None:
    settings = get_settings()
    worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    logger.info("ingestion worker started worker_id=%s", worker_id)
    while True:
        claim = await asyncio.to_thread(claim_ingestion_job, worker_id)
        if claim is None:
            await asyncio.sleep(settings.ingestion_poll_seconds)
            continue
        document_lock: DocumentAdvisoryLock | None = None
        while document_lock is None:
            document_lock = await asyncio.to_thread(try_acquire_document_lock, claim.document_id)
            if document_lock is not None:
                break
            await asyncio.sleep(min(5.0, settings.ingestion_lease_seconds / 6))
            if not await asyncio.to_thread(renew_ingestion_lease, claim):
                logger.warning("ingestion lease lost while waiting for document lock job_id=%s", claim.id)
                break
        if document_lock is None:
            continue

        lease_lost = False
        fence = IngestionFence(claim.id, claim.lease_owner, claim.attempts)
        process_task = asyncio.create_task(process_document(claim.document_id, fence))

        async def maintain_lease() -> None:
            nonlocal lease_lost
            interval = max(5.0, min(60.0, settings.ingestion_lease_seconds / 3))
            while not process_task.done():
                await asyncio.sleep(interval)
                if process_task.done():
                    return
                try:
                    renewed = await asyncio.to_thread(renew_ingestion_lease, claim)
                except Exception:
                    logger.exception("ingestion lease renewal failed job_id=%s", claim.id)
                    renewed = False
                if not renewed:
                    lease_lost = True
                    return

        heartbeat_task = asyncio.create_task(maintain_lease())
        try:
            succeeded = await process_task
        except asyncio.CancelledError:
            if lease_lost:
                logger.warning("ingestion lease lost; stale attempt finished under lock job_id=%s", claim.id)
            else:
                await asyncio.shield(asyncio.to_thread(finish_ingestion_job, claim, False))
            raise
        except Exception:
            logger.exception("unhandled ingestion worker error job_id=%s", claim.id)
            succeeded = False
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
            await asyncio.shield(asyncio.to_thread(release_document_lock, document_lock))
        if lease_lost:
            logger.warning("ignored completion after ingestion lease loss job_id=%s", claim.id)
            continue
        await asyncio.to_thread(finish_ingestion_job, claim, succeeded)
