from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import pytest
from mcp import types
from mcp.server.mcpserver.exceptions import ToolError

from codex_bridge.config import BridgeConfig, ConfigurationError, GitHubMcpConfig
from codex_bridge.paths import PathPolicyError
from codex_bridge.server import build_runtime, create_app, prepare_config


@dataclass
class FakeBridge:
    error: Exception | None = None

    async def start(self, _cwd: str, _prompt: str) -> dict[str, Any]:
        if self.error is not None:
            raise self.error
        return {"ok": True}


class FakeRuntime:
    def __init__(self) -> None:
        self.bridge = FakeBridge()
        self.start_count = 0
        self.shutdown_count = 0

    async def start(self) -> None:
        self.start_count += 1

    async def shutdown(self) -> None:
        self.shutdown_count += 1


class FakeRemoteProvider:
    def __init__(self, settings: GitHubMcpConfig) -> None:
        self.config = settings
        self.upstream_tools = (
            types.Tool(name="get_file_contents", inputSchema={"type": "object"}),
        )
        self.start_count = 0
        self.close_count = 0

    async def start(self) -> None:
        self.start_count += 1

    async def close(self) -> None:
        self.close_count += 1

    async def call_tool(self, _name: str, _arguments: dict[str, Any]):
        return types.CallToolResult(content=[types.TextContent(type="text", text="ok")])


def config(tmp_path) -> BridgeConfig:
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


def test_prepare_config_keeps_cli_codex_before_process_environment(tmp_path, monkeypatch) -> None:
    cli_executable = tmp_path / "cli-codex.exe"
    env_executable = tmp_path / "env-codex.exe"
    cli_executable.write_bytes(b"")
    env_executable.write_bytes(b"")
    monkeypatch.setenv("CODEX_BRIDGE_CODEX_EXECUTABLE", str(env_executable))

    settings = BridgeConfig.from_sources(
        explicit_allowed_roots=(str(tmp_path),),
        explicit_codex_executable=str(cli_executable),
        environ={},
    )

    prepared = prepare_config(settings)

    assert prepared.codex_executable == str(cli_executable)


def test_server_registers_exactly_nine_tools(tmp_path) -> None:
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
        "codex_status",
    }


@pytest.mark.asyncio
async def test_server_uses_low_level_router_for_wire_tools_list(tmp_path) -> None:
    runtime = FakeRuntime()
    app = create_app(config(tmp_path), runtime_factory=lambda _: runtime)

    assert app.state.mcp_lowlevel_server.get_request_handler("tools/list") is not None
    assert app.state.mcp_lowlevel_server.get_request_handler("tools/call") is not None

    async with app.router.lifespan_context(app):
        result = await app.state.mcp_router.list_tools(None, None)

    native_names = [tool.name for tool in app.state.mcp_server._tool_manager.list_tools()]
    assert [tool.name for tool in result.tools] == native_names


