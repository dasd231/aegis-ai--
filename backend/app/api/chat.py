from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import consume_question_quota, get_current_user
from app.core.config import get_settings
from app.database import get_db
from app.models import ConversationSession, KnowledgeBase, Message, MessageRole, SessionStatus, User, utcnow
from app.schemas import ChatRequest, UsageOut
from app.services.intent import classify_intent
from app.services.experiment import get_or_assign_experiment
from app.services.memory import load_prompt_memory
from app.services.user_profile import learn_profile_facts, profile_retrieval_hints
from app.services.rag import stream_rag_answer
from app.services.turn_lease import acquire_session_turn, release_session_turn, renew_session_turn


router = APIRouter(prefix="/chat", tags=["智能问答"])


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


async def _heartbeat(source: AsyncIterator[tuple[str, dict]]) -> AsyncIterator[tuple[str, dict]]:
    queue: asyncio.Queue = asyncio.Queue(maxsize=20)
    sentinel = object()

    async def produce():
        try:
            async for item in source:
                await queue.put(item)
        except BaseException as exc:
            await queue.put(("__exception__", {"exception": exc}))
        finally:
            await queue.put(sentinel)

    task = asyncio.create_task(produce())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=12)
            except TimeoutError:
                yield "ping", {"ts": int(asyncio.get_running_loop().time())}
                continue
            if item is sentinel:
                break
            event, data = item
            if event == "__exception__":
                raise data["exception"]
            yield event, data
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@router.get("/usage", response_model=UsageOut)
def usage(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from app.api.deps import business_date
    from app.models import DailyUsage

    settings = get_settings()
    used = db.scalar(
        select(DailyUsage.question_count).where(
            DailyUsage.user_id == user.id, DailyUsage.usage_date == business_date()
        )
    ) or 0
    return UsageOut(used=used, limit=settings.daily_question_limit, remaining=max(0, settings.daily_question_limit - used))


@router.post("/stream")
async def chat_stream(
    payload: ChatRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    settings = get_settings()
    if len(payload.question) > settings.question_max_chars:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"问题不能超过 {settings.question_max_chars} 个字符",
        )
    conversation = db.scalar(
        select(ConversationSession).where(
            ConversationSession.id == payload.session_id,
            ConversationSession.user_id == user.id,
            ConversationSession.status == SessionStatus.ACTIVE,
        )
    )
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")
    if payload.knowledge_base_id:
        knowledge_base = db.get(KnowledgeBase, payload.knowledge_base_id)
        if knowledge_base is None or not knowledge_base.is_active:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="知识库不存在")

    turn_lease = acquire_session_turn(db, conversation.id, user.id)
    if turn_lease is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该会话已有回答正在生成，请等待完成后再提问",
            headers={"Retry-After": "2"},
        )

    try:
        allowed, used = consume_question_quota(db, user.id)
        if not allowed:
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="今日提问次数已达上限")

        intent = classify_intent(payload.question)
        experiment = get_or_assign_experiment(
            db,
            conversation,
            user.id,
            intent,
            tenant_id=user.tenant_id,
        )
        prior_count = db.scalar(
            select(func.count()).select_from(Message).where(Message.session_id == conversation.id)
        ) or 0
        history_rows = db.execute(
            select(Message.role, Message.content)
            .where(Message.session_id == conversation.id)
            .order_by(Message.created_at.desc())
            .limit(settings.history_turns * 2)
        ).all()
        history = [(role.value, content) for role, content in reversed(history_rows)]
        profile_context, long_memory, task_context = load_prompt_memory(db, conversation.id, user.id)
        retrieval_hints = profile_retrieval_hints(db, user.id)
        user_message = Message(
            session_id=conversation.id,
            role=MessageRole.USER,
            content=payload.question,
            intent=intent,
        )
        db.add(user_message)
        db.flush()
        learn_profile_facts(
            db,
            user_id=user.id,
            message_id=user_message.id,
            text=payload.question,
        )
        conversation.updated_at = utcnow()
        if prior_count == 0 and conversation.title == "新对话":
            conversation.title = payload.question[:40] + ("…" if len(payload.question) > 40 else "")
        db.commit()
        db.refresh(user_message)
    except BaseException:
        release_session_turn(turn_lease)
        raise

    async def event_stream():
        try:
            yield _sse(
                "ack",
                {
                    "user_message_id": user_message.id,
                    "session_id": conversation.id,
                    "usage": {"used": used, "limit": settings.daily_question_limit},
                    "experiment_group": experiment.group,
                },
            )
            source = stream_rag_answer(
                conversation.id,
                payload.question,
                intent,
                history,
                payload.knowledge_base_id,
                profile_context=profile_context,
                long_memory=long_memory,
                task_context=task_context,
                profile_retrieval_hints=retrieval_hints,
                experiment=experiment,
                request_id=request.state.request_id,
            )
            async for event, data in _heartbeat(source):
                if await request.is_disconnected():
                    break
                if not await asyncio.to_thread(renew_session_turn, turn_lease):
                    yield _sse(
                        "error",
                        {"code": "SESSION_TURN_LOST", "message": "会话处理权已失效，请刷新会话后重试。"},
                    )
                    break
                yield _sse(event, data)
        finally:
            await asyncio.to_thread(release_session_turn, turn_lease)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
