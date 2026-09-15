from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AsyncExitStack
from time import perf_counter
from typing import Any, cast

from mcp import Client, types
from mcp.client.streamable_http import (  # type: ignore[attr-defined]
    create_mcp_http_client,
    streamable_http_client,
)

from .config import GitHubMcpConfig
from .logging_utils import log_event


class RemoteMcpError(RuntimeError):
    """Raised for a safe, user-facing Remote MCP lifecycle or call failure."""


class RemoteMcpProvider:
    """One persistent Streamable HTTP MCP client for the configured GitHub server."""

    def __init__(
        self,
        config: GitHubMcpConfig,
        *,
        http_client_factory: Callable[..., Any] | None = None,
        transport_factory: Callable[..., Any] = streamable_http_client,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config
        self._http_client_factory = http_client_factory or create_mcp_http_client
        self._transport_factory = transport_factory
        self._client_factory = client_factory or Client
        self._stack: AsyncExitStack | None = None
        self._client: Any | None = None
        self._session: Any | None = None
        self._upstream_tools: tuple[types.Tool, ...] = ()
        self._connected = False
        self._disconnect_logged = False
        self._closed = False
        self._lock = asyncio.Lock()

    @property
    def upstream_tools(self) -> tuple[types.Tool, ...]:
        return self._upstream_tools

    @property
    def connected(self) -> bool:
        return self._connected

    async def start(self) -> None:
        if not self.config.enabled:
            return
        async with self._lock:
            if self._connected:
                return
            self._closed = False
            log_event("mcp.mount.connect", provider="github")
            try:
                await self._open(fetch_tools=True)
            except Exception as exc:
                await self._close_stack()
                self._mark_disconnected(type(exc).__name__)
                raise RemoteMcpError("GitHub Remote MCP startup failed") from None

    async def _open(self, *, fetch_tools: bool) -> None:
        await self._close_stack()
        stack = AsyncExitStack()
        try:
            http_client = await stack.enter_async_context(
                self._http_client_factory(
                    headers={"Authorization": f"Bearer {self.config.pat}"},
                )
            )
            transport = self._transport_factory(
                self.config.url,
                http_client=http_client,
                terminate_on_close=True,
            )
            client = self._client_factory(
                transport,
                mode="auto",
                message_handler=self._on_message,
                cache=None,
            )
            await stack.enter_async_context(client)
            session = client.session
            if fetch_tools:
                self._upstream_tools = await self._fetch_all_tools(session)
            self._stack = stack
            self._client = client
            self._session = session
            self._connected = True
            self._disconnect_logged = False
        except BaseException:
            await stack.aclose()
            raise

    async def _fetch_all_tools(self, session: Any) -> tuple[types.Tool, ...]:
        tools: list[types.Tool] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        page = 0
        while True:
            if cursor is None:
                result = await session.list_tools()
            else:
                result = await session.list_tools(
                    params=types.PaginatedRequestParams(cursor=cursor)
                )
            page += 1
            page_tools = tuple(result.tools)
            tools.extend(page_tools)
            log_event(
                "mcp.tools.list",
                provider="github",
                page=page,
                upstream_tool_count=len(page_tools),
            )
            next_cursor = result.next_cursor
            if next_cursor is None:
                return tuple(tools)
            if next_cursor in seen_cursors:
                raise RemoteMcpError("GitHub Remote MCP returned a pagination cycle")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    async def call_tool(
        self,
        upstream_name: str,
        arguments: dict[str, Any] | None,
        *,
        input_responses: types.InputResponses | None = None,
        request_state: str | None = None,
        meta: types.RequestParamsMeta | None = None,
    ) -> types.CallToolResult | types.InputRequiredResult:
        if not self.config.enabled:
            raise RemoteMcpError("GitHub Remote MCP is disabled")
        async with self._lock:
            if not self._connected:
                log_event("mcp.mount.connect", provider="github")
                try:
                    await self._open(fetch_tools=False)
                except Exception as exc:
                    self._mark_disconnected(type(exc).__name__)
                    raise RemoteMcpError("GitHub Remote MCP is disconnected") from None
            session = self._session
            if session is None:
                raise RemoteMcpError("GitHub Remote MCP is disconnected")

            started = perf_counter()
            log_event(
                "mcp.mount.call.start",
                provider="github",
                tool_name=upstream_name,
            )
            try:
                result = await session.call_tool(
                    upstream_name,
                    arguments,
                    input_responses=input_responses,
                    request_state=request_state,
                    meta=meta,
                    allow_input_required=True,
                )
            except Exception:
                self._mark_disconnected("outcome_unknown")
                log_event(
                    "mcp.mount.call.error",
                    provider="github",
                    tool_name=upstream_name,
                    duration=perf_counter() - started,
                    error_category="outcome_unknown",
                )
                raise RemoteMcpError(
                    "GitHub Remote MCP call outcome unknown; the request was not retried"
                ) from None
            log_event(
                "mcp.mount.call.end",
                provider="github",
                tool_name=upstream_name,
                duration=perf_counter() - started,
            )
            return cast(types.CallToolResult | types.InputRequiredResult, result)

    async def handle_transport_failure(self, error: Exception | None = None) -> None:
        async with self._lock:
            self._mark_disconnected(type(error).__name__ if error is not None else "transport")

    async def _on_message(self, message: object) -> None:
        if isinstance(message, Exception):
            self._mark_disconnected(type(message).__name__)
            return
        if getattr(message, "method", None) == "notifications/tools/list_changed":
            log_event("mcp.tools.list", provider="github", notification="list_changed")

    def _mark_disconnected(self, error_category: str) -> None:
        self._connected = False
        if not self._disconnect_logged:
            log_event(
                "mcp.mount.disconnect",
                provider="github",
                error_category=error_category,
            )
            self._disconnect_logged = True

    async def _close_stack(self) -> None:
        stack = self._stack
        self._stack = None
        self._client = None
        self._session = None
        if stack is not None:
            try:
                await stack.aclose()
            except Exception:
                log_event(
                    "mcp.mount.disconnect",
                    provider="github",
                    error_category="close_error",
                )

    async def close(self) -> None:
        if not self.config.enabled or self._closed:
            return
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            self._mark_disconnected("shutdown")
            await self._close_stack()
