from __future__ import annotations

import asyncio
import logging
import signal
import socket
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from codex_bridge.activity import ActivityStore
from codex_bridge.config import BridgeConfig
from codex_bridge.server import BridgeRuntime
from codex_bridge.ui_api import create_ui_app
from codex_bridge.ui_server import LocalUiServer, UvicornShutdownController, _create_server


def test_uvicorn_shutdown_controller_requests_outer_server_exit() -> None:
    class FakeOuterServer:
        should_exit = False

    server = FakeOuterServer()
    controller = UvicornShutdownController()
    controller.bind(server)  # type: ignore[arg-type]

    controller.request_shutdown()

    assert server.should_exit is True


class FakeUvicornServer:
    def __init__(self, app: Any, host: str, port: int) -> None:
        self.app = app
        self.host = host
        self.port = port
        self.should_exit = False
        self.started = False
        self.stop = asyncio.Event()
        self.ignore_shutdown = False

    async def serve(self) -> None:
        self.started = True
        while not self.should_exit or self.ignore_shutdown:
            await asyncio.sleep(0)


class FakeServerFactory:
    def __init__(self) -> None:
        self.server: FakeUvicornServer | None = None

    def __call__(self, app: Any, host: str, port: int) -> FakeUvicornServer:
        self.server = FakeUvicornServer(app, host, port)
        return self.server


def test_ui_uvicorn_server_does_not_capture_process_signals(monkeypatch) -> None:
    captured: list[tuple[signal.Signals, Any]] = []

    def record_signal(signum: signal.Signals, handler: Any) -> Any:
        captured.append((signum, handler))
        return signal.SIG_DFL

    monkeypatch.setattr(signal, "signal", record_signal)
    server = _create_server(object(), "127.0.0.1", 8123)

    with server.capture_signals():
        pass

    assert captured == []


@pytest.mark.asyncio
async def test_local_ui_server_binds_fixed_loopback_host_and_configured_port() -> None:
    factory = FakeServerFactory()
    app = object()
    server = LocalUiServer(app, 8123, server_factory=factory)

    await server.start()

    assert factory.server is not None
    assert factory.server.app is app
    assert factory.server.host == "127.0.0.1"
    assert factory.server.port == 8123
    await server.shutdown(0.1)


@pytest.mark.asyncio
async def test_local_ui_server_shutdown_is_bounded() -> None:
    factory = FakeServerFactory()
    server = LocalUiServer(object(), 8123, server_factory=factory)
    await server.start()
    assert factory.server is not None
    factory.server.ignore_shutdown = True

    with pytest.raises(TimeoutError, match="UI server did not shut down"):
        await asyncio.wait_for(server.shutdown(0.01), timeout=0.2)

    assert server.running is False


def test_create_ui_server_configures_graceful_timeout() -> None:
    server = _create_server(object(), "127.0.0.1", 8123, graceful_timeout=2.5)

    config = server.config
    assert config.timeout_graceful_shutdown == 2.5
    assert config.log_config is None


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.asyncio
async def test_local_ui_server_shutdown_is_bounded_with_live_sse_subscription(tmp_path) -> None:
    activities = ActivityStore()
    app = create_ui_app(FakeBridge([]), activities, _config(tmp_path))
    port = _free_loopback_port()
    server = LocalUiServer(app, port)
    await server.start()

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(
        b"GET /ui-api/events HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: keep-alive\r\n\r\n"
    )
    await writer.drain()
    headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=1.0)
    assert b"200 OK" in headers
    for _ in range(100):
        if activities._subscribers:
            break
        await asyncio.sleep(0.01)
    assert activities._subscribers

    try:
        with pytest.raises(TimeoutError, match="UI server did not shut down"):
            await asyncio.wait_for(server.shutdown(0.01), timeout=0.5)
    finally:
        writer.close()
        await writer.wait_closed()

    assert server.running is False


class FakeAppServer:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def start(self) -> None:
        self.events.append("app.start")

    async def shutdown(self, grace_seconds: float) -> None:
        self.events.append("app.shutdown")


class FakeBridge:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def interrupt_active_turns(self, wait_seconds: float) -> None:
        self.events.append("bridge.interrupt")


