from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.database import get_db
from app.models import ConversationSession, Feedback, Message, MessageRole, User
from app.schemas import FeedbackOut, FeedbackRequest


router = APIRouter(prefix="/messages", tags=["反馈"])


@router.put("/{message_id}/feedback", response_model=FeedbackOut)
def upsert_feedback(
    message_id: str,
    payload: FeedbackRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    message = db.scalar(
        select(Message)
        .join(ConversationSession)
        .where(
            Message.id == message_id,
            Message.role == MessageRole.ASSISTANT,
            ConversationSession.user_id == user.id,
        )
    )
    if message is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="回答不存在")
    feedback = db.scalar(select(Feedback).where(Feedback.message_id == message_id))
    if feedback is None:
        feedback = Feedback(message_id=message_id, user_id=user.id, rating=payload.rating, comment=payload.comment)
        db.add(feedback)
    else:
        feedback.rating = payload.rating
        feedback.comment = payload.comment
    try:
        db.commit()
    except IntegrityError:
        # Two tabs may submit the first rating simultaneously. Resolve to one idempotent row.
        db.rollback()
        feedback = db.scalar(select(Feedback).where(Feedback.message_id == message_id))
        if feedback is None:
            raise
        feedback.rating = payload.rating
        feedback.comment = payload.comment
        db.commit()
    db.refresh(feedback)
    return feedback
