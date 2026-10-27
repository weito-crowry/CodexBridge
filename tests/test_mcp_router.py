from __future__ import annotations

import logging
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
from mcp import MCPError, types
from mcp.client.session import ClientSession
from mcp.server import MCPServer
from mcp.server.lowlevel.server import Server as LowLevelMCPServer
from mcp.shared.memory import create_client_server_memory_streams

from codex_bridge.config import GitHubMcpConfig
from codex_bridge.mcp_remote import RemoteMcpError
from codex_bridge.mcp_router import ToolRouter


@dataclass
class FakeRemote:
    config: GitHubMcpConfig
    upstream_tools: tuple[types.Tool, ...]
    start_count: int = 0
    close_count: int = 0
    calls: list[tuple[str, dict[str, Any] | None, dict[str, Any]]] | None = None
    error: Exception | None = None
    result: types.CallToolResult | types.InputRequiredResult | None = None

    def __post_init__(self) -> None:
        self.calls = []

    async def start(self) -> None:
        self.start_count += 1

    async def close(self) -> None:
        self.close_count += 1

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        *,
        input_responses: object = None,
        request_state: str | None = None,
        meta: object = None,
    ):
        assert self.calls is not None
        self.calls.append(
            (
                name,
                arguments,
                {
                    "input_responses": input_responses,
                    "request_state": request_state,
                    "meta": meta,
                },
            )
        )
        if self.error is not None:
            raise self.error
        if self.result is not None:
            return self.result
        return types.CallToolResult(content=[types.TextContent(type="text", text="remote-result")])


def remote_config(**overrides: object) -> GitHubMcpConfig:
    values: dict[str, object] = {"enabled": True, "pat": "secret"}
    values.update(overrides)
    return GitHubMcpConfig(**values)


def upstream_tool(name: str) -> types.Tool:
    return types.Tool(name=name, inputSchema={"type": "object"})


async def _sdk_round_trip(
    router: ToolRouter,
    protocol_version: str,
    *,
    call_native_array: bool = False,
) -> tuple[
    types.ListToolsResult,
    types.CallToolResult | types.InputRequiredResult,
    types.CallToolResult | types.InputRequiredResult | None,
    types.CallToolResult | types.InputRequiredResult | MCPError,
]:
    server = LowLevelMCPServer(
        "CodexBridge test",
        on_list_tools=router.list_tools,
        on_call_tool=router.call_tool,
    )
    initialization_options = server.create_initialization_options()

    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(server.run, *server_streams, initialization_options)
            try:
                async with ClientSession(*client_streams) as client:
                    if protocol_version == "2026-07-28":
                        client.adopt(
                            types.DiscoverResult(
                                supportedVersions=[protocol_version],
                                capabilities=types.ServerCapabilities(),
                            )
                        )
                        discovered = await client.send_request(
                            types.DiscoverRequest(),
                            types.DiscoverResult,
                        )
                        client.adopt(discovered)
                    else:
                        initialized = await client.send_request(
                            types.InitializeRequest(
                                params=types.InitializeRequestParams(
                                    protocolVersion=protocol_version,
                                    capabilities=types.ClientCapabilities(),
                                    clientInfo=types.Implementation(
                                        name="CodexBridge test",
                                        version="1",
                                    ),
                                ),
                            ),
                            types.InitializeResult,
                        )
                        assert initialized.protocol_version == protocol_version
                        client.adopt(initialized)
                        await client.send_notification(types.InitializedNotification())

                    tools = await client.list_tools()
                    native_result = await client.call_tool("native_echo", {"value": "native"})
                    native_array_result = (
                        await client.call_tool("native_array") if call_native_array else None
                    )
                    try:
                        result = await client.call_tool(
                            "github_read_tool",
                            {"path": "README.md"},
                        )
                    except MCPError as exc:
                        result = exc
                    return tools, native_result, native_array_result, result
            finally:
                task_group.cancel_scope.cancel()


def native_server(*, include_array: bool = False) -> MCPServer:
    server = MCPServer("native")

    @server.tool()
    async def native_echo(value: str) -> dict[str, str]:
        return {"value": value}

    if include_array:

        @server.tool()
        async def native_array() -> list[str]:
            return ["native", "array"]

    return server