class FakeUiServer:
    def __init__(self, events: list[str], fail_start: bool = False) -> None:
        self.events = events
        self.fail_start = fail_start
        self.shutdown_timeouts: list[float] = []

    async def start(self) -> None:
        self.events.append("ui.start")
        if self.fail_start:
            raise RuntimeError("ui bind failed")

    async def shutdown(self, timeout: float) -> None:
        self.events.append("ui.shutdown")
        self.shutdown_timeouts.append(timeout)


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
async def test_control_shutdown_closes_live_sse_without_uvicorn_errors(tmp_path, caplog) -> None:
    token = "valid-control-token"
    settings = replace(_config(tmp_path), control_token=token, shutdown_grace_seconds=1.5)
    activities = ActivityStore()
    outer_server = SimpleNamespace(should_exit=False)
    controller = UvicornShutdownController()
    controller.bind(outer_server)
    app = create_ui_app(
        FakeBridge([]),
        activities,
        settings,
        shutdown_callback=controller.request_shutdown,
    )
    port = _free_loopback_port()
    ui_server = LocalUiServer(app, port, graceful_shutdown_timeout=settings.shutdown_grace_seconds)
    events: list[str] = []
    runtime = BridgeRuntime(
        bridge=FakeBridge(events),  # type: ignore[arg-type]
        app_server=FakeAppServer(events),  # type: ignore[arg-type]
        activity_store=activities,
        config=settings,
        ui_server=ui_server,
    )
    caplog.set_level(logging.ERROR, logger="uvicorn.error")

    await ui_server.start()
    sse_reader, sse_writer = await asyncio.open_connection("127.0.0.1", port)
    control_writer = None
    try:
        sse_writer.write(
            b"GET /ui-api/events HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: keep-alive\r\n\r\n"
        )
        await sse_writer.drain()
        assert b"200 OK" in await asyncio.wait_for(sse_reader.readuntil(b"\r\n\r\n"), timeout=1.0)
        for _ in range(100):
            if activities._subscribers:
                break
            await asyncio.sleep(0.01)
        assert activities._subscribers

        control_reader, control_writer = await asyncio.open_connection("127.0.0.1", port)
        control_writer.write(
            (
                "POST /ui-api/control/shutdown HTTP/1.1\r\n"
                "Host: 127.0.0.1\r\n"
                f"Authorization: Bearer {token}\r\n"
                "Content-Length: 0\r\nConnection: close\r\n\r\n"
            ).encode()
        )
        await control_writer.drain()
        control_headers = await asyncio.wait_for(control_reader.readuntil(b"\r\n\r\n"), timeout=1.0)
        control_body = await asyncio.wait_for(control_reader.read(), timeout=1.0)
        assert b"202 Accepted" in control_headers
        assert b"shutdown_requested" in control_body
        assert outer_server.should_exit is True

        await runtime.shutdown()
        await asyncio.wait_for(sse_reader.read(), timeout=0.5)

        assert not activities._subscribers
        assert events == ["bridge.interrupt", "app.shutdown"]
        assert not [record for record in caplog.records if record.name.startswith("uvicorn")]
    finally:
        sse_writer.close()
        await sse_writer.wait_closed()
        if control_writer is not None:
            control_writer.close()
            await control_writer.wait_closed()
        if ui_server.running:
            await ui_server.shutdown(0.5)


@pytest.mark.asyncio
async def test_runtime_starts_ui_after_app_server_and_stops_it_before_app_server(
    tmp_path,
) -> None:
    events: list[str] = []
    runtime = BridgeRuntime(
        bridge=FakeBridge(events),  # type: ignore[arg-type]
        app_server=FakeAppServer(events),  # type: ignore[arg-type]
        activity_store=ActivityStore(),
        config=_config(tmp_path),
        ui_server=FakeUiServer(events),  # type: ignore[arg-type]
    )

    await runtime.start()
    await runtime.shutdown()

    assert events == [
        "app.start",
        "ui.start",
        "bridge.interrupt",
        "ui.shutdown",
        "app.shutdown",
    ]
    assert runtime.ui_server.shutdown_timeouts == [3.0]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_runtime_cleans_up_app_server_when_ui_start_fails(tmp_path) -> None:
    events: list[str] = []
    runtime = BridgeRuntime(
        bridge=FakeBridge(events),  # type: ignore[arg-type]
        app_server=FakeAppServer(events),  # type: ignore[arg-type]
        activity_store=ActivityStore(),
        config=_config(tmp_path),
        ui_server=FakeUiServer(events, fail_start=True),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="ui bind"):
        await runtime.start()

    assert events == ["app.start", "ui.start", "app.shutdown"]
