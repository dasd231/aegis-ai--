from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
import uuid
from dataclasses import dataclass, field

import httpx


@dataclass
class Results:
    latencies_ms: list[float] = field(default_factory=list)
    source_ready_ms: list[float] = field(default_factory=list)
    ttft_ms: list[float] = field(default_factory=list)
    errors: int = 0
    rejected: int = 0
    setup_errors: int = 0
    cleanup_errors: int = 0
    ready_users: int = 0
    status_counts: dict[str, int] = field(default_factory=dict)

    def record_status(self, stage: str, status_code: int | str) -> None:
        key = f"{stage}:{status_code}"
        self.status_counts[key] = self.status_counts.get(key, 0) + 1


def percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(quantile * len(ordered)) - 1)
    return round(ordered[index], 2)


async def virtual_user(base_url: str, duration: int, run_id: str, index: int, results: Results) -> None:
    # Load tests must hit the named target directly. Inheriting a developer
    # machine's HTTP_PROXY can silently benchmark the proxy instead.
    async with httpx.AsyncClient(base_url=base_url, timeout=60, trust_env=False) as client:
        try:
            registered = await client.post(
                "/api/v1/auth/register",
                json={
                    "email": f"load-{run_id}-{index}@example.com",
                    "password": "LoadTest123!",
                    "display_name": f"load-{index}",
                },
            )
        except httpx.HTTPError as exc:
            results.errors += 1
            results.setup_errors += 1
            results.record_status("register", type(exc).__name__)
            return
        results.record_status("register", registered.status_code)
        if registered.status_code != 201:
            results.errors += 1
            results.setup_errors += 1
            return
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        try:
            session = await client.post("/api/v1/sessions", json={"title": "capacity"}, headers=headers)
        except httpx.HTTPError as exc:
            results.errors += 1
            results.setup_errors += 1
            results.record_status("session", type(exc).__name__)
            return
        results.record_status("session", session.status_code)
        if session.status_code != 201:
            results.errors += 1
            results.setup_errors += 1
            return
        session_id = session.json()["id"]
        results.ready_users += 1
        try:
            deadline = time.monotonic() + duration
            sequence = 0
            while time.monotonic() < deadline:
                started = time.perf_counter()
                try:
                    async with client.stream(
                        "POST",
                        "/api/v1/chat/stream",
                        json={"session_id": session_id, "question": f"小米手机充不进电怎么排查？批次 {sequence}"},
                        headers=headers,
                    ) as response:
                        results.record_status("chat", response.status_code)
                        if response.status_code == 200:
                            event_name = ""
                            terminal_event = ""
                            first_source_ms: float | None = None
                            first_token_ms: float | None = None
                            async for line in response.aiter_lines():
                                if line.startswith("event:"):
                                    event_name = line[6:].strip()
                                elif not line:
                                    event_elapsed = (time.perf_counter() - started) * 1000
                                    if event_name == "sources" and first_source_ms is None:
                                        first_source_ms = event_elapsed
                                    elif event_name == "token" and first_token_ms is None:
                                        first_token_ms = event_elapsed
                                    elif event_name in {"done", "error"}:
                                        terminal_event = event_name
                                    event_name = ""
                            elapsed = (time.perf_counter() - started) * 1000
                            if terminal_event == "done" and first_token_ms is not None:
                                results.latencies_ms.append(elapsed)
                                results.ttft_ms.append(first_token_ms)
                                if first_source_ms is not None:
                                    results.source_ready_ms.append(first_source_ms)
                            else:
                                results.errors += 1
                        elif response.status_code in {429, 503}:
                            results.rejected += 1
                            await response.aread()
                        else:
                            results.errors += 1
                            await response.aread()
                except httpx.HTTPError as exc:
                    results.errors += 1
                    results.record_status("chat", type(exc).__name__)
                    sequence += 1
                    continue
                sequence += 1
        finally:
            try:
                cleanup = await client.delete(f"/api/v1/sessions/{session_id}", headers=headers)
                results.record_status("cleanup", cleanup.status_code)
                if cleanup.status_code != 204:
                    results.cleanup_errors += 1
            except httpx.HTTPError as exc:
                results.cleanup_errors += 1
                results.record_status("cleanup", type(exc).__name__)


async def run(args) -> int:
    run_id = uuid.uuid4().hex[:10]
    results = Results()
    started = time.monotonic()
    await asyncio.gather(
        *(
            virtual_user(args.base_url.rstrip("/"), args.duration, run_id, index, results)
            for index in range(args.concurrency)
        )
    )
    elapsed = time.monotonic() - started
    total = len(results.latencies_ms) + results.errors + results.rejected
    report = {
        "concurrency": args.concurrency,
        "duration_seconds": round(elapsed, 2),
        "requests": total,
        "success": len(results.latencies_ms),
        "errors": results.errors,
        "rejected": results.rejected,
        "setup_errors": results.setup_errors,
        "cleanup_errors": results.cleanup_errors,
        "ready_users": results.ready_users,
        "status_counts": dict(sorted(results.status_counts.items())),
        "requests_per_second": round(total / max(elapsed, 0.001), 2),
        "p50_ms": percentile(results.latencies_ms, 0.50),
        "p95_ms": percentile(results.latencies_ms, 0.95),
        "p99_ms": percentile(results.latencies_ms, 0.99),
        "source_ready_p95_ms": percentile(results.source_ready_ms, 0.95),
        "ttft_p50_ms": percentile(results.ttft_ms, 0.50),
        "ttft_p95_ms": percentile(results.ttft_ms, 0.95),
        "ttft_p99_ms": percentile(results.ttft_ms, 0.99),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    error_rate = (results.errors + results.rejected) / max(total, 1)
    return 1 if (
        total == 0
        or report["p95_ms"] > args.max_p95_ms
        or report["ttft_p95_ms"] > args.max_ttft_ms
        or error_rate > args.max_error_rate
        or results.cleanup_errors > 0
    ) else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Aegis closed-loop SSE capacity test")
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--duration", type=int, default=30)
    parser.add_argument("--max-p95-ms", type=float, default=3000)
    parser.add_argument("--max-ttft-ms", type=float, default=1500)
    parser.add_argument("--max-error-rate", type=float, default=0.01)
    args = parser.parse_args()
    if args.concurrency < 1 or args.duration < 1:
        parser.error("concurrency and duration must be positive")
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