@pytest.mark.asyncio
async def test_enabled_github_mount_starts_before_runtime_and_closes_afterwards(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = FakeRuntime()
    settings = replace(
        config(tmp_path),
        github_mcp=GitHubMcpConfig(enabled=True, pat="secret"),
    )
    provider = FakeRemoteProvider(settings.github_mcp)
    monkeypatch.setattr("codex_bridge.server.RemoteMcpProvider", lambda _settings: provider)
    app = create_app(settings, runtime_factory=lambda _: runtime)

    async with app.router.lifespan_context(app):
        assert provider.start_count == 1
        assert runtime.start_count == 1
        result = await app.state.mcp_router.list_tools(None, None)
        assert "github_get_file_contents" in {tool.name for tool in result.tools}

    assert provider.close_count == 1
    assert runtime.shutdown_count == 1


def test_server_publishes_codex_delegation_instructions(tmp_path) -> None:
    runtime = FakeRuntime()
    app = create_app(config(tmp_path), runtime_factory=lambda _: runtime)

    instructions = app.state.mcp_server.instructions

    assert instructions is not None
    for anchor in (
        "delegation interface",
        "codex_start",
        "codex_wait",
        "state=in_progress",
        "codex_continue",
        "codex_steer",
        "approved specification",
        "final review",
    ):
        assert anchor in instructions

    initialization_options = app.state.mcp_server._lowlevel_server.create_initialization_options()
    assert initialization_options.instructions == instructions


def test_codex_wait_description_explains_bounded_long_poll(tmp_path) -> None:
    runtime = FakeRuntime()
    app = create_app(config(tmp_path), runtime_factory=lambda _: runtime)
    tool = next(
        tool
        for tool in app.state.mcp_server._tool_manager.list_tools()
        if tool.name == "codex_wait"
    )

    description = tool.description

    assert description is not None
    description = description.casefold()
    assert "long-poll" in description
    assert "terminal" in description
    assert "intervention" in description
    assert "in_progress" in description


def test_codex_continue_description_explains_new_turn_on_existing_thread(tmp_path) -> None:
    runtime = FakeRuntime()
    app = create_app(config(tmp_path), runtime_factory=lambda _: runtime)
    tool = next(
        tool
        for tool in app.state.mcp_server._tool_manager.list_tools()
        if tool.name == "codex_continue"
    )

    description = tool.description

    assert description is not None
    description = description.casefold()
    assert "new turn" in description
    assert "existing" in description
    assert "thread" in description
    assert "resum" in description
    assert "additional input" not in description
    assert "active turn" not in description


@pytest.mark.asyncio
async def test_lifespan_starts_and_shutdowns_one_runtime(tmp_path) -> None:
    runtime = FakeRuntime()
    app = create_app(config(tmp_path), runtime_factory=lambda _: runtime)

    async with app.router.lifespan_context(app):
        assert runtime.start_count == 1
        assert app.state.bridge is runtime.bridge
    assert runtime.shutdown_count == 1


@pytest.mark.asyncio
async def test_expected_path_errors_are_mcp_tool_errors(tmp_path) -> None:
    runtime = FakeRuntime()
    runtime.bridge.error = PathPolicyError("cwd is outside CODEX_BRIDGE_ALLOWED_ROOTS")
    app = create_app(config(tmp_path), runtime_factory=lambda _: runtime)
    tools = app.state.mcp_server._tool_manager.list_tools()
    tool = next(tool for tool in tools if tool.name == "codex_start")

    async with app.router.lifespan_context(app):
        with pytest.raises(ToolError, match="CODEX_BRIDGE_ALLOWED_ROOTS"):
            await tool.fn(str(tmp_path), "prompt")


@pytest.mark.asyncio
async def test_unexpected_tool_errors_are_not_silently_downgraded(tmp_path) -> None:
    runtime = FakeRuntime()
    runtime.bridge.error = RuntimeError("unexpected")
    app = create_app(config(tmp_path), runtime_factory=lambda _: runtime)
    tools = app.state.mcp_server._tool_manager.list_tools()
    tool = next(tool for tool in tools if tool.name == "codex_start")

    async with app.router.lifespan_context(app):
        with pytest.raises(RuntimeError, match="unexpected"):
            await tool.fn(str(tmp_path), "prompt")


def test_configured_host_security_is_exposed_on_app(tmp_path) -> None:
    runtime = FakeRuntime()
    settings = config(tmp_path)
    settings = replace(settings, allowed_hosts=("bridge.example.com", "bridge.example.com:*"))

    app = create_app(settings, runtime_factory=lambda _: runtime)

    security = app.state.transport_security
    assert security.allowed_hosts == ["bridge.example.com", "bridge.example.com:*"]
    assert security.allowed_origins == []


def test_non_loopback_bind_requires_allowed_host(tmp_path) -> None:
    runtime = FakeRuntime()
    settings = replace(config(tmp_path), host="0.0.0.0")

    with pytest.raises(ConfigurationError, match="allowed host"):
        create_app(settings, runtime_factory=lambda _: runtime)


def test_mcp_app_does_not_register_ui_routes(tmp_path) -> None:
    runtime = FakeRuntime()
    app = create_app(config(tmp_path), runtime_factory=lambda _: runtime)

    assert all(
        not getattr(route, "path", "").startswith(("/healthz", "/ui-api")) for route in app.routes
    )


def test_build_runtime_passes_shutdown_callback_to_ui_app(tmp_path, monkeypatch) -> None:
    import codex_bridge.server as server_module

    captured: list[object] = []

    def fake_create_ui_app(*args, **kwargs):
        captured.append(kwargs.get("shutdown_callback"))
        return object()

    monkeypatch.setattr(server_module, "create_ui_app", fake_create_ui_app)

    def callback() -> None:
        pass

    build_runtime(config(tmp_path), shutdown_callback=callback)

    assert captured == [callback]
