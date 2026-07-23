from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import decode_access_token
from app.database import get_db
from app.models import DailyUsage, User, UserRole


bearer = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="需要登录", headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = decode_access_token(credentials.credentials)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="登录状态已失效",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    user = db.get(User, payload["sub"])
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在或已停用")
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != UserRole.ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return user


def business_date():
    settings = get_settings()
    return datetime.now(ZoneInfo(settings.business_timezone)).date()


def consume_question_quota(db: Session, user_id: str) -> tuple[bool, int]:
    """Atomically increment the counter without allowing concurrent overrun."""
    settings = get_settings()
    usage_date = business_date()
    increment = (
        update(DailyUsage)
        .where(
            DailyUsage.user_id == user_id,
            DailyUsage.usage_date == usage_date,
            DailyUsage.question_count < settings.daily_question_limit,
        )
        .values(question_count=DailyUsage.question_count + 1)
    )
    result = db.execute(increment)
    if result.rowcount == 1:
        count = db.scalar(
            select(DailyUsage.question_count).where(
                DailyUsage.user_id == user_id, DailyUsage.usage_date == usage_date
            )
        )
        db.commit()
        return True, int(count or 0)

    existing = db.scalar(
        select(DailyUsage.question_count).where(
            DailyUsage.user_id == user_id, DailyUsage.usage_date == usage_date
        )
    )
    if existing is not None:
        db.rollback()
        return False, int(existing)

    try:
        db.add(DailyUsage(user_id=user_id, usage_date=usage_date, question_count=1))
        db.commit()
        return True, 1
    except IntegrityError:
        # Another request inserted the row between our update and insert.
        db.rollback()
        result = db.execute(increment)
        count = db.scalar(
            select(DailyUsage.question_count).where(
                DailyUsage.user_id == user_id, DailyUsage.usage_date == usage_date
            )
        )
        if result.rowcount == 1:
            db.commit()
            return True, int(count or 0)
        db.rollback()
        return False, int(count or settings.daily_question_limit)