@pytest.mark.asyncio
async def test_router_combines_native_and_prefixed_remote_catalog() -> None:
    remote = FakeRemote(remote_config(), (upstream_tool("get_file_contents"),))
    router = ToolRouter(native_server(), remote)

    await router.start()
    result = await router.list_tools(None, None)

    assert [tool.name for tool in result.tools] == [
        "native_echo",
        "github_get_file_contents",
    ]
    assert router.snapshot is not None
    assert router.snapshot.native_tool_count == 1
    assert router.snapshot.exposed_remote_tool_count == 1
    assert router.snapshot.total_tool_count == 2
    await router.shutdown()
    assert remote.start_count == 1
    assert remote.close_count == 1


@pytest.mark.asyncio
async def test_router_dispatches_native_and_original_remote_name() -> None:
    remote = FakeRemote(remote_config(), (upstream_tool("get_file_contents"),))
    router = ToolRouter(native_server(), remote)
    await router.start()

    native_result = await router.call_tool(
        None,
        types.CallToolRequestParams(name="native_echo", arguments={"value": "native"}),
    )
    remote_result = await router.call_tool(
        None,
        types.CallToolRequestParams(
            name="github_get_file_contents",
            arguments={"path": "README.md"},
            input_responses={},
            request_state="resume-state",
            meta={"request": "metadata"},
        ),
    )

    assert native_result.structured_content == {"value": "native"}
    assert remote_result.content[0].text == "remote-result"
    assert remote.calls == [
        (
            "get_file_contents",
            {"path": "README.md"},
            {
                "input_responses": {},
                "request_state": "resume-state",
                "meta": {"request": "metadata"},
            },
        )
    ]
    await router.shutdown()


@pytest.mark.asyncio
async def test_router_unknown_and_remote_execution_errors_use_expected_protocol_shapes() -> None:
    remote = FakeRemote(remote_config(), (upstream_tool("read_tool"),))
    router = ToolRouter(native_server(), remote)
    await router.start()

    with pytest.raises(MCPError, match="Unknown tool") as exc_info:
        await router.call_tool(None, types.CallToolRequestParams(name="missing", arguments={}))
    assert exc_info.value.code == types.INVALID_PARAMS

    remote.error = RemoteMcpError("GitHub Remote MCP is disconnected")
    result = await router.call_tool(
        None, types.CallToolRequestParams(name="github_read_tool", arguments={})
    )
    assert isinstance(result, types.CallToolResult)
    assert result.is_error is True
    assert "disconnected" in result.content[0].text

    remote.error = RemoteMcpError("GitHub Remote MCP call outcome unknown: secret")
    outcome = await router.call_tool(
        None, types.CallToolRequestParams(name="github_read_tool", arguments={})
    )
    assert isinstance(outcome, types.CallToolResult)
    assert outcome.is_error is True
    assert "outcome unknown" in outcome.content[0].text
    assert "secret" not in outcome.content[0].text
    assert router.snapshot is not None
    assert [tool.name for tool in router.snapshot.tools] == [
        "native_echo",
        "github_read_tool",
    ]
    await router.shutdown()


@pytest.mark.asyncio
async def test_router_list_tools_logs_catalog_fingerprint(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="codex_bridge")
    remote = FakeRemote(remote_config(), (upstream_tool("read_tool"),))
    router = ToolRouter(native_server(), remote)

    await router.start()
    await router.list_tools(None, None)

    assert router.snapshot is not None
    assert "mcp.tools.list" in caplog.text
    assert f"total_tool_count={router.snapshot.total_tool_count}" in caplog.text
    assert f"catalog_sha256={router.snapshot.catalog_sha256}" in caplog.text
    assert f"serialized_schema_bytes={router.snapshot.serialized_schema_bytes}" in caplog.text
    assert f"native_tool_count={router.snapshot.native_tool_count}" in caplog.text
    assert f"exposed_remote_tool_count={router.snapshot.exposed_remote_tool_count}" in caplog.text
    await router.shutdown()


