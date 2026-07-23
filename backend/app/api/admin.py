from __future__ import annotations

from datetime import timedelta
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, selectinload

from app.api.deps import business_date, require_admin
from app.database import get_db
from app.models import (
    AdminDataAccessAudit,
    ConversationSession,
    DailyUsage,
    ExperimentControlAudit,
    Feedback,
    Message,
    ProviderCredential,
    ProviderCredentialAudit,
    User,
    utcnow,
)
from app.schemas import (
    AdminDataAccessAuditOut,
    AdminOverview,
    AdminSessionDetail,
    AdminSessionPage,
    ExperimentControlOut,
    ExperimentControlAuditOut,
    ExperimentControlUpdate,
    ProviderCredentialAuditOut,
    ProviderCredentialOut,
    ProviderCredentialUpdate,
)
from app.services.canary_control import get_control, update_control
from app.services.provider_credentials import (
    SUPPORTED_CREDENTIAL_IDS,
    RuntimeProvider,
    api_key_fingerprint,
    decrypt_api_key,
    encrypt_api_key,
    list_provider_credentials,
    public_credential,
    validate_provider_url,
    validate_runtime_provider,
)


router = APIRouter(prefix="/admin", tags=["管理后台"])
logger = logging.getLogger(__name__)


@router.get("/overview", response_model=AdminOverview)
def overview(_: User = Depends(require_admin), db: Session = Depends(get_db)):
    today = business_date()
    start = today - timedelta(days=6)
    rows = db.execute(
        select(DailyUsage.usage_date, func.sum(DailyUsage.question_count))
        .where(DailyUsage.usage_date >= start)
        .group_by(DailyUsage.usage_date)
    ).all()
    counts = {day: int(count) for day, count in rows}
    series = [
        {"date": (start + timedelta(days=index)).isoformat(), "count": counts.get(start + timedelta(days=index), 0)}
        for index in range(7)
    ]
    return AdminOverview(
        users=db.scalar(select(func.count()).select_from(User)) or 0,
        sessions=db.scalar(select(func.count()).select_from(ConversationSession)) or 0,
        questions_today=db.scalar(
            select(func.coalesce(func.sum(DailyUsage.question_count), 0)).where(DailyUsage.usage_date == today)
        ) or 0,
        positive_feedback=db.scalar(select(func.count()).select_from(Feedback).where(Feedback.rating == 1)) or 0,
        negative_feedback=db.scalar(select(func.count()).select_from(Feedback).where(Feedback.rating == -1)) or 0,
        daily_questions=series,
    )


@router.get("/experiments/control", response_model=ExperimentControlOut)
def read_experiment_control(_: User = Depends(require_admin), db: Session = Depends(get_db)):
    control = get_control(db)
    db.commit()
    return control


@router.patch("/experiments/control", response_model=ExperimentControlOut)
def change_experiment_control(
    payload: ExperimentControlUpdate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        control = update_control(
            db,
            expected_version=payload.expected_version,
            canary_enabled=payload.canary_enabled,
            reason=payload.reason,
            admin_id=admin.id,
            stage=payload.stage,
            model_canary_percent=payload.model_canary_percent,
            embedding_canary_percent=payload.embedding_canary_percent,
            prompt_canary_percent=payload.prompt_canary_percent,
            rag_canary_percent=payload.rag_canary_percent,
            service_canary_percent=payload.service_canary_percent,
            shadow_canary_percent=payload.shadow_canary_percent,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    if control is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="灰度控制状态已变化，请刷新后重试",
        )
    logger.warning(
        "experiment_control_changed admin_id=%s enabled=%s version=%s reason=%s",
        admin.id,
        payload.canary_enabled,
        control.version,
        payload.reason,
    )
    return control


@router.get("/experiments/audits", response_model=list[ExperimentControlAuditOut])
def read_experiment_audits(
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
):
    return list(
        db.scalars(
            select(ExperimentControlAudit)
            .order_by(ExperimentControlAudit.created_at.desc())
            .limit(limit)
        ).all()
    )


@router.get("/providers", response_model=list[ProviderCredentialOut])
def read_provider_credentials(
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return [public_credential(item) for item in list_provider_credentials(db)]


@router.patch("/providers/{credential_id}", response_model=ProviderCredentialOut)
async def change_provider_credential(
    credential_id: str,
    payload: ProviderCredentialUpdate,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if credential_id not in SUPPORTED_CREDENTIAL_IDS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="模型凭证配置不存在")
    record = db.get(ProviderCredential, credential_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="模型凭证配置不存在")
    if record.version != payload.expected_version:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="配置已被其他管理员更新，请刷新后重试")
    if credential_id == "dashscope_ai" and payload.provider != "dashscope":
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="百炼能力配置仅支持 dashscope 协议")
    try:
        base_url = validate_provider_url(payload.base_url)
        previous_key = decrypt_api_key(record.api_key_ciphertext)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    supplied = payload.api_key.get_secret_value().strip() if payload.api_key is not None else ""
    candidate_key = supplied or previous_key
    if not candidate_key:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="必须配置 API Key")
    changed_fields = [
        field
        for field, old, new in (
            ("provider", record.provider, payload.provider),
            ("base_url", record.base_url, base_url),
            ("model", record.model, payload.model),
            ("enabled", record.enabled, payload.enabled),
            ("api_key", record.api_key_fingerprint, api_key_fingerprint(candidate_key)),
        )
        if old != new
    ]
    validation = None
    if payload.enabled or payload.validate_before_save:
        validation = await validate_runtime_provider(
            RuntimeProvider(
                id=credential_id,
                provider=payload.provider,
                base_url=base_url,
                model=payload.model,
                api_key=candidate_key,
                version=record.version + 1,
                enabled=payload.enabled,
            )
        )
        if not validation.ok:
            db.add(
                ProviderCredentialAudit(
                    credential_id=credential_id,
                    admin_id=admin.id,
                    request_id=request.state.request_id,
                    action="validation_failed",
                    from_version=record.version,
                    to_version=record.version,
                    changed_fields=changed_fields,
                    succeeded=False,
                    error_code=validation.error_code,
                )
            )
            db.commit()
            logger.warning(
                "provider_validation_failed admin_id=%s credential_id=%s error_code=%s request_id=%s",
                admin.id,
                credential_id,
                validation.error_code,
                request.state.request_id,
            )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"凭证验证失败（{validation.error_code or 'unknown'}），原配置未变更",
            )

    now = utcnow()
    values = {
        "provider": payload.provider,
        "base_url": base_url,
        "model": payload.model,
        "api_key_ciphertext": encrypt_api_key(candidate_key),
        "api_key_fingerprint": api_key_fingerprint(candidate_key),
        "enabled": payload.enabled,
        "validation_status": "valid" if validation else "unverified",
        "last_error_code": None,
        "last_validated_at": now if validation else None,
        "version": record.version + 1,
        "updated_by": admin.id,
        "updated_at": now,
    }
    result = db.execute(
        update(ProviderCredential)
        .where(ProviderCredential.id == credential_id, ProviderCredential.version == record.version)
        .values(**values)
    )
    if result.rowcount != 1:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="配置已被其他管理员更新，请刷新后重试")
    db.add(
        ProviderCredentialAudit(
            credential_id=credential_id,
            admin_id=admin.id,
            request_id=request.state.request_id,
            action="validated_update" if validation else "disabled_draft_update",
            from_version=record.version,
            to_version=record.version + 1,
            changed_fields=changed_fields,
            succeeded=True,
        )
    )
    db.commit()
    updated = db.get(ProviderCredential, credential_id)
    logger.warning(
        "provider_credential_changed admin_id=%s credential_id=%s version=%s fields=%s request_id=%s",
        admin.id,
        credential_id,
        updated.version if updated else None,
        changed_fields,
        request.state.request_id,
    )
    return public_credential(updated)


