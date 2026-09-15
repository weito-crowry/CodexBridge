from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from mcp import types
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from codex_bridge.config import GitHubMcpConfig
from codex_bridge.mcp_remote import RemoteMcpError
from codex_bridge.mcp_router import ToolRouter


@dataclass
class FakeRemote:
    config: GitHubMcpConfig
    upstream_tools: tuple[types.Tool, ...]
    start_count: int = 0
    close_count: int = 0
    calls: list[tuple[str, dict[str, Any]]] | None = None
    error: Exception | None = None

    def __post_init__(self) -> None:
        self.calls = []

    async def start(self) -> None:
        self.start_count += 1

    async def close(self) -> None:
        self.close_count += 1

    async def call_tool(self, name: str, arguments: dict[str, Any]):
        assert self.calls is not None
        self.calls.append((name, arguments))
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
            name="github_get_file_contents", arguments={"path": "README.md"}
        ),
    )

    assert native_result.structured_content == {"value": "native"}
    assert remote_result.content[0].text == "remote-result"
    assert remote.calls == [("get_file_contents", {"path": "README.md"})]
    await router.shutdown()


@pytest.mark.asyncio
async def test_router_unknown_and_disconnected_remote_calls_are_tool_errors() -> None:
    remote = FakeRemote(remote_config(), (upstream_tool("read_tool"),))
    router = ToolRouter(native_server(), remote)
    await router.start()

    with pytest.raises(ToolError, match="Unknown tool"):
        await router.call_tool(None, types.CallToolRequestParams(name="missing", arguments={}))

    remote.error = RemoteMcpError("GitHub Remote MCP is disconnected")
    with pytest.raises(ToolError, match="disconnected"):
        await router.call_tool(
            None, types.CallToolRequestParams(name="github_read_tool", arguments={})
        )
    assert router.snapshot is not None
    assert [tool.name for tool in router.snapshot.tools] == [
        "native_echo",
        "github_read_tool",
    ]
    await router.shutdown()


@pytest.mark.asyncio
async def test_enabled_remote_with_no_exposed_tools_fails_startup() -> None:
    remote = FakeRemote(remote_config(), ())
    router = ToolRouter(native_server(), remote)

    with pytest.raises(ValueError, match="no exposed tools"):
        await router.start()

    await router.shutdown()