@pytest.mark.parametrize(
    ("schema", "expected_schema"),
    [
        (None, False),
        ({"type": "object", "properties": {"ok": {"type": "boolean"}}}, True),
        ({"type": ["object", "null"]}, False),
        ({"type": ["null", "object"]}, False),
        ({"type": ["null", "array"]}, False),
        ({"type": "array"}, False),
        ({"type": "null"}, False),
        ({"type": "string"}, False),
        ({"oneOf": [{"type": "object"}, {"type": "null"}]}, False),
    ],
)
@pytest.mark.asyncio
async def test_legacy_tools_list_projects_only_object_root_output_schemas(
    schema: dict[str, Any] | None,
    expected_schema: bool,
) -> None:
    remote_tool = types.Tool(
        name="read_tool",
        title="Read tool",
        description="Reads a file",
        inputSchema={"type": "object", "properties": {"path": {"type": "string"}}},
        outputSchema=schema,
        annotations=types.ToolAnnotations(readOnlyHint=True),
        icons=[types.Icon(src="https://example.test/icon.png")],
        _meta={"origin": "upstream"},
    )
    remote = FakeRemote(remote_config(), (remote_tool,))
    router = ToolRouter(native_server(), remote)
    await router.start()

    result = await router.list_tools(SimpleNamespace(protocol_version="2025-11-25"), None)

    assert len(result.tools) == 2
    exposed = result.tools[1]
    assert exposed.name == "github_read_tool"
    assert (exposed.output_schema is not None) is expected_schema
    assert exposed.title == "Read tool"
    assert exposed.description == "Reads a file"
    assert exposed.input_schema == remote_tool.input_schema
    assert exposed.annotations == remote_tool.annotations
    assert exposed.icons == remote_tool.icons
    assert exposed.meta == remote_tool.meta
    assert router.snapshot is not None
    assert router.snapshot.remote_tools[0].output_schema == schema
    await router.shutdown()


@pytest.mark.asyncio
async def test_contextless_tools_list_keeps_current_catalog_shape() -> None:
    schema = {"type": ["object", "null"]}
    remote = FakeRemote(
        remote_config(),
        (types.Tool(name="read_tool", inputSchema={"type": "object"}, outputSchema=schema),),
    )
    router = ToolRouter(native_server(), remote)
    await router.start()

    result = await router.list_tools(None, None)

    assert result.tools[1].output_schema == schema
    await router.shutdown()


@pytest.mark.parametrize(
    "protocol_version",
    ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"],
)
@pytest.mark.asyncio
async def test_mcp_sdk_serializes_legacy_catalog_and_non_object_tool_results(
    protocol_version: str,
) -> None:
    schema = {"type": ["array", "null"], "items": {"type": "string"}}
    remote_tool = types.Tool(
        name="read_tool",
        title="Read tool",
        description="Reads a file",
        inputSchema={"type": "object", "properties": {"path": {"type": "string"}}},
        outputSchema=schema,
        annotations=types.ToolAnnotations(readOnlyHint=True),
        icons=[types.Icon(src="https://example.test/icon.png")],
        _meta={"origin": "upstream"},
    )
    remote = FakeRemote(
        remote_config(),
        (remote_tool,),
        result=types.CallToolResult(
            content=[types.TextContent(type="text", text="existing")],
            structuredContent=["file contents"],
            isError=True,
            _meta={"origin": "remote-result"},
        ),
    )
    router = ToolRouter(native_server(), remote)
    await router.start()

    tools, native_result, _native_array_result, call_result = await _sdk_round_trip(
        router,
        protocol_version,
    )

    exposed = next(item for item in tools.tools if item.name == "github_read_tool")
    assert exposed.output_schema is None
    assert native_result.structured_content == {"value": "native"}
    assert isinstance(call_result, types.CallToolResult)
    assert call_result.structured_content is None
    assert [item.text for item in call_result.content if isinstance(item, types.TextContent)] == [
        "existing",
        '["file contents"]',
    ]
    assert call_result.is_error is True
    assert call_result.meta == {"origin": "remote-result"}
    assert router.snapshot is not None
    assert router.snapshot.remote_tools[0].output_schema == schema
    await router.shutdown()


