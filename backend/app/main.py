from __future__ import annotations

import logging
import re
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from app.api import admin, auth, chat, feedback, handoffs, health, knowledge, profile, sessions, tasks
from app.core.config import get_settings
from app.database import Base, engine
from app.services.bootstrap import ensure_seed_data
from app.services.capacity import CapacityMiddleware
from app.services.multimodal import close_multimodal_service
from app.services.canary_control import get_control
from app.services.rag import close_answer_generator
from app.services.reranker import close_evidence_reranker
from app.services.vector_store import close_vector_store
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from app.database import SessionLocal
from app.services.metrics import CANARY_ENABLED
from app.services.provider_credentials import ensure_provider_credentials


settings = get_settings()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    settings.qdrant_path.mkdir(parents=True, exist_ok=True)
    if settings.auto_create_tables:
        Base.metadata.create_all(bind=engine)
    try:
        ensure_provider_credentials()
        await ensure_seed_data()
        yield
    finally:
        await close_answer_generator()
        await close_evidence_reranker()
        await close_multimodal_service()
        await close_vector_store()
        engine.dispose()


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    description="可追溯、拒绝臆造的企业级 RAG 智能客服 API",
    lifespan=lifespan,
    docs_url="/docs" if settings.environment != "production" else None,
    redoc_url=None,
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.host_list)
app.add_middleware(CapacityMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.origin_list,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
    expose_headers=["X-Request-ID"],
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    supplied = request.headers.get("X-Request-ID", "")
    request_id = supplied if re.fullmatch(r"[A-Za-z0-9_.-]{8,64}", supplied) else str(uuid.uuid4())
    request.state.request_id = request_id
    started = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    logger.info(
        "%s %s status=%s duration_ms=%s request_id=%s",
        request.method,
        request.url.path,
        response.status_code,
        round((time.perf_counter() - started) * 1000),
        request_id,
    )
    return response


for router in (
    health.router,
    auth.router,
    profile.router,
    sessions.router,
    tasks.router,
    feedback.router,
    handoffs.router,
    knowledge.router,
    chat.router,
    admin.router,
):
    app.include_router(router, prefix=settings.api_prefix)


@app.get("/", include_in_schema=False)
def root():
    return {"service": settings.app_name, "version": "1.0.0", "health": f"{settings.api_prefix}/health/live"}


@app.get("/metrics", include_in_schema=False)
def prometheus_metrics():
    with SessionLocal() as db:
        control = get_control(db)
        CANARY_ENABLED.set(1 if control.canary_enabled else 0)
        db.commit()
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