@router.get("/providers/audits", response_model=list[ProviderCredentialAuditOut])
def read_provider_credential_audits(
    limit: int = Query(default=50, ge=1, le=200),
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return list(
        db.scalars(
            select(ProviderCredentialAudit)
            .order_by(ProviderCredentialAudit.created_at.desc())
            .limit(limit)
        )
    )


@router.get("/sessions", response_model=AdminSessionPage)
def list_all_sessions(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    total = db.scalar(select(func.count()).select_from(ConversationSession)) or 0
    message_count = (
        select(func.count(Message.id))
        .where(Message.session_id == ConversationSession.id)
        .correlate(ConversationSession)
        .scalar_subquery()
    )
    rows = db.execute(
        select(
            ConversationSession,
            User.display_name,
            message_count.label("message_count"),
        )
        .join(User, User.id == ConversationSession.user_id)
        .order_by(ConversationSession.updated_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    logger.info("admin_session_list admin_id=%s page=%s page_size=%s", admin.id, page, page_size)
    db.add(
        AdminDataAccessAudit(
            admin_id=admin.id,
            action="session_list",
            target_type="conversation_session",
            target_id=None,
            request_id=getattr(request.state, "request_id", None),
            purpose="客服质量与运营审阅",
        )
    )
    db.commit()
    return AdminSessionPage(
        items=[
            {
                "id": conversation.id,
                "user_id": conversation.user_id,
                "user_display_name": display_name,
                "title": conversation.title,
                "status": conversation.status,
                "message_count": int(message_count),
                "created_at": conversation.created_at,
                "updated_at": conversation.updated_at,
            }
            for conversation, display_name, message_count in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/sessions/{session_id}", response_model=AdminSessionDetail)
def read_any_session(
    session_id: str,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    conversation = db.scalar(
        select(ConversationSession)
        .where(ConversationSession.id == session_id)
        .options(
            selectinload(ConversationSession.messages).selectinload(Message.citations),
            selectinload(ConversationSession.messages).selectinload(Message.feedback),
        )
    )
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")
    owner = db.get(User, conversation.user_id)
    messages = sorted(conversation.messages, key=lambda item: (item.created_at, item.id))
    logger.info("admin_session_read admin_id=%s session_id=%s owner_id=%s", admin.id, session_id, conversation.user_id)
    db.add(
        AdminDataAccessAudit(
            admin_id=admin.id,
            action="session_read",
            target_type="conversation_session",
            target_id=session_id,
            request_id=getattr(request.state, "request_id", None),
            purpose="客服质量与争议处理",
        )
    )
    db.commit()
    return {
        "id": conversation.id,
        "user_id": conversation.user_id,
        "user_display_name": owner.display_name if owner else "[deleted]",
        "title": conversation.title,
        "status": conversation.status,
        "message_count": len(messages),
        "created_at": conversation.created_at,
        "updated_at": conversation.updated_at,
        "messages": messages,
    }


@router.get("/data-access-audits", response_model=list[AdminDataAccessAuditOut])
def read_data_access_audits(
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
    limit: int = Query(default=100, ge=1, le=500),
):
    return list(
        db.scalars(
            select(AdminDataAccessAudit)
            .order_by(AdminDataAccessAudit.created_at.desc())
            .limit(limit)
        ).all()
    )
