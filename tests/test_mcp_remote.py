from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, ClassVar

import pytest
from mcp import types

from codex_bridge.config import GitHubMcpConfig
from codex_bridge.mcp_remote import RemoteMcpError, RemoteMcpProvider


@dataclass
class FakeHttpClient:
    headers: dict[str, str]
    closed: bool = False
    instances: ClassVar[list[FakeHttpClient]] = []

    def __post_init__(self) -> None:
        self.__class__.instances.append(self)

    async def __aenter__(self) -> FakeHttpClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.closed = True


class FakeSession:
    instances: list[FakeSession] = []
    pages: list[types.ListToolsResult] = []
    call_error: Exception | None = None
    initialize_error: Exception | None = None
    list_error: Exception | None = None

    def __init__(self, _read: object, _write: object, **kwargs: object) -> None:
        self.initialized = False
        self.closed = False
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.message_handler = kwargs["message_handler"]
        self.__class__.instances.append(self)

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.closed = True

    async def initialize(self) -> types.InitializeResult:
        if self.__class__.initialize_error is not None:
            raise self.__class__.initialize_error
        self.initialized = True
        return types.InitializeResult(
            protocolVersion="2025-11-25",
            capabilities=types.ServerCapabilities(),
            serverInfo=types.Implementation(name="fake", version="1"),
        )

    async def list_tools(self, *, params=None) -> types.ListToolsResult:
        if self.__class__.list_error is not None:
            raise self.__class__.list_error
        index = 0 if params is None or params.cursor is None else 1
        return self.__class__.pages[index]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        self.calls.append((name, arguments))
        if self.__class__.call_error is not None:
            raise self.__class__.call_error
        return types.CallToolResult(content=[types.TextContent(type="text", text="ok")])


@asynccontextmanager
async def fake_transport(_url: str, **_kwargs: object):
    yield object(), object()


def settings(**overrides: object) -> GitHubMcpConfig:
    values: dict[str, object] = {"enabled": True, "pat": "secret-pat"}
    values.update(overrides)
    return GitHubMcpConfig(**values)


def setup_function() -> None:
    FakeHttpClient.instances = []
    FakeSession.instances = []
    FakeSession.pages = [
        types.ListToolsResult(
            tools=[types.Tool(name="a", inputSchema={"type": "object"})],
            nextCursor="page-2",
        ),
        types.ListToolsResult(
            tools=[types.Tool(name="b", inputSchema={"type": "object"})],
        ),
    ]
    FakeSession.call_error = None
    FakeSession.initialize_error = None
    FakeSession.list_error = None


@pytest.mark.asyncio
async def test_start_initializes_and_fetches_all_tool_pages() -> None:
    provider = RemoteMcpProvider(
        settings(),
        http_client_factory=lambda **kwargs: FakeHttpClient(kwargs["headers"]),
        transport_factory=fake_transport,
        session_factory=FakeSession,
    )

    await provider.start()

    assert len(FakeSession.instances) == 1
    assert FakeSession.instances[0].initialized is True
    assert FakeHttpClient.instances[0].headers == {"Authorization": "Bearer secret-pat"}
    assert [item.name for item in provider.upstream_tools] == ["a", "b"]

    await provider.close()
    assert FakeSession.instances[0].closed is True


@pytest.mark.asyncio
async def test_disabled_provider_does_not_connect() -> None:
    calls = 0

    def client_factory(**_kwargs):
        nonlocal calls
        calls += 1
        return FakeHttpClient({})

    provider = RemoteMcpProvider(
        settings(enabled=False),
        http_client_factory=client_factory,
        transport_factory=fake_transport,
        session_factory=FakeSession,
    )

    await provider.start()

    assert calls == 0
    assert provider.upstream_tools == ()


@pytest.mark.asyncio
async def test_initialize_failure_is_fail_fast_and_does_not_echo_pat() -> None:
    FakeSession.initialize_error = RuntimeError("upstream rejected authentication")
    provider = RemoteMcpProvider(
        settings(),
        http_client_factory=lambda **kwargs: FakeHttpClient(kwargs["headers"]),
        transport_factory=fake_transport,
        session_factory=FakeSession,
    )

    with pytest.raises(RemoteMcpError, match="startup failed") as exc_info:
        await provider.start()

    assert "secret-pat" not in str(exc_info.value)
    assert provider.connected is False


@pytest.mark.asyncio
async def test_tools_list_failure_is_fail_fast() -> None:
    FakeSession.list_error = RuntimeError("listing failed")
    provider = RemoteMcpProvider(
        settings(),
        http_client_factory=lambda **kwargs: FakeHttpClient(kwargs["headers"]),
        transport_factory=fake_transport,
        session_factory=FakeSession,
    )

    with pytest.raises(RemoteMcpError, match="startup failed"):
        await provider.start()


@pytest.mark.asyncio
async def test_call_forwards_original_name_and_arguments_without_result_rewrite() -> None:
    provider = RemoteMcpProvider(
        settings(),
        http_client_factory=lambda **kwargs: FakeHttpClient(kwargs["headers"]),
        transport_factory=fake_transport,
        session_factory=FakeSession,
    )
    await provider.start()

    result = await provider.call_tool("get_file_contents", {"path": "README.md"})

    assert result.content[0].text == "ok"
    assert FakeSession.instances[0].calls == [("get_file_contents", {"path": "README.md"})]

    await provider.close()


@pytest.mark.asyncio
async def test_call_error_after_send_is_not_retried() -> None:
    FakeSession.call_error = TimeoutError("network timeout")
    provider = RemoteMcpProvider(
        settings(),
        http_client_factory=lambda **kwargs: FakeHttpClient(kwargs["headers"]),
        transport_factory=fake_transport,
        session_factory=FakeSession,
    )
    await provider.start()

    with pytest.raises(RemoteMcpError, match="outcome unknown"):
        await provider.call_tool("write_tool", {})

    assert len(FakeSession.instances) == 1
    assert len(FakeSession.instances[0].calls) == 1
    await provider.close()


@pytest.mark.asyncio
async def test_disconnect_keeps_provider_reconnectable_before_next_call() -> None:
    provider = RemoteMcpProvider(
        settings(),
        http_client_factory=lambda **kwargs: FakeHttpClient(kwargs["headers"]),
        transport_factory=fake_transport,
        session_factory=FakeSession,
    )
    await provider.start()
    await provider.handle_transport_failure(ConnectionError("closed"))

    await provider.call_tool("read_tool", {})

    assert len(FakeSession.instances) == 2
    assert FakeSession.instances[1].calls == [("read_tool", {})]
    await provider.close()
