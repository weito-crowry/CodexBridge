from __future__ import annotations

import json
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
from mcp import ClientSession, MCPError, types
from mcp.server.lowlevel import Server as LowLevelServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError

from codex_bridge.config import (
    BridgeConfig,
    ConfigurationError,
    ExecutionTargetConfig,
    GitHubMcpConfig,
)
from codex_bridge.paths import PathPolicyError
from codex_bridge.server import build_runtime, create_app, prepare_config


@dataclass
class FakeBridge:
    error: Exception | None = None
    start_count: int = 0

    async def start(
        self,
        _cwd: str,
        _prompt: str,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        self.start_count += 1
        if self.error is not None:
            raise self.error
        return {"ok": True}

    async def model_capabilities(self) -> dict[str, Any]:
        return {
            "models": [
                {
                    "model": "test-model",
                    "display_name": "Test model",
                    "description": None,
                    "reasoning_efforts": [{"id": "effort-a", "description": None}],
                    "default_reasoning_effort": "effort-a",
                }
            ],
            "defaults": {"model": "test-model", "reasoning_effort": "effort-a"},
        }


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


class ProbeSession:
    def __init__(self, client_capabilities: types.ClientCapabilities | None = None) -> None:
        self.protocol_version = "2026-07-28"
        self.client_capabilities = client_capabilities
        self.progress_attempts: list[tuple[float, float | None, str | None]] = []

    async def report_progress(
        self, progress: float, total: float | None, message: str | None
    ) -> None:
        self.progress_attempts.append((progress, total, message))


def probe_context(
    meta: dict[str, Any] | None = None,
    client_capabilities: types.ClientCapabilities | None = None,
) -> tuple[Context, ProbeSession]:
    session = ProbeSession(client_capabilities)
    request_context = SimpleNamespace(
        meta=meta,
        protocol_version="2026-07-28",
        session=session,
    )
    return Context(request_context=request_context), session


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


def test_server_registers_thirteen_native_tools_plus_local_probe_tools(tmp_path) -> None:
    runtime = FakeRuntime()
    app = create_app(config(tmp_path), runtime_factory=lambda _: runtime)

    names = {tool.name for tool in app.state.mcp_server._tool_manager.list_tools()}

    probe_names = names & {
        "mcp_tasks_probe",
        "mcp_long_wait_probe",
        "mcp_long_wait_progress_probe",
        "mcp_progress_token_probe",
    }
    assert len(names - probe_names) == 13
    assert names - probe_names == {
        "codex_targets",
        "codex_start",
        "codex_continue",
        "codex_wait",
        "codex_steer",
        "codex_approval",
        "codex_user_input",
        "codex_interrupt",
        "codex_threads",
        "codex_status",
        "codex_setup",
        "codex_setup_capabilities",
        "codex_setup_confirm",
    }


@pytest.mark.asyncio
async def test_mcp_tasks_probe_reports_capability_advertisements_without_starting_codex(
    tmp_path,
) -> None:
    runtime = FakeRuntime()
    app = create_app(config(tmp_path), runtime_factory=lambda _: runtime)
    tool = next(
        tool
        for tool in app.state.mcp_server._tool_manager.list_tools()
        if tool.name == "mcp_tasks_probe"
    )
    absent_context, _ = probe_context()

    absent = await tool.fn(absent_context)

    assert absent["protocol_version"] == "2026-07-28"
    assert absent["tasks_extension_id"] == "io.modelcontextprotocol/tasks"
    assert absent["tasks_extension_advertised"] is False
    assert type(absent["tasks_extension_advertised"]) is bool
    assert absent["legacy_tasks_capability_advertised"] is False
    assert type(absent["legacy_tasks_capability_advertised"]) is bool
    assert absent["client_capabilities"] is None

    capabilities = types.ClientCapabilities(
        extensions={"io.modelcontextprotocol/tasks": {}},
        tasks=types.ClientTasksCapability(),
    )
    advertised_context, _ = probe_context(client_capabilities=capabilities)
    advertised = await tool.fn(advertised_context)

    assert advertised["tasks_extension_advertised"] is True
    assert advertised["legacy_tasks_capability_advertised"] is True
    assert advertised["client_capabilities"]["extensions"] == {"io.modelcontextprotocol/tasks": {}}
    assert runtime.bridge.start_count == 0


@pytest.mark.asyncio
async def test_mcp_long_wait_probe_completes_without_starting_codex(tmp_path) -> None:
    runtime = FakeRuntime()
    app = create_app(config(tmp_path), runtime_factory=lambda _: runtime)
    tool = next(
        tool
        for tool in app.state.mcp_server._tool_manager.list_tools()
        if tool.name == "mcp_long_wait_probe"
    )

    result = await tool.fn(seconds=1)

    assert result["probe"] == "mcp_long_wait_probe"
    assert result["status"] == "completed"
    assert result["requested_seconds"] == 1
    assert result["elapsed_seconds"] >= 1
    assert result["codex_invoked"] is False
    assert runtime.bridge.start_count == 0


@pytest.mark.asyncio
async def test_mcp_long_wait_progress_probe_completes_without_progress_token_or_codex(
    tmp_path,
) -> None:
    runtime = FakeRuntime()
    app = create_app(config(tmp_path), runtime_factory=lambda _: runtime)
    tool = next(
        tool
        for tool in app.state.mcp_server._tool_manager.list_tools()
        if tool.name == "mcp_long_wait_progress_probe"
    )
    context, session = probe_context(meta={})

    result = await tool.fn(seconds=1, interval_seconds=1, ctx=context)

    assert result["status"] == "completed"
    assert result["requested_seconds"] == 1
    assert result["interval_seconds"] == 1
    assert result["elapsed_seconds"] >= 1
    assert result["progress_token_present"] is False
    assert result["progress_token_type"] is None
    assert result["notifications_attempted"] == 2
    assert len(session.progress_attempts) == 2
    assert result["codex_invoked"] is False
    assert runtime.bridge.start_count == 0


@pytest.mark.asyncio
async def test_mcp_progress_token_probe_omits_raw_request_metadata_values(tmp_path) -> None:
    runtime = FakeRuntime()
    app = create_app(config(tmp_path), runtime_factory=lambda _: runtime)
    tool = next(
        tool
        for tool in app.state.mcp_server._tool_manager.list_tools()
        if tool.name == "mcp_progress_token_probe"
    )
    capabilities = types.ClientCapabilities(extensions={"example-safe-capability": {}})
    context, _ = probe_context(
        meta={
            "io.modelcontextprotocol/clientInfo": {
                "name": "openai-mcp",
                "version": "1.0.0",
                "secret": "DO_NOT_LEAK",
            },
            "progress_token": "secret-progress-token",
            "openai/session": "secret-session",
            "openai/subject": "secret-subject",
            "openai/organization": "secret-org",
            "openai/userLocation": {
                "city": "Secret City",
                "country": "Secret Country",
                "coordinates": "SECRET_COORDINATES",
            },
            "user/custom": "secret-custom-value",
            "locale": "ja-JP",
            "timezone": "Asia/Tokyo",
            "timezone_offset_minutes": 540,
        },
        client_capabilities=capabilities,
    )

    result = await tool.fn(context)
    serialized = json.dumps(result, sort_keys=True)

    assert set(result) == {
        "probe",
        "progress_token_present",
        "progress_token_type",
        "request_meta_keys",
        "protocol_version",
        "client_info",
        "client_capabilities",
        "locale",
        "timezone",
        "timezone_offset_minutes",
        "codex_invoked",
    }
    assert result["probe"] == "mcp_progress_token_probe"
    assert result["progress_token_present"] is True
    assert result["progress_token_type"] == "str"
    assert result["request_meta_keys"] == [
        "io.modelcontextprotocol/clientInfo",
        "locale",
        "openai/organization",
        "openai/session",
        "openai/subject",
        "openai/userLocation",
        "progress_token",
        "timezone",
        "timezone_offset_minutes",
        "user/custom",
    ]
    assert result["protocol_version"] == "2026-07-28"
    assert result["client_info"] == {"name": "openai-mcp", "version": "1.0.0"}
    assert result["client_capabilities"]["extensions"] == {"example-safe-capability": {}}
    assert result["locale"] == "ja-JP"
    assert result["timezone"] == "Asia/Tokyo"
    assert result["timezone_offset_minutes"] == 540
    assert result["codex_invoked"] is False
    for sensitive_value in (
        "secret-progress-token",
        "secret-session",
        "secret-subject",
        "secret-org",
        "Secret City",
        "Secret Country",
        "SECRET_COORDINATES",
        "secret-custom-value",
        "DO_NOT_LEAK",
    ):
        assert sensitive_value not in serialized
    assert runtime.bridge.start_count == 0


@pytest.mark.asyncio
async def test_mcp_progress_token_probe_omits_absent_or_malformed_client_info(
    tmp_path,
) -> None:
    runtime = FakeRuntime()
    app = create_app(config(tmp_path), runtime_factory=lambda _: runtime)
    tool = next(
        tool
        for tool in app.state.mcp_server._tool_manager.list_tools()
        if tool.name == "mcp_progress_token_probe"
    )
    client_info_values = (
        None,
        "invalid",
        {"name": 123, "version": ["x"]},
        {"name": "openai-mcp", "version": 1},
    )

    for client_info in client_info_values:
        meta = {} if client_info is None else {"io.modelcontextprotocol/clientInfo": client_info}
        context, _ = probe_context(meta=meta)

        result = await tool.fn(context)

        assert result["client_info"] is None


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
async def test_unknown_tool_is_invalid_params_at_low_level_client_boundary(tmp_path) -> None:
    runtime = FakeRuntime()
    app = create_app(config(tmp_path), runtime_factory=lambda _: runtime)

    async with app.router.lifespan_context(app):
        client_to_server_send, client_to_server_receive = anyio.create_memory_object_stream(0)
        server_to_client_send, server_to_client_receive = anyio.create_memory_object_stream(0)
        wire_server: LowLevelServer = app.state.mcp_lowlevel_server

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(
                wire_server.run,
                client_to_server_receive,
                server_to_client_send,
                wire_server.create_initialization_options(),
            )
            async with ClientSession(server_to_client_receive, client_to_server_send) as client:
                await client.initialize()
                with pytest.raises(MCPError) as exc_info:
                    await client.call_tool("missing", {})

                assert exc_info.value.code == types.INVALID_PARAMS
                assert exc_info.value.code != types.INTERNAL_ERROR
                assert "missing" in exc_info.value.message
            task_group.cancel_scope.cancel()


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
        "codex_targets",
        "Never infer a target",
        "thread_id carries routing affinity",
        "ユーザーに選択を求めて",
        "codex_setup",
        "confirmed target/model/reasoning",
    ):
        assert anchor in instructions

    initialization_options = app.state.mcp_server._lowlevel_server.create_initialization_options()
    assert initialization_options.instructions == instructions