@pytest.mark.asyncio
async def test_mcp_sdk_keeps_modern_catalog_and_non_object_tool_results_unchanged() -> None:
    schema = {"oneOf": [{"type": "array"}, {"type": "null"}]}
    remote_tool = types.Tool(
        name="read_tool",
        inputSchema={"type": "object"},
        outputSchema=schema,
    )
    original_result = types.CallToolResult(
        content=[types.TextContent(type="text", text="existing")],
        structuredContent=["file contents"],
        isError=True,
    )
    remote = FakeRemote(remote_config(), (remote_tool,), result=original_result)
    router = ToolRouter(native_server(), remote)
    await router.start()

    tools, native_result, _native_array_result, call_result = await _sdk_round_trip(
        router,
        "2026-07-28",
    )

    exposed = next(item for item in tools.tools if item.name == "github_read_tool")
    assert exposed.output_schema == schema
    assert native_result.structured_content == {"value": "native"}
    assert call_result.structured_content == ["file contents"]
    assert [item.text for item in call_result.content if isinstance(item, types.TextContent)] == [
        "existing"
    ]
    assert call_result.is_error is True
    assert router.snapshot is not None
    assert router.snapshot.remote_tools[0].output_schema == schema
    assert original_result.structured_content == ["file contents"]
    await router.shutdown()


@pytest.mark.asyncio
async def test_catalog_projection_is_request_scoped_and_does_not_mutate_snapshot() -> None:
    schema = {"oneOf": [{"type": "object"}, {"type": "null"}]}
    remote = FakeRemote(
        remote_config(),
        (
            types.Tool(name="z_tool", inputSchema={"type": "object"}, outputSchema=schema),
            types.Tool(name="a_tool", inputSchema={"type": "object"}),
        ),
    )
    router = ToolRouter(native_server(), remote)
    await router.start()
    original_snapshot = router.snapshot
    assert original_snapshot is not None
    original_tools = original_snapshot.tools
    original_digest = original_snapshot.catalog_sha256

    legacy_first = await router.list_tools(SimpleNamespace(protocol_version="2025-11-25"), None)
    modern = await router.list_tools(SimpleNamespace(protocol_version="2026-07-28"), None)
    legacy_second = await router.list_tools(SimpleNamespace(protocol_version="2025-11-25"), None)

    expected_names = ["native_echo", "github_a_tool", "github_z_tool"]
    assert [tool.name for tool in legacy_first.tools] == expected_names
    assert [tool.name for tool in modern.tools] == expected_names
    assert [tool.name for tool in legacy_second.tools] == expected_names
    assert legacy_first.tools[2].output_schema is None
    assert modern.tools[2].output_schema == schema
    assert legacy_second.tools[2].output_schema is None
    assert router.snapshot is original_snapshot
    assert router.snapshot.tools == original_tools
    assert router.snapshot.catalog_sha256 == original_digest
    await router.shutdown()


@pytest.mark.asyncio
async def test_legacy_tool_call_preserves_object_null_absence_and_avoids_duplicate_text() -> None:
    remote = FakeRemote(remote_config(), (upstream_tool("read_tool"),))
    router = ToolRouter(native_server(), remote)
    await router.start()
    context = SimpleNamespace(protocol_version="2025-11-25", meta=None)
    params = types.CallToolRequestParams(name="github_read_tool", arguments={})

    remote.result = types.CallToolResult(
        content=[
            types.TextContent(type="text", text="[1,2]"),
            types.TextContent(type="text", text="existing"),
        ],
        structuredContent=[1, 2],
        isError=True,
        _meta={"source": "upstream"},
    )
    array_result = await router.call_tool(context, params)
    assert isinstance(array_result, types.CallToolResult)
    assert array_result.structured_content is None
    assert [item.text for item in array_result.content if isinstance(item, types.TextContent)] == [
        "[1,2]",
        "existing",
    ]
    assert array_result.is_error is True
    assert array_result.meta == {"source": "upstream"}

    remote.result = types.CallToolResult(
        content=[types.TextContent(type="text", text="existing")],
        structuredContent=None,
    )
    null_result = await router.call_tool(context, params)
    assert isinstance(null_result, types.CallToolResult)
    assert [item.text for item in null_result.content if isinstance(item, types.TextContent)] == [
        "existing",
        "null",
    ]

    remote.result = types.CallToolResult(content=[types.TextContent(type="text", text="existing")])
    absent_result = await router.call_tool(context, params)
    assert isinstance(absent_result, types.CallToolResult)
    assert [item.text for item in absent_result.content if isinstance(item, types.TextContent)] == [
        "existing"
    ]

    object_result = types.CallToolResult(
        content=[types.TextContent(type="text", text="existing")],
        structuredContent={"ok": True},
    )
    remote.result = object_result
    preserved_object = await router.call_tool(context, params)
    assert preserved_object is object_result
    await router.shutdown()


