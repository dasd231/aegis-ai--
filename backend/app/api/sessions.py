from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_current_user
from app.database import get_db
from app.models import ConversationSession, Message, User, utcnow
from app.schemas import SessionCreate, SessionDetail, SessionPage, SessionSummary


router = APIRouter(prefix="/sessions", tags=["会话"])


@router.post("", response_model=SessionSummary, status_code=status.HTTP_201_CREATED)
def create_session(payload: SessionCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    session = ConversationSession(user_id=user.id, title=payload.title.strip())
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


@router.get("", response_model=SessionPage)
def list_sessions(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    condition = ConversationSession.user_id == user.id
    total = db.scalar(select(func.count()).select_from(ConversationSession).where(condition)) or 0
    items = db.scalars(
        select(ConversationSession)
        .where(condition)
        .order_by(ConversationSession.updated_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return SessionPage(items=list(items), total=total, page=page, page_size=page_size)


@router.get("/{session_id}", response_model=SessionDetail)
def get_session(session_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    session = db.scalar(
        select(ConversationSession)
        .options(
            selectinload(ConversationSession.messages).selectinload(Message.citations),
            selectinload(ConversationSession.messages).selectinload(Message.feedback),
        )
        .where(ConversationSession.id == session_id, ConversationSession.user_id == user.id)
    )
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")
    return session


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_session(session_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    result = db.execute(
        delete(ConversationSession).where(
            ConversationSession.id == session_id,
            ConversationSession.user_id == user.id,
            or_(
                ConversationSession.turn_lock_token.is_(None),
                ConversationSession.turn_lock_expires_at.is_(None),
                ConversationSession.turn_lock_expires_at < utcnow(),
            ),
        ).execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        exists = db.scalar(
            select(ConversationSession.id).where(
                ConversationSession.id == session_id,
                ConversationSession.user_id == user.id,
            )
        )
        if exists is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="回答生成期间不能删除会话",
            headers={"Retry-After": "2"},
        )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
