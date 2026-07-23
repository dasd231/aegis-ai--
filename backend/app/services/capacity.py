from __future__ import annotations

import asyncio
import json

from app.core.config import get_settings


class CapacityState:
    def __init__(self) -> None:
        settings = get_settings()
        self.limit = settings.max_inflight_requests
        self.queue_timeout = settings.request_queue_timeout_seconds
        self.active = 0
        self.rejected = 0
        self._semaphore = asyncio.Semaphore(self.limit)

    async def acquire(self) -> bool:
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=self.queue_timeout)
        except TimeoutError:
            self.rejected += 1
            return False
        self.active += 1
        return True

    def release(self) -> None:
        self.active -= 1
        self._semaphore.release()


capacity_state = CapacityState()


class CapacityMiddleware:
    """ASGI-level bulkhead that keeps SSE requests counted until their body closes."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path", "") in {
            "/api/v1/health/live",
            "/api/v1/health/ready",
        }:
            await self.app(scope, receive, send)
            return
        if not await capacity_state.acquire():
            payload = json.dumps({"detail": "服务繁忙，请稍后重试"}, ensure_ascii=False).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 503,
                    "headers": [
                        (b"content-type", b"application/json; charset=utf-8"),
                        (b"retry-after", b"2"),
                        (b"content-length", str(len(payload)).encode("ascii")),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": payload})
            return
        try:
            await self.app(scope, receive, send)
        finally:
            capacity_state.release()


def capacity_snapshot() -> dict[str, int | float]:
    return {
        "active": capacity_state.active,
        "limit": capacity_state.limit,
        "utilization": round(capacity_state.active / capacity_state.limit, 4),
        "rejected_total": capacity_state.rejected,
    }
