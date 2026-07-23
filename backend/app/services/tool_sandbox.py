from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


logger = logging.getLogger(__name__)
JsonObject = dict[str, Any]
ToolHandler = Callable[[JsonObject], Awaitable[JsonObject]]
ToolValidator = Callable[[JsonObject], None]


class ToolPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class ToolSpec:
    name: str
    handler: ToolHandler
    validator: ToolValidator
    allowed_roles: frozenset[str] = frozenset({"user", "admin"})
    timeout_seconds: float = 8
    max_input_bytes: int = 16_384
    max_output_bytes: int = 65_536


class ToolSandbox:
    """In-process policy boundary for pure tools; it never exposes shell or dynamic imports.

    Side-effecting/untrusted MCP tools must run in a separate OS sandbox. This class
    still enforces the application contract before any such adapter is invoked.
    """

    def __init__(self, max_concurrency: int = 16) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._semaphore = asyncio.Semaphore(max_concurrency)

    def register(self, spec: ToolSpec) -> None:
        if not spec.name or spec.name in self._tools:
            raise ToolPolicyError("tool name is empty or already registered")
        self._tools[spec.name] = spec

    async def execute(self, name: str, payload: JsonObject, *, role: str, user_id: str) -> JsonObject:
        spec = self._tools.get(name)
        if spec is None:
            raise ToolPolicyError("tool is not allowlisted")
        if role not in spec.allowed_roles:
            raise ToolPolicyError("role is not allowed to call this tool")
        encoded_input = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded_input) > spec.max_input_bytes:
            raise ToolPolicyError("tool input exceeds size limit")
        spec.validator(payload)
        call_id = str(uuid.uuid4())
        started = time.perf_counter()
        status = "ok"
        try:
            async with self._semaphore:
                result = await asyncio.wait_for(spec.handler(payload), timeout=spec.timeout_seconds)
            encoded_output = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(encoded_output) > spec.max_output_bytes:
                raise ToolPolicyError("tool output exceeds size limit")
            return result
        except Exception:
            status = "error"
            raise
        finally:
            logger.info(
                "tool_call name=%s call_id=%s user_id=%s input_sha256=%s status=%s duration_ms=%s",
                name,
                call_id,
                user_id,
                hashlib.sha256(encoded_input).hexdigest(),
                status,
                round((time.perf_counter() - started) * 1000),
            )