@pytest.mark.asyncio
async def test_execution_target_tool_schemas_are_explicit_and_optional(tmp_path) -> None:
    app = create_app(config(tmp_path), runtime_factory=lambda _: FakeRuntime())
    tools = {tool.name: tool for tool in await app.state.mcp_server.list_tools()}

    assert "codex_targets" in tools
    assert "target_id" in tools["codex_start"].input_schema["properties"]
    assert "target_id" not in tools["codex_start"].input_schema.get("required", [])
    assert "target_id" in tools["codex_threads"].input_schema["properties"]
    assert "target_id" not in tools["codex_threads"].input_schema.get("required", [])


def test_setup_tools_bind_the_app_resource_and_app_only_visibility(tmp_path) -> None:
    app = create_app(config(tmp_path), runtime_factory=lambda _: FakeRuntime())
    tools = {tool.name: tool for tool in app.state.mcp_server._tool_manager.list_tools()}

    assert tools["codex_setup"].meta["ui"]["resourceUri"] == "ui://codexbridge/setup/app.html"
    assert tools["codex_setup"].meta["ui"].get("visibility") is None
    for name in ("codex_setup_capabilities", "codex_setup_confirm"):
        assert tools[name].meta["ui"]["visibility"] == ["app"]
    assert all(
        "ui" not in (tool.meta or {})
        for name, tool in tools.items()
        if name not in {"codex_setup", "codex_setup_capabilities", "codex_setup_confirm"}
    )


