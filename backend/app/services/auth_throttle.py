from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import hashlib
import hmac
import math
import secrets

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.database import SessionLocal
from app.models import LoginThrottle, utcnow


@dataclass(frozen=True)
class LoginDecision:
    blocked: bool
    retry_after_seconds: int = 0


def _key(account: str) -> str:
    settings = get_settings()
    normalized = account.strip().casefold()
    return hmac.new(
        settings.secret_key.get_secret_value().encode("utf-8"),
        f"login-account:{normalized}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _decision(locked_until, now) -> LoginDecision:
    if locked_until is None:
        return LoginDecision(False)
    comparable_now = now if locked_until.tzinfo is not None else now.replace(tzinfo=None)
    remaining = (locked_until - comparable_now).total_seconds()
    return LoginDecision(remaining > 0, max(1, math.ceil(remaining)) if remaining > 0 else 0)


def check_login_allowed(account: str) -> LoginDecision:
    now = utcnow()
    with SessionLocal() as db:
        throttle = db.get(LoginThrottle, _key(account))
        return _decision(throttle.locked_until, now) if throttle else LoginDecision(False)


def record_login_failure(account: str) -> LoginDecision:
    settings = get_settings()
    key_hash = _key(account)
    for _ in range(3):
        now = utcnow()
        with SessionLocal() as db:
            throttle = db.scalar(
                select(LoginThrottle).where(LoginThrottle.key_hash == key_hash).with_for_update()
            )
            if throttle is None:
                throttle = LoginThrottle(key_hash=key_hash, failures=1, window_started_at=now)
                db.add(throttle)
            else:
                comparable_now = now if throttle.window_started_at.tzinfo is not None else now.replace(tzinfo=None)
                window_age = comparable_now - throttle.window_started_at
                if window_age >= timedelta(seconds=settings.login_failure_window_seconds):
                    throttle.failures = 1
                    throttle.window_started_at = now
                    throttle.locked_until = None
                else:
                    throttle.failures += 1
            if throttle.failures >= settings.login_max_failures:
                throttle.locked_until = now + timedelta(seconds=settings.login_lock_seconds)
            throttle.updated_at = now
            # Unknown-account attacks otherwise create unbounded rows. An indexed,
            # sampled cleanup keeps stale HMAC buckets bounded without a hot full scan.
            if secrets.randbelow(100) == 0:
                stale_before = now - timedelta(
                    seconds=max(settings.login_failure_window_seconds, settings.login_lock_seconds) * 2
                )
                db.execute(delete(LoginThrottle).where(LoginThrottle.updated_at < stale_before))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                continue
            return _decision(throttle.locked_until, now)
    # Only a first-insert race reaches this path. Fail closed briefly so a burst
    # cannot exploit database contention to bypass protection.
    return LoginDecision(True, 1)


def clear_login_failures(account: str) -> None:
    with SessionLocal() as db:
        db.execute(delete(LoginThrottle).where(LoginThrottle.key_hash == _key(account)))
        db.commit()
