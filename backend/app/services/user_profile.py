from __future__ import annotations

from datetime import timedelta
import re

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import (
    ProfileFactSource,
    UserProfile,
    UserProfileFact,
    utcnow,
)


ALLOWED_FACT_TYPES = frozenset(
    {"device_model", "purchase_channel", "preferred_contact_channel", "service_region"}
)
ENUM_FACT_VALUES: dict[str, frozenset[str]] = {
    "purchase_channel": frozenset(
        {"xiaomi_store", "xiaomi_home", "third_party", "enterprise_group"}
    ),
    "preferred_contact_channel": frozenset({"chat", "phone", "email"}),
}
SENSITIVE_MARKERS = (
    "密码",
    "验证码",
    "身份证",
    "银行卡",
    "api key",
    "apikey",
    "token",
)


def normalize_fact_value(fact_type: str, value: str) -> tuple[str, str]:
    if fact_type not in ALLOWED_FACT_TYPES:
        raise ValueError("不支持该画像事实类型")
    cleaned = re.sub(r"\s+", " ", value).strip()
    if not 1 <= len(cleaned) <= 160:
        raise ValueError("画像事实内容长度无效")
    lowered = cleaned.casefold()
    if any(marker in lowered for marker in SENSITIVE_MARKERS):
        raise ValueError("画像不能保存密码、验证码、证件、银行卡或密钥信息")
    if "@" in cleaned or re.search(r"(?<!\d)1[3-9]\d{9}(?!\d)", cleaned):
        raise ValueError("画像不能保存邮箱或完整手机号")
    if re.search(r"\d{9,}", cleaned):
        raise ValueError("画像不能保存长数字标识、订单号或证件号")
    allowed = ENUM_FACT_VALUES.get(fact_type)
    if allowed is not None and lowered not in allowed:
        raise ValueError("画像事实值不在允许范围内")
    return cleaned, lowered


def active_profile_facts(
    db: Session,
    user_id: str,
    *,
    limit: int = 30,
) -> list[UserProfileFact]:
    now = utcnow()
    return list(
        db.scalars(
            select(UserProfileFact)
            .where(
                UserProfileFact.user_id == user_id,
                UserProfileFact.revoked_at.is_(None),
                or_(UserProfileFact.expires_at.is_(None), UserProfileFact.expires_at > now),
            )
            .order_by(
                UserProfileFact.confidence.desc(),
                UserProfileFact.last_confirmed_at.desc(),
            )
            .limit(limit)
        ).all()
    )


def profile_retrieval_hints(db: Session, user_id: str) -> dict[str, str]:
    """Return one highest-confidence active value per retrieval-safe facet."""
    hints: dict[str, str] = {}
    for fact in active_profile_facts(db, user_id, limit=20):
        if fact.fact_type in {"device_model", "purchase_channel", "service_region"}:
            hints.setdefault(fact.fact_type, fact.value)
    return hints


def apply_profile_retrieval_hints(question: str, hints: dict[str, str]) -> str:
    """Qualify anaphoric questions without overriding explicit current facts."""
    additions: list[str] = []
    if (
        hints.get("device_model")
        and re.search(r"(?:我的|这台|这个).{0,4}(?:手机|设备)|(?:手机|设备).{0,4}(?:我的|这台)", question)
        and not re.search(r"(?:小米|redmi|红米)\s*(?:note\s*)?\d", question, re.IGNORECASE)
    ):
        additions.append(f"设备型号={hints['device_model']}")
    if (
        hints.get("purchase_channel")
        and re.search(r"订单|退货|换货|退款|保修|售后|买的", question)
        and not re.search(r"小米商城|小米官网|小米之家|线下|京东|天猫|淘宝|拼多多|第三方|政企|团购", question)
    ):
        additions.append(f"购买渠道={hints['purchase_channel']}")
    if not additions:
        return question
    return f"{question}\n用户已确认的检索条件：{'；'.join(additions)}"


