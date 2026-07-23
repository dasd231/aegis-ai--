from __future__ import annotations

from datetime import timedelta
import math

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.database import SessionLocal
from app.models import CanaryObservation, ExperimentControl, ExperimentControlAudit, utcnow
from app.services.experiment import ExperimentAssignment
from app.services.metrics import CANARY_ENABLED, observe_canary


CONTROL_ID = "global"


def _control_snapshot(control: ExperimentControl) -> dict:
    return {
        "canary_enabled": control.canary_enabled,
        "stage": control.stage,
        "model_canary_percent": control.model_canary_percent,
        "embedding_canary_percent": control.embedding_canary_percent,
        "prompt_canary_percent": control.prompt_canary_percent,
        "rag_canary_percent": control.rag_canary_percent,
        "service_canary_percent": control.service_canary_percent,
        "shadow_canary_percent": control.shadow_canary_percent,
    }


def get_control(db: Session, *, lock: bool = False) -> ExperimentControl:
    query = select(ExperimentControl).where(ExperimentControl.id == CONTROL_ID)
    if lock:
        query = query.with_for_update()
    control = db.scalar(query)
    if control is None:
        settings = get_settings()
        control = ExperimentControl(
            id=CONTROL_ID,
            canary_enabled=True,
            stage=0,
            model_canary_percent=settings.model_canary_percent,
            embedding_canary_percent=settings.embedding_canary_percent,
            prompt_canary_percent=settings.prompt_canary_percent,
            rag_canary_percent=settings.rag_canary_percent,
            service_canary_percent=settings.service_canary_percent,
            shadow_canary_percent=settings.shadow_canary_percent,
            version=1,
        )
        db.add(control)
        db.flush()
    return control


