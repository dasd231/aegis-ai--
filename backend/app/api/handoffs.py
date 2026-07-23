from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_admin
from app.database import get_db
from app.models import (
    ConversationSession,
    HandoffStatus,
    HandoffTicket,
    HandoffTrigger,
    Message,
    User,
    UserRole,
)
from app.schemas import HandoffAdminUpdate, HandoffCreate, HandoffOut


router = APIRouter(tags=["人工接管"])


def _conversation_snapshot(db: Session, session_id: str) -> str:
    rows = db.execute(
        select(Message.role, Message.content)
        .where(Message.session_id == session_id)
        .order_by(Message.created_at.desc())
        .limit(20)
    ).all()
    lines = []
    for role, content in reversed(rows):
        normalized = re.sub(r"\s+", " ", content).strip()
        lines.append(f"{role.value}: {normalized[:500]}")
    return "\n".join(lines)[-6000:] or "暂无对话内容"


@router.post("/handoffs", response_model=HandoffOut, status_code=status.HTTP_201_CREATED)
def request_handoff(
    payload: HandoffCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if payload.trigger != HandoffTrigger.MANUAL:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="用户只能主动发起人工接管")
    conversation = db.scalar(
        select(ConversationSession).where(
            ConversationSession.id == payload.session_id,
            ConversationSession.user_id == user.id,
        )
    )
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")
    existing = db.scalar(
        select(HandoffTicket).where(
            HandoffTicket.session_id == payload.session_id,
            HandoffTicket.user_id == user.id,
            HandoffTicket.status.in_([HandoffStatus.OPEN, HandoffStatus.IN_PROGRESS]),
        )
    )
    if existing is not None:
        return existing
    ticket = HandoffTicket(
        session_id=payload.session_id,
        active_dedupe_key=payload.session_id,
        user_id=user.id,
        trigger=payload.trigger,
        reason=payload.reason,
        priority=payload.priority,
        conversation_summary=_conversation_snapshot(db, payload.session_id),
    )
    db.add(ticket)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        concurrent = db.scalar(
            select(HandoffTicket).where(
                HandoffTicket.active_dedupe_key == payload.session_id,
                HandoffTicket.user_id == user.id,
            )
        )
        if concurrent is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="工单状态冲突，请刷新") from None
        return concurrent
    db.refresh(ticket)
    return ticket


@router.get("/handoffs/mine", response_model=list[HandoffOut])
def my_handoffs(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return list(
        db.scalars(
            select(HandoffTicket)
            .where(HandoffTicket.user_id == user.id)
            .order_by(HandoffTicket.created_at.desc())
            .limit(100)
        ).all()
    )


@router.get("/admin/handoffs", response_model=list[HandoffOut])
def admin_handoffs(
    ticket_status: HandoffStatus | None = Query(default=None, alias="status"),
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    query = select(HandoffTicket)
    if ticket_status is not None:
        query = query.where(HandoffTicket.status == ticket_status)
    query = query.order_by(HandoffTicket.priority.desc(), HandoffTicket.created_at.asc()).limit(200)
    return list(db.scalars(query).all())


@router.patch("/admin/handoffs/{ticket_id}", response_model=HandoffOut)
def update_handoff(
    ticket_id: str,
    payload: HandoffAdminUpdate,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    ticket = db.get(HandoffTicket, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="人工工单不存在")
    if ticket.version != payload.expected_version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="工单已被其他管理员更新，请刷新后重试",
        )
    if ticket.status == HandoffStatus.CLOSED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="已关闭工单不能继续修改")
    transitions = {
        HandoffStatus.OPEN: {HandoffStatus.IN_PROGRESS, HandoffStatus.CLOSED},
        HandoffStatus.IN_PROGRESS: {HandoffStatus.RESOLVED, HandoffStatus.CLOSED},
        HandoffStatus.RESOLVED: {HandoffStatus.IN_PROGRESS, HandoffStatus.CLOSED},
    }
    target_status = payload.status or ticket.status
    if target_status != ticket.status and target_status not in transitions[ticket.status]:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="不允许执行该工单状态流转")

    assigned_to = payload.assigned_to if payload.assigned_to is not None else ticket.assigned_to
    if payload.assigned_to is not None:
        assignee = db.get(User, payload.assigned_to)
        if assignee is None or assignee.role != UserRole.ADMIN or not assignee.is_active:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="处理人必须是有效管理员")
    if target_status == HandoffStatus.IN_PROGRESS and assigned_to is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="接管工单前必须指定处理人")
    resolution = payload.resolution if payload.resolution is not None else ticket.resolution
    if target_status == HandoffStatus.RESOLVED and not resolution:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="解决工单前必须填写处理结论")

    values = {
        "version": HandoffTicket.version + 1,
        "active_dedupe_key": (
            ticket.session_id if target_status in {HandoffStatus.OPEN, HandoffStatus.IN_PROGRESS} else None
        ),
    }
    if payload.status is not None:
        values["status"] = payload.status
    if payload.assigned_to is not None:
        values["assigned_to"] = payload.assigned_to
    if payload.resolution is not None:
        values["resolution"] = payload.resolution
    try:
        changed = db.execute(
            update(HandoffTicket)
            .where(HandoffTicket.id == ticket_id, HandoffTicket.version == payload.expected_version)
            .values(**values)
        )
        if changed.rowcount != 1:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="工单已被其他管理员更新，请刷新后重试",
            )
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该会话已有活跃人工工单") from None
    return db.get(HandoffTicket, ticket_id)
