"""Verify a deployed Chinese RAG/SSE path through the public HTTP endpoint.

Credentials are read from environment variables and are never printed.  The
script creates a temporary conversation and removes it even when an assertion
fails; quota consumption remains an intentional audit record.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


DEFAULT_QUESTION = "小米手机完全无法开机，充电也没有反应，应该先做什么？"
DEFAULT_REQUIRED_TERMS = ("至少充电 30 分钟", "长按电源键 10 秒以上")


@dataclass(frozen=True)
class Event:
    name: str
    data: dict[str, Any]


def parse_sse(body: str) -> list[Event]:
    events: list[Event] = []
    normalized = body.replace("\r\n", "\n")
    for frame in normalized.split("\n\n"):
        name = ""
        data_lines: list[str] = []
        for line in frame.splitlines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if not name or not data_lines:
            continue
        payload = json.loads("\n".join(data_lines))
        if not isinstance(payload, dict):
            raise AssertionError(f"SSE {name!r} data must be a JSON object")
        events.append(Event(name, payload))
    return events


class Client:
    def __init__(self, api_base: str, timeout: float, host_header: str | None = None):
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self.host_header = host_header
        self.token = ""

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        expect_json: bool = True,
    ) -> tuple[Any, str]:
        headers = {"Accept": "application/json"}
        if self.host_header:
            headers["Host"] = self.host_header
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(
            f"{self.api_base}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
                content_type = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:800]
            raise AssertionError(f"{method} {path} returned HTTP {exc.code}: {detail}") from exc
        if expect_json:
            return (json.loads(body) if body else {}), content_type
        return body, content_type


def _one(events: list[Event], name: str) -> Event:
    matches = [event for event in events if event.name == name]
    if len(matches) != 1:
        raise AssertionError(f"expected one {name!r} event, received {len(matches)}")
    return matches[0]


def run(args: argparse.Namespace) -> dict[str, Any]:
    account = os.getenv("SMOKE_ACCOUNT") or os.getenv("INITIAL_ADMIN_EMAIL")
    password = os.getenv("SMOKE_PASSWORD") or os.getenv("INITIAL_ADMIN_PASSWORD")
    if not account or not password:
        raise AssertionError(
            "set SMOKE_ACCOUNT and SMOKE_PASSWORD (or INITIAL_ADMIN_EMAIL and INITIAL_ADMIN_PASSWORD)"
        )

    root = args.base_url.rstrip("/")
    api_base = root if root.endswith("/api/v1") else f"{root}/api/v1"
    client = Client(api_base, args.timeout, args.host_header)
    login, _ = client.request("POST", "/auth/login", {"account": account, "password": password})
    client.token = str(login["access_token"])
    smoke_user = login["user"]
    session, _ = client.request("POST", "/sessions", {"title": "部署后自动验收"})
    session_id = str(session["id"])
    try:
        body, content_type = client.request(
            "POST",
            "/chat/stream",
            {"session_id": session_id, "question": args.question},
            expect_json=False,
        )
        if not content_type.startswith("text/event-stream"):
            raise AssertionError(f"unexpected stream content type: {content_type!r}")
        events = parse_sse(body)
        _one(events, "ack")
        source_event = _one(events, "sources")
        done = _one(events, "done").data
        sources = source_event.data.get("items", [])
        answer = "".join(str(event.data.get("delta", "")) for event in events if event.name == "token")
        source_names = [str(source.get("document_name", "")) for source in sources]
        if args.expected_source and not any(args.expected_source in name for name in source_names):
            raise AssertionError(f"expected source containing {args.expected_source!r}; got {source_names!r}")
        if not sources or not answer or "[S" not in answer:
            raise AssertionError("stream must contain non-empty sources, answer tokens, and citations")
        required_terms = args.required_answer_term
        if required_terms is None and args.question == DEFAULT_QUESTION:
            required_terms = list(DEFAULT_REQUIRED_TERMS)
        missing_terms = [term for term in (required_terms or []) if term not in answer]
        if missing_terms:
            raise AssertionError(f"answer is missing required grounded terms: {missing_terms!r}")
        if not done.get("grounded") or not done.get("evidence_available"):
            raise AssertionError("done event did not confirm grounded evidence")
        if done.get("answerability") == "unsupported" or not done.get("claims"):
            raise AssertionError("deployed answer was unsupported or had no structured claims")
        handoff, _ = client.request(
            "POST",
            "/handoffs",
            {
                "session_id": session_id,
                "trigger": "manual",
                "reason": "部署冒烟：验证人工接管状态机",
                "priority": 3,
            },
        )
        duplicate_handoff, _ = client.request(
            "POST",
            "/handoffs",
            {
                "session_id": session_id,
                "trigger": "manual",
                "reason": "部署冒烟：重复请求必须幂等",
                "priority": 3,
            },
        )
        if duplicate_handoff.get("id") != handoff.get("id"):
            raise AssertionError("active handoff creation was not idempotent")
        handoff_version_verified = False
        if smoke_user.get("role") == "admin":
            claimed, _ = client.request(
                "PATCH",
                f"/admin/handoffs/{handoff['id']}",
                {
                    "expected_version": handoff["version"],
                    "status": "in_progress",
                    "assigned_to": smoke_user["id"],
                },
            )
            resolved, _ = client.request(
                "PATCH",
                f"/admin/handoffs/{handoff['id']}",
                {
                    "expected_version": claimed["version"],
                    "status": "resolved",
                    "resolution": "部署冒烟验证通过",
                },
            )
            handoff_version_verified = (
                claimed.get("version") == handoff.get("version", 0) + 1
                and resolved.get("version") == claimed.get("version", 0) + 1
            )
            if not handoff_version_verified:
                raise AssertionError("handoff optimistic version did not advance monotonically")
        return {
            "status": "通过",
            "events": len(events),
            "sources": len(sources),
            "answer_chars": len(answer),
            "required_terms_verified": len(required_terms or []),
            "answerability": done.get("answerability"),
            "guardrail_filtered_segments": done.get("guardrail_filtered_segments"),
            "route_mode": (done.get("knowledge_route") or {}).get("mode"),
            "handoff_idempotent": True,
            "handoff_version_verified": handoff_version_verified,
        }
    finally:
        client.request("DELETE", f"/sessions/{session_id}")


def main() -> None:
    parser = argparse.ArgumentParser(description="验收已部署的中文 RAG/SSE 链路")
    parser.add_argument("--base-url", default=os.getenv("SMOKE_BASE_URL", "http://localhost:18080"))
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--host-header",
        default=os.getenv("SMOKE_HOST_HEADER"),
        help="通过内部 Service 验证代理时显式发送已加入白名单的 Host，例如 localhost",
    )
    parser.add_argument("--question", default=DEFAULT_QUESTION)
    parser.add_argument("--expected-source", default="小米产品使用与故障排查")
    parser.add_argument(
        "--required-answer-term",
        action="append",
        help="可重复指定；自定义问题时用于校验回答必须包含的有据关键词",
    )
    args = parser.parse_args()
    try:
        print(json.dumps(run(args), ensure_ascii=False, indent=2))
    except (AssertionError, KeyError, OSError, ValueError) as exc:
        print(f"部署验收失败：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
