from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from codex_bridge.config import BridgeConfig
from codex_bridge.server import create_app


@dataclass
class FakeBridge:
    pass


class FakeRuntime:
    def __init__(self) -> None:
        self.bridge = FakeBridge()
        self.start_count = 0
        self.shutdown_count = 0

    async def start(self) -> None:
        self.start_count += 1

    async def shutdown(self) -> None:
        self.shutdown_count += 1


def config(tmp_path) -> BridgeConfig:
    return BridgeConfig(
        host="127.0.0.1",
        port=8000,
        allowed_roots=(str(tmp_path),),
        allowed_hosts=(),
        allowed_origins=(),
        codex_executable="codex",
        wait_default_seconds=18.0,
        wait_max_seconds=30.0,
        shutdown_grace_seconds=3.0,
    )


def test_server_registers_exactly_eight_tools(tmp_path) -> None:
    runtime = FakeRuntime()
    app = create_app(config(tmp_path), runtime_factory=lambda _: runtime)

    names = {tool.name for tool in app.state.mcp_server._tool_manager.list_tools()}

    assert names == {
        "codex_start",
        "codex_continue",
        "codex_wait",
        "codex_steer",
        "codex_approval",
        "codex_user_input",
        "codex_interrupt",
        "codex_threads",
    }


@pytest.mark.asyncio
async def test_lifespan_starts_and_shutdowns_one_runtime(tmp_path) -> None:
    runtime = FakeRuntime()
    app = create_app(config(tmp_path), runtime_factory=lambda _: runtime)

    async with app.router.lifespan_context(app):
        assert runtime.start_count == 1
        assert app.state.bridge is runtime.bridge
    assert runtime.shutdown_count == 1


def test_configured_host_security_is_exposed_on_app(tmp_path) -> None:
    runtime = FakeRuntime()
    settings = config(tmp_path)
    settings = replace(settings, allowed_hosts=("bridge.example.com", "bridge.example.com:*"))

    app = create_app(settings, runtime_factory=lambda _: runtime)

    security = app.state.transport_security
    assert security.allowed_hosts == ["bridge.example.com", "bridge.example.com:*"]
    assert security.allowed_origins == []
