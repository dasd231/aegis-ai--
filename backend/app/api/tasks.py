from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.database import get_db
from app.models import ConversationSession, ConversationTask, User
from app.schemas import TaskCreate, TaskOut, TaskUpdate


router = APIRouter(prefix="/sessions/{session_id}/tasks", tags=["任务记忆"])


def _owned_session(db: Session, session_id: str, user_id: str) -> ConversationSession:
    conversation = db.scalar(
        select(ConversationSession).where(
            ConversationSession.id == session_id,
            ConversationSession.user_id == user_id,
        )
    )
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")
    return conversation


@router.post("", response_model=TaskOut, status_code=status.HTTP_201_CREATED)
def create_task(
    session_id: str,
    payload: TaskCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _owned_session(db, session_id, user.id)
    task = ConversationTask(
        session_id=session_id,
        user_id=user.id,
        task_type=payload.task_type,
        slots=payload.slots,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


@router.get("", response_model=list[TaskOut])
def list_tasks(session_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _owned_session(db, session_id, user.id)
    return list(
        db.scalars(
            select(ConversationTask)
            .where(ConversationTask.session_id == session_id, ConversationTask.user_id == user.id)
            .order_by(ConversationTask.updated_at.desc())
        ).all()
    )


@router.patch("/{task_id}", response_model=TaskOut)
def update_task(
    session_id: str,
    task_id: str,
    payload: TaskUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _owned_session(db, session_id, user.id)
    values = {"version": ConversationTask.version + 1}
    if payload.status is not None:
        values["status"] = payload.status
    if payload.slots is not None:
        values["slots"] = payload.slots
    result = db.execute(
        update(ConversationTask)
        .where(
            ConversationTask.id == task_id,
            ConversationTask.session_id == session_id,
            ConversationTask.user_id == user.id,
            ConversationTask.version == payload.expected_version,
        )
        .values(**values)
    )
    if result.rowcount != 1:
        db.rollback()
        exists = db.scalar(
            select(ConversationTask.id).where(
                ConversationTask.id == task_id,
                ConversationTask.session_id == session_id,
                ConversationTask.user_id == user.id,
            )
        )
        if exists is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="任务已被其他请求更新，请刷新后重试")
    db.commit()
    return db.get(ConversationTask, task_id)