@pytest.mark.asyncio
async def test_wire_server_advertises_apps_and_reads_setup_html_resource(tmp_path) -> None:
    app = create_app(config(tmp_path), runtime_factory=lambda _: FakeRuntime())
    lowlevel = app.state.mcp_lowlevel_server
    initialization = lowlevel.create_initialization_options()
    assert "io.modelcontextprotocol/ui" in initialization.capabilities.extensions

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
                discover_result = await client.discover()
                assert "io.modelcontextprotocol/ui" in discover_result.capabilities.extensions
                result = await client.read_resource("ui://codexbridge/setup/app.html")
                assert result.contents[0].mime_type == "text/html;profile=mcp-app"
                assert "CodexBridge Setup" in result.contents[0].text
                assert "cdn.jsdelivr.net" not in result.contents[0].text
                assert "unpkg.com" not in result.contents[0].text
                capabilities = await client.call_tool(
                    "codex_setup_capabilities", {"target_id": "local"}
                )
                assert capabilities.structured_content["models"][0]["model"] == "test-model"
                confirmed = await client.call_tool(
                    "codex_setup_confirm",
                    {
                        "target_id": "local",
                        "model": "test-model",
                        "reasoning_effort": "effort-a",
                    },
                )
                assert confirmed.structured_content["confirmed"] is True
            task_group.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_text_only_codex_setup_returns_target_ids_and_selection_guidance(
    tmp_path, monkeypatch
) -> None:
    settings = replace(
        config(tmp_path),
        targets=(
            ExecutionTargetConfig("main-pc", "Main PC", "local"),
            ExecutionTargetConfig(
                "notebook", "Notebook", "remote", "https://notebook.example.test/mcp"
            ),
        ),
    )

    class FakeSetupRouter:
        def __init__(self, *_args: Any) -> None:
            pass

        async def start(self) -> None:
            return None

        async def close(self) -> None:
            return None

        async def target_list(self) -> dict[str, Any]:
            return {
                "targets": [
                    {"id": "main-pc", "name": "Main PC", "kind": "local", "available": True},
                    {
                        "id": "notebook",
                        "name": "Notebook",
                        "kind": "remote",
                        "available": False,
                    },
                ],
                "selection_required": True,
            }

    monkeypatch.setattr("codex_bridge.server.ExecutionTargetRouter", FakeSetupRouter)
    app = create_app(settings, runtime_factory=lambda _: FakeRuntime())
    lowlevel = app.state.mcp_lowlevel_server
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
                await client.initialize()
                result = await client.call_tool("codex_setup", {})
                content = result.structured_content
                assert content["selection_required"] is True
                assert "main-pc" in content["message"]
                assert "notebook" in content["message"]
                assert "selection" in content["message"].lower()
                assert "MCP Apps support" in content["message"]
                assert "notebook.example.test" not in str(content)
            task_group.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_setup_confirm_tool_returns_explicit_validated_selection(tmp_path) -> None:
    app = create_app(config(tmp_path), runtime_factory=lambda _: FakeRuntime())
    tool = next(
        tool
        for tool in app.state.mcp_server._tool_manager.list_tools()
        if tool.name == "codex_setup_confirm"
    )

    async with app.router.lifespan_context(app):
        result = await tool.fn("local", "test-model", "effort-a")

    assert result == {
        "confirmed": True,
        "selection": {
            "target_id": "local",
            "target_name": "Local PC",
            "model": "test-model",
            "reasoning_effort": "effort-a",
        },
    }