def upsert_profile_fact(
    db: Session,
    *,
    user_id: str,
    fact_type: str,
    value: str,
    source: ProfileFactSource,
    confidence: int,
    source_message_id: str | None = None,
    ttl_days: int | None = None,
) -> UserProfileFact:
    cleaned, normalized = normalize_fact_value(fact_type, value)
    now = utcnow()
    existing = db.scalar(
        select(UserProfileFact).where(
            UserProfileFact.user_id == user_id,
            UserProfileFact.fact_type == fact_type,
            UserProfileFact.normalized_value == normalized,
        )
    )
    expires_at = now + timedelta(days=ttl_days) if ttl_days else None
    if existing is None:
        existing = UserProfileFact(
            user_id=user_id,
            fact_type=fact_type,
            value=cleaned,
            normalized_value=normalized,
            source=source,
            source_message_id=source_message_id,
            confidence=max(0, min(10000, confidence)),
            expires_at=expires_at,
            last_confirmed_at=now,
        )
        db.add(existing)
    else:
        # Explicit confirmation always wins over an inference. Repeated
        # conversational evidence raises confidence gradually but never to 1.0.
        existing.value = cleaned
        if source == ProfileFactSource.EXPLICIT:
            existing.source = source
            existing.confidence = 10000
            existing.expires_at = None
            existing.source_message_id = None
        elif existing.source != ProfileFactSource.EXPLICIT:
            existing.source = source
            existing.confidence = min(9500, max(existing.confidence, confidence) + 300)
            existing.expires_at = expires_at
            existing.source_message_id = source_message_id
        existing.revoked_at = None
        existing.last_confirmed_at = now
        existing.updated_at = now
    return existing


def revoke_inferred_profile_facts(db: Session, user_id: str) -> None:
    now = utcnow()
    for fact in db.scalars(
        select(UserProfileFact).where(
            UserProfileFact.user_id == user_id,
            UserProfileFact.source == ProfileFactSource.CONVERSATION,
            UserProfileFact.revoked_at.is_(None),
        )
    ):
        fact.revoked_at = now


def learn_profile_facts(
    db: Session,
    *,
    user_id: str,
    message_id: str,
    text: str,
) -> list[UserProfileFact]:
    """Extract only low-risk, deterministic facts after separate consent."""
    profile = db.get(UserProfile, user_id)
    if profile is None or not profile.profile_learning_consent:
        return []
    learned: list[UserProfileFact] = []
    if re.search(r"(?:我|我的|这台|手上|买的|购买)", text):
        model_match = re.search(
            r"((?:小米|redmi|红米)\s*(?:note\s*)?(?:\d{1,3}[a-z]*|mix\s*[a-z0-9]+)"
            r"(?:\s*(?:pro|max|ultra|青春版|至尊版|\+))?)",
            text,
            flags=re.IGNORECASE,
        )
        if model_match:
            learned.append(
                upsert_profile_fact(
                    db,
                    user_id=user_id,
                    fact_type="device_model",
                    value=model_match.group(1),
                    source=ProfileFactSource.CONVERSATION,
                    confidence=8500,
                    source_message_id=message_id,
                    ttl_days=180,
                )
            )
        channel_patterns = (
            (r"(?:小米商城|小米官网).{0,8}(?:买|购买|下单)", "xiaomi_store"),
            (r"(?:小米之家|线下门店).{0,8}(?:买|购买|下单)", "xiaomi_home"),
            (r"(?:京东|天猫|淘宝|拼多多|第三方).{0,8}(?:买|购买|下单)", "third_party"),
            (r"(?:政企|团购).{0,8}(?:买|采购|下单)", "enterprise_group"),
        )
        for pattern, channel in channel_patterns:
            if re.search(pattern, text, flags=re.IGNORECASE):
                learned.append(
                    upsert_profile_fact(
                        db,
                        user_id=user_id,
                        fact_type="purchase_channel",
                        value=channel,
                        source=ProfileFactSource.CONVERSATION,
                        confidence=9000,
                        source_message_id=message_id,
                        ttl_days=180,
                    )
                )
                break
    return learned
