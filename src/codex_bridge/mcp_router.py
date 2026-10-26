from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from mcp import MCPError, types
from mcp.server import MCPServer, ServerRequestContext

from .config import GitHubMcpConfig
from .logging_utils import log_event
from .mcp_catalog import CatalogError, CatalogListToolsResult, CatalogSnapshot, build_catalog
from .mcp_remote import RemoteMcpError, RemoteMcpProvider

_LEGACY_PROTOCOL_VERSIONS = frozenset({"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"})
_MODERN_PROTOCOL_VERSION = "2026-07-28"


class RemoteProviderLike(Protocol):
    @property
    def config(self) -> GitHubMcpConfig: ...

    @property
    def upstream_tools(self) -> tuple[types.Tool, ...]: ...

    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def call_tool(
        self,
        upstream_name: str,
        arguments: dict[str, Any] | None,
        *,
        input_responses: types.InputResponses | None = None,
        request_state: str | None = None,
        meta: types.RequestParamsMeta | None = None,
    ) -> types.CallToolResult | types.InputRequiredResult: ...


@dataclass(frozen=True, slots=True)
class ToolRoute:
    provider: str
    upstream_name: str


class ToolRouter:
    """Route one MCP namespace over native tools and one remote provider."""

    def __init__(
        self,
        native_server: MCPServer,
        remote_provider: RemoteProviderLike | None = None,
    ) -> None:
        self.native_server = native_server
        self.remote_provider = remote_provider or RemoteMcpProvider(GitHubMcpConfig())
        self._snapshot: CatalogSnapshot | None = None
        self._routes: dict[str, ToolRoute] = {}

    @property
    def snapshot(self) -> CatalogSnapshot | None:
        return self._snapshot

    async def start(self) -> None:
        if self._snapshot is not None:
            return
        native_tools = tuple(await self.native_server.list_tools())
        await self.remote_provider.start()
        snapshot = build_catalog(
            native_tools,
            self.remote_provider.upstream_tools,
            self.remote_provider.config,
        )
        if self.remote_provider.config.enabled and snapshot.exposed_remote_tool_count == 0:
            raise CatalogError("enabled GitHub MCP has no exposed tools")

        routes = {tool.name: ToolRoute("native", tool.name) for tool in snapshot.native_tools}
        prefix = self.remote_provider.config.prefix
        for tool in snapshot.remote_tools:
            routes[tool.name] = ToolRoute("github", tool.name[len(prefix) :])
        self._routes = routes
        self._snapshot = snapshot
        log_event(
            "mcp.catalog.snapshot",
            provider="codexbridge",
            native_tool_count=snapshot.native_tool_count,
            upstream_tool_count=snapshot.upstream_tool_count,
            exposed_remote_tool_count=snapshot.exposed_remote_tool_count,
            total_tool_count=snapshot.total_tool_count,
            serialized_schema_bytes=snapshot.serialized_schema_bytes,
            catalog_sha256=snapshot.catalog_sha256,
        )
        if self.remote_provider.config.enabled:
            log_event(
                "mcp.mount.ready",
                provider="github",
                upstream_tool_count=snapshot.upstream_tool_count,
                exposed_remote_tool_count=snapshot.exposed_remote_tool_count,
            )

    async def shutdown(self) -> None:
        await self.remote_provider.close()

    async def list_tools(
        self,
        context: ServerRequestContext[Any] | None,
        _params: types.PaginatedRequestParams | None,
    ) -> CatalogListToolsResult:
        snapshot = self._snapshot
        if snapshot is None:
            raise RuntimeError("MCP tool router is not started")
        tools = list(snapshot.tools)
        if context is not None:
            legacy_protocol = _is_legacy_protocol(context)
            if legacy_protocol:
                tools = [
                    tool.model_copy(update={"output_schema": None})
                    if tool.output_schema is not None and tool.output_schema.get("type") != "object"
                    else tool
                    for tool in snapshot.tools
                ]
        log_event(
            "mcp.tools.list",
            provider="codexbridge",
            total_tool_count=snapshot.total_tool_count,
            catalog_sha256=snapshot.catalog_sha256,
            serialized_schema_bytes=snapshot.serialized_schema_bytes,
            native_tool_count=snapshot.native_tool_count,
            exposed_remote_tool_count=snapshot.exposed_remote_tool_count,
        )
        return CatalogListToolsResult.model_construct(
            meta=None,
            ttl_ms=0,
            cache_scope="private",
            next_cursor=None,
            tools=tools,
            result_type="complete",
        )

    async def call_tool(
        self,
        context: ServerRequestContext[Any] | None,
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult | types.InputRequiredResult:
        legacy_protocol = _is_legacy_protocol(context) if context is not None else False
        route = self._routes.get(params.name)
        if route is None:
            raise MCPError(
                code=types.INVALID_PARAMS,
                message=f"Unknown tool: {params.name}",
            ) from None
        if route.provider == "native":
            arguments = params.arguments or {}
            if context is None:
                return await self.native_server.call_tool(route.upstream_name, arguments)
            return await self.native_server._handle_call_tool(context, params)
        request_meta = params.meta
        if request_meta is None and context is not None:
            request_meta = context.meta
        try:
            result = await self.remote_provider.call_tool(
                route.upstream_name,
                params.arguments,
                input_responses=params.input_responses,
                request_state=params.request_state,
                meta=request_meta,
            )
            if legacy_protocol and isinstance(result, types.CallToolResult):
                return _legacy_call_result(result)
            return result
        except RemoteMcpError as exc:
            message = str(exc) or "GitHub Remote MCP call failed"
            pat = self.remote_provider.config.pat
            if pat:
                message = message.replace(pat, "[redacted]")
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=message)],
                is_error=True,
            )


def _is_legacy_protocol(context: ServerRequestContext[Any]) -> bool:
    protocol_version = context.protocol_version
    if protocol_version in _LEGACY_PROTOCOL_VERSIONS:
        return True
    if protocol_version == _MODERN_PROTOCOL_VERSION:
        return False
    raise MCPError(
        code=types.UNSUPPORTED_PROTOCOL_VERSION,
        message=f"Unsupported MCP protocol version: {protocol_version}",
    ) from None


def _legacy_call_result(result: types.CallToolResult) -> types.CallToolResult:
    if "structured_content" not in result.model_fields_set or isinstance(
        result.structured_content, dict
    ):
        return result

    structured_text = json.dumps(
        result.structured_content,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    content = list(result.content)
    if not any(
        isinstance(item, types.TextContent) and item.text == structured_text for item in content
    ):
        content.append(types.TextContent(type="text", text=structured_text))
    return result.model_copy(update={"content": content, "structured_content": None})
