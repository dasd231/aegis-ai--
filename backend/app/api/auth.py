from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.security import DUMMY_PASSWORD_HASH, create_access_token, hash_password, verify_password
from app.database import get_db
from app.models import User
from app.schemas import LoginRequest, RegisterRequest, TokenResponse, UserOut
from app.services.auth_throttle import check_login_allowed, clear_login_failures, record_login_failure


router = APIRouter(prefix="/auth", tags=["认证"])


def _token_response(user: User) -> TokenResponse:
    token, expires = create_access_token(user.id, user.role.value)
    return TokenResponse(access_token=token, expires_in=expires, user=UserOut.model_validate(user))


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, db: Session = Depends(get_db)):
    email = str(payload.email).lower() if payload.email else None
    phone = payload.phone
    identity_condition = User.email == email if email is not None else User.phone == phone
    existing = db.scalar(select(User.id).where(identity_condition))
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该账号已注册")
    user = User(
        email=email,
        phone=phone,
        display_name=payload.display_name.strip(),
        password_hash=hash_password(payload.password),
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该账号已注册") from None
    db.refresh(user)
    return _token_response(user)


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    account = payload.account.strip()
    decision = check_login_allowed(account)
    if decision.blocked:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="登录尝试过于频繁，请稍后再试",
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )
    user = db.scalar(select(User).where(or_(User.email == account.lower(), User.phone == account)))
    password_matches = verify_password(payload.password, user.password_hash if user else DUMMY_PASSWORD_HASH)
    if user is None or not user.is_active or not password_matches:
        decision = record_login_failure(account)
        if decision.blocked:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="登录尝试过于频繁，请稍后再试",
                headers={"Retry-After": str(decision.retry_after_seconds)},
            )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="账号或密码错误")
    clear_login_failures(account)
    return _token_response(user)


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user
