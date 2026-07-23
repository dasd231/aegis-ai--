from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import uuid

from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.database import SessionLocal
from app.models import ConversationSession, SessionStatus, utcnow


@dataclass(frozen=True)
class SessionTurnLease:
    session_id: str
    token: str


def acquire_session_turn(db: Session, session_id: str, user_id: str) -> SessionTurnLease | None:
    """Atomically acquire a cross-process lease for one conversation turn."""
    now = utcnow()
    token = str(uuid.uuid4())
    expires_at = now + timedelta(seconds=get_settings().chat_turn_lease_seconds)
    result = db.execute(
        update(ConversationSession)
        .where(
            ConversationSession.id == session_id,
            ConversationSession.user_id == user_id,
            ConversationSession.status == SessionStatus.ACTIVE,
            or_(
                ConversationSession.turn_lock_token.is_(None),
                ConversationSession.turn_lock_expires_at.is_(None),
                ConversationSession.turn_lock_expires_at < now,
            ),
        )
        .values(turn_lock_token=token, turn_lock_expires_at=expires_at)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        return None
    db.commit()
    return SessionTurnLease(session_id=session_id, token=token)


def renew_session_turn(lease: SessionTurnLease) -> bool:
    expires_at = utcnow() + timedelta(seconds=get_settings().chat_turn_lease_seconds)
    with SessionLocal() as db:
        result = db.execute(
            update(ConversationSession)
            .where(
                ConversationSession.id == lease.session_id,
                ConversationSession.turn_lock_token == lease.token,
            )
            .values(turn_lock_expires_at=expires_at)
            .execution_options(synchronize_session=False)
        )
        db.commit()
        return result.rowcount == 1


def release_session_turn(lease: SessionTurnLease) -> bool:
    with SessionLocal() as db:
        result = db.execute(
            update(ConversationSession)
            .where(
                ConversationSession.id == lease.session_id,
                ConversationSession.turn_lock_token == lease.token,
            )
            .values(turn_lock_token=None, turn_lock_expires_at=None)
            .execution_options(synchronize_session=False)
        )
        db.commit()
        return result.rowcount == 1
