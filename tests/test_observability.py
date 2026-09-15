from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest

from codex_bridge.config import BridgeConfig
from codex_bridge.server import create_app


class FakeBridge:
    pass


class FakeRuntime:
    def __init__(self) -> None:
        self.bridge = FakeBridge()

    async def start(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass


def _config(tmp_path) -> BridgeConfig:
    return BridgeConfig(
        host="127.0.0.1",
        port=8000,
        ui_port=8001,
        allowed_roots=(str(tmp_path),),
        allowed_hosts=(),
        allowed_origins=(),
        codex_executable="codex",
        wait_default_seconds=18.0,
        wait_max_seconds=30.0,
        shutdown_grace_seconds=3.0,
    )


@pytest.mark.asyncio
async def test_server_lifecycle_persists_observability_events(tmp_path, monkeypatch) -> None:
    local_app_data = tmp_path / "local-app-data"
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    runtime = FakeRuntime()
    app = create_app(_config(tmp_path), runtime_factory=lambda _: runtime)

    async with app.router.lifespan_context(app):
        pass

    log_path = local_app_data / "CodexBridge" / "logs" / "observability.jsonl"
    assert log_path.exists()
    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]

    assert [record["event"] for record in records] == [
        "server.start",
        "server.shutdown",
    ]


def test_windows_local_app_data_resolves_outside_repository(tmp_path) -> None:
    from codex_bridge.observability import default_log_path

    local_app_data = tmp_path / "local-app-data"

    path = default_log_path(
        {"LOCALAPPDATA": str(local_app_data)},
        platform="win32",
    )

    assert path == local_app_data / "CodexBridge" / "logs" / "observability.jsonl"


def test_emit_writes_safe_jsonl_record(tmp_path) -> None:
    from codex_bridge.observability import ObservabilityLogger

    observer = ObservabilityLogger(tmp_path / "observability.jsonl")
    observer.server_start()

    record = json.loads((tmp_path / "observability.jsonl").read_text(encoding="utf-8").strip())

    assert isinstance(record["timestamp"], str)
    assert record["timestamp"].endswith("Z")
    assert record["event"] == "server.start"
    assert isinstance(record["pid"], int)
    assert set(record) == {"timestamp", "event", "pid"}


@pytest.mark.asyncio
async def test_mcp_request_records_status_duration_and_protocol_without_payloads(tmp_path) -> None:
    from codex_bridge.observability import MCPObservabilityMiddleware, ObservabilityLogger

    observer = ObservabilityLogger(tmp_path / "observability.jsonl")
    receive_calls = 0

    async def downstream(scope: dict[str, Any], receive: Any, send: Any) -> None:
        del scope, receive
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send(
            {
                "type": "http.response.body",
                "body": b"prompt body tool args token session_id",
            }
        )

    async def receive() -> dict[str, Any]:
        nonlocal receive_calls
        receive_calls += 1
        return {
            "type": "http.request",
            "body": b"request body prompt tool args authorization cookie token",
        }

    messages: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    middleware = MCPObservabilityMiddleware(downstream, observer=observer)
    await middleware(
        {
            "type": "http",
            "method": "POST",
            "path": "/mcp",
            "query_string": b"prompt=private&session_id=private",
            "headers": [
                (b"mcp-protocol-version", b"2025-06-18"),
                (b"authorization", b"Bearer private-token"),
                (b"cookie", b"private-cookie"),
            ],
        },
        receive,
        send,
    )

    records = [
        json.loads(line)
        for line in (tmp_path / "observability.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    start, end = records
    serialized = json.dumps(records)

    assert receive_calls == 0
    assert messages[0]["status"] == 204
    assert start == {
        "timestamp": start["timestamp"],
        "event": "mcp.request.start",
        "pid": start["pid"],
        "method": "POST",
        "path": "/mcp",
        "protocol_version": "2025-06-18",
    }
    assert end["event"] == "mcp.request.end"
    assert end["status"] == 204
    assert isinstance(end["duration_ms"], (int, float))
    assert "private" not in serialized
    assert "prompt" not in serialized
    assert "tool args" not in serialized
    assert "authorization" not in serialized
    assert "cookie" not in serialized
    assert "token" not in serialized
    assert "session" not in serialized
    assert "body" not in serialized


@pytest.mark.asyncio
async def test_mcp_exception_and_cancel_are_distinct_and_do_not_log_messages(tmp_path) -> None:
    from codex_bridge.observability import MCPObservabilityMiddleware, ObservabilityLogger

    async def run(failure: BaseException) -> list[dict[str, Any]]:
        observer = ObservabilityLogger(tmp_path / f"{type(failure).__name__}.jsonl")

        async def downstream(scope: dict[str, Any], receive: Any, send: Any) -> None:
            del scope, receive, send
            raise failure

        async def send(_message: dict[str, Any]) -> None:
            pass

        middleware = MCPObservabilityMiddleware(downstream, observer=observer)
        with pytest.raises(type(failure)):
            await middleware(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/mcp",
                    "query_string": b"secret=private",
                    "headers": [],
                },
                lambda: {"type": "http.request", "body": b"secret body"},
                send,
            )
        return [
            json.loads(line)
            for line in (tmp_path / f"{type(failure).__name__}.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]

    exception_records = await run(RuntimeError("prompt secret-token"))
    cancel_records = await run(asyncio.CancelledError("tool args secret-token"))

    exception_end = exception_records[-1]
    cancel_end = cancel_records[-1]
    assert exception_end["event"] == "mcp.request.end"
    assert exception_end["exception_type"] == "RuntimeError"
    assert "message" not in exception_end
    assert exception_end.get("cancelled") is not True
    assert cancel_end["event"] == "mcp.request.end"
    assert cancel_end["cancelled"] is True
    assert "exception_type" not in cancel_end
    assert "prompt" not in json.dumps(exception_records + cancel_records)
    assert "secret-token" not in json.dumps(exception_records + cancel_records)


def test_observability_storage_failures_are_swallowed(tmp_path) -> None:
    from codex_bridge.observability import ObservabilityLogger

    def raising_factory(*args: Any, **kwargs: Any) -> logging.Handler:
        del args, kwargs
        raise OSError("cannot create handler")

    observer = ObservabilityLogger(
        tmp_path / "handler-failure.jsonl",
        handler_factory=raising_factory,
    )
    observer.server_start()

    blocker = tmp_path / "not-a-directory"
    blocker.write_text("blocker", encoding="utf-8")
    directory_observer = ObservabilityLogger(blocker / "observability.jsonl")
    directory_observer.server_shutdown()

    class FailingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            del record
            raise OSError("cannot write")

    write_observer = ObservabilityLogger(
        tmp_path / "write-failure.jsonl",
        handler_factory=lambda *args, **kwargs: FailingHandler(),
    )
    write_observer.server_start()


def test_observability_rotates_with_injected_small_limit(tmp_path) -> None:
    from codex_bridge.observability import ObservabilityLogger

    log_path = tmp_path / "observability.jsonl"
    observer = ObservabilityLogger(log_path, max_bytes=100, backup_count=2)

    for _ in range(20):
        observer.server_start()

    rotated = sorted(tmp_path.glob("observability.jsonl*"))
    assert log_path.exists()
    assert (tmp_path / "observability.jsonl.1").exists()
    assert len(rotated) <= 3
