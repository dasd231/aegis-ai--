from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.database import get_db
from app.models import (
    ConversationSession,
    ConversationTask,
    Feedback,
    HandoffTicket,
    Message,
    MessageRole,
    ProfileFactSource,
    SessionMemory,
    TaskStatus,
    User,
    UserProfile,
    UserProfileFact,
    utcnow,
)
from app.schemas import (
    UserProfileFactCreate,
    UserProfileFactOut,
    UserProfileOut,
    UserProfileUpdate,
    UserServiceSummary,
)
from app.services.user_profile import (
    active_profile_facts,
    revoke_inferred_profile_facts,
    upsert_profile_fact,
)


router = APIRouter(prefix="/profile", tags=["用户画像与记忆"])


def _get_or_create(db: Session, user_id: str) -> UserProfile:
    profile = db.get(UserProfile, user_id)
    if profile is None:
        profile = UserProfile(user_id=user_id)
        db.add(profile)
        db.commit()
        db.refresh(profile)
    return profile


def _service_summary(db: Session, user_id: str) -> UserServiceSummary:
    total_questions = db.scalar(
        select(func.count())
        .select_from(Message)
        .join(ConversationSession, ConversationSession.id == Message.session_id)
        .where(ConversationSession.user_id == user_id, Message.role == MessageRole.USER)
    ) or 0
    positive_feedback = db.scalar(
        select(func.count()).select_from(Feedback).where(
            Feedback.user_id == user_id, Feedback.rating == 1
        )
    ) or 0
    negative_feedback = db.scalar(
        select(func.count()).select_from(Feedback).where(
            Feedback.user_id == user_id, Feedback.rating == -1
        )
    ) or 0
    intent_rows = db.execute(
        select(Message.intent, func.count().label("count"))
        .join(ConversationSession, ConversationSession.id == Message.session_id)
        .where(
            ConversationSession.user_id == user_id,
            Message.role == MessageRole.USER,
            Message.intent.is_not(None),
        )
        .group_by(Message.intent)
        .order_by(func.count().desc(), Message.intent.asc())
        .limit(3)
    ).all()
    last_interaction = db.scalar(
        select(func.max(Message.created_at))
        .join(ConversationSession, ConversationSession.id == Message.session_id)
        .where(ConversationSession.user_id == user_id)
    )
    return UserServiceSummary(
        total_questions=total_questions,
        positive_feedback=positive_feedback,
        negative_feedback=negative_feedback,
        handoff_count=db.scalar(
            select(func.count()).select_from(HandoffTicket).where(HandoffTicket.user_id == user_id)
        )
        or 0,
        active_task_count=db.scalar(
            select(func.count()).select_from(ConversationTask).where(
                ConversationTask.user_id == user_id,
                ConversationTask.status == TaskStatus.ACTIVE,
            )
        )
        or 0,
        top_intents=[str(row.intent) for row in intent_rows],
        last_interaction_at=last_interaction,
    )


def _profile_payload(db: Session, profile: UserProfile) -> dict:
    return {
        "user_id": profile.user_id,
        "preferred_name": profile.preferred_name,
        "locale": profile.locale,
        "timezone": profile.timezone,
        "response_style": profile.response_style,
        "memory_consent": profile.memory_consent,
        "consent_version": profile.consent_version,
        "profile_learning_consent": profile.profile_learning_consent,
        "profile_consent_version": profile.profile_consent_version,
        "facts": active_profile_facts(db, profile.user_id),
        "service_summary": _service_summary(db, profile.user_id),
        "created_at": profile.created_at,
        "updated_at": profile.updated_at,
    }


@router.get("", response_model=UserProfileOut)
def get_profile(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _profile_payload(db, _get_or_create(db, user.id))


@router.put("", response_model=UserProfileOut)
def update_profile(
    payload: UserProfileUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    profile = _get_or_create(db, user.id)
    for field, value in payload.model_dump().items():
        setattr(profile, field, value)
    if not payload.memory_consent:
        session_ids = select(ConversationSession.id).where(ConversationSession.user_id == user.id)
        db.execute(delete(SessionMemory).where(SessionMemory.session_id.in_(session_ids)))
    if not payload.profile_learning_consent:
        revoke_inferred_profile_facts(db, user.id)
    db.commit()
    db.refresh(profile)
    return _profile_payload(db, profile)


@router.post("/facts", response_model=UserProfileFactOut, status_code=status.HTTP_201_CREATED)
def create_profile_fact(
    payload: UserProfileFactCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        fact = upsert_profile_fact(
            db,
            user_id=user.id,
            fact_type=payload.fact_type,
            value=payload.value,
            source=ProfileFactSource.EXPLICIT,
            confidence=10000,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    db.commit()
    db.refresh(fact)
    return fact


@router.delete("/facts/{fact_id}", status_code=status.HTTP_204_NO_CONTENT)
def forget_profile_fact(
    fact_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    fact = db.scalar(
        select(UserProfileFact).where(
            UserProfileFact.id == fact_id,
            UserProfileFact.user_id == user.id,
            UserProfileFact.revoked_at.is_(None),
        )
    )
    if fact is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="画像事实不存在")
    fact.revoked_at = utcnow()
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/memory", status_code=status.HTTP_204_NO_CONTENT)
def forget_long_term_memory(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    profile = _get_or_create(db, user.id)
    profile.memory_consent = False
    profile.consent_version = None
    session_ids = select(ConversationSession.id).where(ConversationSession.user_id == user.id)
    db.execute(delete(SessionMemory).where(SessionMemory.session_id.in_(session_ids)))
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