@pytest.mark.asyncio
async def test_unknown_protocol_versions_are_rejected_explicitly() -> None:
    remote = FakeRemote(remote_config(), (upstream_tool("read_tool"),))
    router = ToolRouter(native_server(), remote)
    await router.start()
    context = SimpleNamespace(protocol_version="2099-01-01")

    with pytest.raises(MCPError) as list_error:
        await router.list_tools(context, None)
    with pytest.raises(MCPError) as call_error:
        await router.call_tool(
            context,
            types.CallToolRequestParams(name="github_read_tool", arguments={}),
        )

    assert list_error.value.code == types.UNSUPPORTED_PROTOCOL_VERSION
    assert call_error.value.code == types.UNSUPPORTED_PROTOCOL_VERSION
    await router.shutdown()


@pytest.mark.asyncio
async def test_legacy_sdk_reports_input_required_as_an_error() -> None:
    remote = FakeRemote(
        remote_config(),
        (upstream_tool("read_tool"),),
        result=types.InputRequiredResult(inputRequests={}, requestState="resume-state"),
    )
    router = ToolRouter(native_server(), remote)
    await router.start()

    _tools, _native_result, _native_array_result, result = await _sdk_round_trip(
        router,
        "2025-11-25",
    )

    assert isinstance(result, MCPError)
    assert "invalid result" in result.message.casefold()
    await router.shutdown()


@pytest.mark.asyncio
async def test_legacy_sdk_projects_native_non_object_structured_content() -> None:
    remote = FakeRemote(remote_config(), (upstream_tool("read_tool"),))
    router = ToolRouter(native_server(include_array=True), remote)
    await router.start()

    _tools, _native_result, array_result, _remote_result = await _sdk_round_trip(
        router,
        "2025-11-25",
        call_native_array=True,
    )

    assert isinstance(array_result, types.CallToolResult)
    assert array_result.structured_content == {"result": ["native", "array"]}
    assert [item.text for item in array_result.content if isinstance(item, types.TextContent)] == [
        "native",
        "array",
    ]
    await router.shutdown()


@pytest.mark.parametrize("structured_content", [["value"], None, "value", 42, 1.5, False])
@pytest.mark.asyncio
async def test_modern_tool_calls_keep_non_object_structured_content(
    structured_content: Any,
) -> None:
    original = types.CallToolResult(
        content=[types.TextContent(type="text", text="existing")],
        structuredContent=structured_content,
        isError=True,
    )
    remote = FakeRemote(
        remote_config(),
        (upstream_tool("read_tool"),),
        result=original,
    )
    router = ToolRouter(native_server(), remote)
    await router.start()

    result = await router.call_tool(
        SimpleNamespace(protocol_version="2026-07-28", meta=None),
        types.CallToolRequestParams(name="github_read_tool", arguments={}),
    )

    assert result is original
    assert result.structured_content == structured_content
    assert [item.text for item in result.content if isinstance(item, types.TextContent)] == [
        "existing"
    ]
    assert result.is_error is True
    await router.shutdown()


@pytest.mark.asyncio
async def test_enabled_remote_with_no_exposed_tools_fails_startup() -> None:
    remote = FakeRemote(remote_config(), ())
    router = ToolRouter(native_server(), remote)

    with pytest.raises(ValueError, match="no exposed tools"):
        await router.start()

    await router.shutdown()