@pytest.mark.asyncio
async def test_legacy_start_without_target_uses_existing_local_bridge(tmp_path) -> None:
    runtime = FakeRuntime()
    app = create_app(config(tmp_path), runtime_factory=lambda _: runtime)
    start = next(
        tool
        for tool in app.state.mcp_server._tool_manager.list_tools()
        if tool.name == "codex_start"
    )

    async with app.router.lifespan_context(app):
        result = await start.fn(str(tmp_path), "legacy prompt")

    assert result["target_id"] == "local"
    assert runtime.bridge.start_count == 1


@pytest.mark.asyncio
async def test_codex_targets_returns_local_identity_without_connection_details(tmp_path) -> None:
    app = create_app(config(tmp_path), runtime_factory=lambda _: FakeRuntime())
    targets_tool = next(
        tool
        for tool in app.state.mcp_server._tool_manager.list_tools()
        if tool.name == "codex_targets"
    )

    result = await targets_tool.fn()

    assert result == {
        "targets": [{"id": "local", "name": "Local PC", "kind": "local", "available": True}],
        "selection_required": False,
    }
    assert "url" not in result["targets"][0]


@pytest.mark.asyncio
async def test_multiple_target_start_selection_error_is_a_tool_error(tmp_path) -> None:
    settings = replace(
        config(tmp_path),
        targets=(
            ExecutionTargetConfig("main-pc", "Main PC", "local"),
            ExecutionTargetConfig(
                "notebook", "Notebook", "remote", "https://notebook.example.test/mcp"
            ),
        ),
    )
    app = create_app(settings, runtime_factory=lambda _: FakeRuntime())
    start = next(
        tool
        for tool in app.state.mcp_server._tool_manager.list_tools()
        if tool.name == "codex_start"
    )

    with pytest.raises(ToolError, match="target_id is required.*codex_targets"):
        await start.fn("Z:/notebook/repo", "prompt")


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