def _p95(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


def record_canary_observation(
    assignment: ExperimentAssignment,
    *,
    success: bool,
    latency_ms: int,
    ttft_ms: int | None,
    error_code: str | None = None,
    timed_out: bool = False,
    tool_failed: bool = False,
) -> bool:
    """Persist one metadata-only observation and atomically trip the global kill switch.

    Returns the resulting ``canary_enabled`` state. Raw prompts and answers are
    intentionally excluded from this operational table.
    """
    settings = get_settings()
    now = utcnow()
    cutoff = now - timedelta(seconds=settings.canary_rollback_window_seconds)
    with SessionLocal() as db:
        control = get_control(db, lock=True)
        db.add(
            CanaryObservation(
                experiment_group=assignment.group,
                model_version=assignment.model_version,
                embedding_version=assignment.embedding_version,
                embedding_model=assignment.embedding_model,
                vector_index_version=assignment.vector_index_version,
                prompt_version=assignment.prompt_version,
                rag_version=assignment.rag_version,
                service_version=assignment.service_version,
                success=success,
                timed_out=timed_out,
                tool_failed=tool_failed,
                ttft_ms=ttft_ms,
                latency_ms=max(0, latency_ms),
                error_code=(error_code or "")[:80] or None,
            )
        )
        db.flush()
        observations = list(
            db.scalars(
                select(CanaryObservation)
                .where(CanaryObservation.created_at >= cutoff)
                .order_by(CanaryObservation.created_at.desc())
                .limit(max(settings.canary_rollback_min_samples * 20, 1000))
            ).all()
        )
        if control.canary_enabled and len(observations) >= settings.canary_rollback_min_samples:
            sample_count = len(observations)
            error_rate = sum(not item.success for item in observations) / sample_count
            timeout_rate = sum(item.timed_out for item in observations) / sample_count
            tool_failure_rate = sum(item.tool_failed for item in observations) / sample_count
            ttft_p95 = _p95([item.ttft_ms for item in observations if item.ttft_ms is not None])
            breaches = []
            if error_rate >= settings.canary_rollback_error_rate:
                breaches.append(f"error_rate={error_rate:.3f}")
            if timeout_rate >= settings.canary_rollback_timeout_rate:
                breaches.append(f"timeout_rate={timeout_rate:.3f}")
            if tool_failure_rate >= settings.canary_rollback_tool_failure_rate:
                breaches.append(f"tool_failure_rate={tool_failure_rate:.3f}")
            if ttft_p95 is not None and ttft_p95 >= settings.canary_rollback_ttft_p95_ms:
                breaches.append(f"ttft_p95_ms={ttft_p95}")
            if breaches:
                previous = _control_snapshot(control)
                control.canary_enabled = False
                control.disabled_reason = "automatic: " + ", ".join(breaches)
                control.disabled_at = now
                control.version += 1
                db.add(
                    ExperimentControlAudit(
                        admin_id=None,
                        action="automatic_rollback",
                        from_version=control.version - 1,
                        to_version=control.version,
                        previous_config=previous,
                        next_config=_control_snapshot(control),
                        reason=control.disabled_reason,
                    )
                )
        db.commit()
        CANARY_ENABLED.set(1 if control.canary_enabled else 0)
        observe_canary(
            success=success,
            model_version=assignment.model_version,
            service_version=assignment.service_version,
        )
        return control.canary_enabled


def update_control(
    db: Session,
    *,
    expected_version: int,
    canary_enabled: bool,
    reason: str,
    admin_id: str | None = None,
    stage: int | None = None,
    model_canary_percent: int | None = None,
    embedding_canary_percent: int | None = None,
    prompt_canary_percent: int | None = None,
    rag_canary_percent: int | None = None,
    service_canary_percent: int | None = None,
    shadow_canary_percent: int | None = None,
) -> ExperimentControl | None:
    control = get_control(db, lock=True)
    if control.version != expected_version:
        db.rollback()
        return None
    previous = _control_snapshot(control)
    updates = {
        "stage": stage,
        "model_canary_percent": model_canary_percent,
        "embedding_canary_percent": embedding_canary_percent,
        "prompt_canary_percent": prompt_canary_percent,
        "rag_canary_percent": rag_canary_percent,
        "service_canary_percent": service_canary_percent,
        "shadow_canary_percent": shadow_canary_percent,
    }
    effective = {
        field: value if value is not None else getattr(control, field)
        for field, value in updates.items()
        if field.endswith("_percent")
    }
    if canary_enabled:
        settings = get_settings()
        if effective["embedding_canary_percent"] > 0:
            if not settings.embedding_canary_model or not settings.qdrant_canary_collection:
                raise ValueError("启用 Embedding 灰度前必须配置 Canary 模型和独立向量集合")
            if settings.qdrant_canary_collection == settings.qdrant_collection:
                raise ValueError("Stable 与 Canary Embedding 不得共用同一个向量集合")
        if effective["model_canary_percent"] > 0 and not settings.llm_canary_model:
            raise ValueError("启用模型灰度前必须配置 LLM_CANARY_MODEL")
        if effective["service_canary_percent"] > 0 and not settings.llm_canary_base_url:
            raise ValueError("启用服务灰度前必须配置 LLM_CANARY_BASE_URL")
        if effective["shadow_canary_percent"] > 0 and (
            not settings.llm_canary_base_url or not settings.llm_canary_model
        ):
            raise ValueError("启用 Shadow Traffic 前必须配置 Canary 模型与服务地址")
    control.canary_enabled = canary_enabled
    control.disabled_reason = None if canary_enabled else f"manual: {reason}"
    control.disabled_at = None if canary_enabled else utcnow()
    for field, value in updates.items():
        if value is not None:
            setattr(control, field, value)
    control.version += 1
    control.updated_at = utcnow()
    db.add(
        ExperimentControlAudit(
            admin_id=admin_id,
            action="manual_update",
            from_version=expected_version,
            to_version=control.version,
            previous_config=previous,
            next_config=_control_snapshot(control),
            reason=reason,
        )
    )
    db.commit()
    CANARY_ENABLED.set(1 if control and control.canary_enabled else 0)
    return control
