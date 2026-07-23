from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import text

from app.database import SessionLocal
from app.services.capacity import capacity_snapshot
from app.services.job_queue import ingestion_queue_snapshot
from app.services.resilience import resilience_snapshot
from app.services.vector_store import configured_vector_pools, get_vector_store, vector_cache_snapshot


router = APIRouter(prefix="/health", tags=["健康检查"])
logger = logging.getLogger(__name__)


@router.get("/live")
def live():
    return {"status": "ok"}


@router.get("/ready")
def ready():
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
        for pool in configured_vector_pools():
            get_vector_store(pool).healthcheck()
    except Exception:
        logger.exception("readiness dependency check failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="必要的服务依赖当前不可用",
        ) from None
    return {
        "status": "ready",
        "components": {
            "database": "ready",
            "vector_store": {pool: "ready" for pool in configured_vector_pools()},
        },
    }


@router.get("/capacity")
def capacity() -> dict[str, object]:
    return {
        "requests": capacity_snapshot(),
        "dependencies": resilience_snapshot(),
        "retrieval_cache": vector_cache_snapshot(),
        "ingestion_queue": ingestion_queue_snapshot(),
    }
