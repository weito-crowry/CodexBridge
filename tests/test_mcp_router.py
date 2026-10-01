from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import pytest
from mcp import MCPError, types
from mcp.server import MCPServer

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
        return types.CallToolResult(content=[types.TextContent(type="text", text="remote-result")])


def remote_config(**overrides: object) -> GitHubMcpConfig:
    values: dict[str, object] = {"enabled": True, "pat": "secret"}
    values.update(overrides)
    return GitHubMcpConfig(**values)


def upstream_tool(name: str) -> types.Tool:
    return types.Tool(name=name, inputSchema={"type": "object"})


def native_server() -> MCPServer:
    server = MCPServer("native")

    @server.tool()
    async def native_echo(value: str) -> dict[str, str]:
        return {"value": value}

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


@pytest.mark.asyncio
async def test_enabled_remote_with_no_exposed_tools_fails_startup() -> None:
    remote = FakeRemote(remote_config(), ())
    router = ToolRouter(native_server(), remote)

    with pytest.raises(ValueError, match="no exposed tools"):
        await router.start()

    await router.shutdown()
