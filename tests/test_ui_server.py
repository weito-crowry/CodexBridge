from __future__ import annotations

import asyncio
from typing import Any

import pytest

from codex_bridge.activity import ActivityStore
from codex_bridge.config import BridgeConfig
from codex_bridge.server import BridgeRuntime
from codex_bridge.ui_server import LocalUiServer


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

    await asyncio.wait_for(server.shutdown(0.01), timeout=0.2)

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

    async def start(self) -> None:
        self.events.append("ui.start")
        if self.fail_start:
            raise RuntimeError("ui bind failed")

    async def shutdown(self, timeout: float) -> None:
        self.events.append("ui.shutdown")


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
