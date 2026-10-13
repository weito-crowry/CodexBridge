from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import ClientSession, types
from mcp.server.lowlevel import Server as LowLevelServer
from pydantic import TypeAdapter

from codex_bridge.config import BridgeConfig, McpEventsConfig
from codex_bridge.server import create_app


class _Runtime:
    def __init__(self) -> None:
        self.bridge: Any = object()
        self.start_count = 0
        self.shutdown_count = 0

    async def start(self) -> None:
        self.start_count += 1

    async def shutdown(self) -> None:
        self.shutdown_count += 1


def _config(root: Path) -> BridgeConfig:
    return BridgeConfig(
        host="127.0.0.1",
        port=8000,
        ui_port=8001,
        allowed_roots=(str(root),),
        allowed_hosts=(),
        allowed_origins=(),
        codex_executable="codex",
        wait_default_seconds=18.0,
        wait_max_seconds=30.0,
        shutdown_grace_seconds=3.0,
    )


def _exception_group_contains(
    error: BaseException, expected: type[BaseException], message: str
) -> bool:
    if isinstance(error, BaseExceptionGroup):
        return any(
            _exception_group_contains(child, expected, message) for child in error.exceptions
        )
    return isinstance(error, expected) and message in str(error)


@pytest.mark.asyncio
async def test_mcp_events_json_rpc_routes_discovery_and_subscription_methods(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = replace(
        _config(tmp_path),
        mcp_events=McpEventsConfig(enabled=True, db_path=tmp_path / "events.sqlite3"),
    )
    runtime = _Runtime()

    class EventBridge:
        async def threads(self, thread_id=None, *, include_history=False):
            assert include_history is False
            return {"thread": {"id": thread_id}}

    runtime.bridge = EventBridge()
    app = create_app(settings, runtime_factory=lambda _: runtime)
    events = app.state.mcp_events
    assert events is not None
    events._principal_resolver = lambda _context: "test-user-id"

    async def verify_callback(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr("codex_bridge.mcp_events.verify_callback", verify_callback)
    lowlevel: LowLevelServer = app.state.mcp_lowlevel_server
    initialization = lowlevel.create_initialization_options()

    async with app.router.lifespan_context(app):
        client_to_server_send, client_to_server_receive = anyio.create_memory_object_stream(0)
        server_to_client_send, server_to_client_receive = anyio.create_memory_object_stream(0)
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(
                lowlevel.run,
                client_to_server_receive,
                server_to_client_send,
                initialization,
            )
            async with ClientSession(server_to_client_receive, client_to_server_send) as client:
                await client.discover()
                discovered = await client.send_discover("2026-07-28")
                assert discovered["capabilities"].get("events") == {}, discovered

                async def request(method: str, params: dict[str, Any]) -> dict[str, Any]:
                    return await client.send_request(
                        types.Request[dict[str, Any], str](method=method, params=params),
                        TypeAdapter(dict[str, Any]),
                    )

                listed = await request("events/list", {})
                assert listed["events"][0]["name"] == "codex.turn.finished"
                subscribed = await request(
                    "events/subscribe",
                    {
                        "name": "codex.turn.finished",
                        "arguments": {"thread_id": "thread-a"},
                        "delivery": {
                            "mode": "webhook",
                            "url": "https://receiver.example/callback",
                            "secret": "whsec_AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",
                        },
                    },
                )
                assert subscribed["id"].startswith("sub_")
                unsubscribed = await request(
                    "events/unsubscribe",
                    {
                        "name": "codex.turn.finished",
                        "arguments": {"thread_id": "thread-a"},
                        "delivery": {
                            "mode": "webhook",
                            "url": "https://receiver.example/callback",
                        },
                    },
                )
                assert unsubscribed["resultType"] == "complete"
                assert not events.store.subscription_is_active(subscribed["id"])
            task_group.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_mcp_events_invalid_database_path_aborts_startup_and_cleans_resources(
    tmp_path: Path,
) -> None:
    blocking_file = tmp_path / "file-not-directory"
    blocking_file.write_text("", encoding="utf-8")
    settings = replace(
        _config(tmp_path),
        mcp_events=McpEventsConfig(enabled=True, db_path=blocking_file / "events.sqlite3"),
    )
    runtime = _Runtime()
    app = create_app(settings, runtime_factory=lambda _: runtime)
    events = app.state.mcp_events
    assert events is not None

    with pytest.raises(ExceptionGroup) as exc_info:
        async with app.router.lifespan_context(app):
            pytest.fail("Events initialization should fail before serving requests")

    assert _exception_group_contains(exc_info.value, OSError, "file-not-directory")
    assert not events._ready
    assert events.worker._task is None
    assert runtime.start_count == 0


@pytest.mark.asyncio
async def test_mcp_events_sqlite_initialization_error_propagates_and_closes_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = replace(
        _config(tmp_path),
        mcp_events=McpEventsConfig(enabled=True, db_path=tmp_path / "events.sqlite3"),
    )
    runtime = _Runtime()
    app = create_app(settings, runtime_factory=lambda _: runtime)
    events = app.state.mcp_events
    assert events is not None
    close_store = events.store.close
    close_calls = 0

    def close() -> None:
        nonlocal close_calls
        close_calls += 1
        close_store()

    def fail_connect(*_args: Any, **_kwargs: Any) -> None:
        raise sqlite3.OperationalError("injected initialization failure")

    monkeypatch.setattr(events.store, "close", close)
    monkeypatch.setattr("codex_bridge.event_store.sqlite3.connect", fail_connect)

    with pytest.raises(ExceptionGroup) as exc_info:
        async with app.router.lifespan_context(app):
            pytest.fail("Events initialization should fail before serving requests")

    assert _exception_group_contains(exc_info.value, RuntimeError, "database initialization failed")
    assert close_calls == 1
    assert not events._ready
    assert events.worker._task is None
    assert runtime.start_count == 0


@pytest.mark.asyncio
async def test_mcp_events_worker_start_error_propagates_and_closes_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = replace(
        _config(tmp_path),
        mcp_events=McpEventsConfig(enabled=True, db_path=tmp_path / "events.sqlite3"),
    )
    runtime = _Runtime()
    app = create_app(settings, runtime_factory=lambda _: runtime)
    events = app.state.mcp_events
    assert events is not None
    close_store = events.store.close
    stop_worker = events.worker.stop
    close_calls = 0
    stop_calls = 0

    def close() -> None:
        nonlocal close_calls
        close_calls += 1
        close_store()

    async def fail_start() -> None:
        raise RuntimeError("injected worker start failure")

    async def stop() -> None:
        nonlocal stop_calls
        stop_calls += 1
        await stop_worker()

    monkeypatch.setattr(events.store, "close", close)
    monkeypatch.setattr(events.worker, "start", fail_start)
    monkeypatch.setattr(events.worker, "stop", stop)

    with pytest.raises(ExceptionGroup) as exc_info:
        async with app.router.lifespan_context(app):
            pytest.fail("Events worker startup should fail before serving requests")

    assert _exception_group_contains(exc_info.value, RuntimeError, "injected worker start failure")
    assert close_calls == 1
    assert stop_calls == 1
    assert not events._ready
    assert events.worker._task is None
    assert runtime.start_count == 0


@pytest.mark.asyncio
async def test_disabled_mcp_events_keeps_normal_lifespan_startup(tmp_path: Path) -> None:
    runtime = _Runtime()
    app = create_app(_config(tmp_path), runtime_factory=lambda _: runtime)

    async with app.router.lifespan_context(app):
        assert app.state.mcp_events is None
        assert runtime.start_count == 1
        assert not (tmp_path / "events.sqlite3").exists()

    assert runtime.shutdown_count == 1
