from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, ClassVar

import pytest
from mcp import MCPError, types
from mcp.client.streamable_http import create_mcp_http_client

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
    call_result: types.CallToolResult | types.InputRequiredResult | None = None

    def __init__(self, _read: object, _write: object, **kwargs: object) -> None:
        self.initialized = False
        self.closed = False
        self.calls: list[tuple[str, dict[str, Any] | None, dict[str, Any]]] = []
        self.list_call_count = 0
        self.initialize_count = 0
        self.message_handler = kwargs["message_handler"]
        self.__class__.instances.append(self)

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.closed = True

    async def initialize(self) -> types.InitializeResult:
        self.initialize_count += 1
        if self.__class__.initialize_error is not None:
            raise self.__class__.initialize_error
        self.initialized = True
        return types.InitializeResult(
            protocolVersion="2025-11-25",
            capabilities=types.ServerCapabilities(),
            serverInfo=types.Implementation(name="fake", version="1"),
        )

    async def list_tools(self, *, params=None) -> types.ListToolsResult:
        self.list_call_count += 1
        if self.__class__.list_error is not None:
            raise self.__class__.list_error
        index = 0 if params is None or params.cursor is None else 1
        return self.__class__.pages[index]

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        **kwargs: Any,
    ) -> types.CallToolResult | types.InputRequiredResult:
        self.calls.append((name, arguments, kwargs))
        if self.__class__.call_error is not None:
            raise self.__class__.call_error
        if self.__class__.call_result is not None:
            return self.__class__.call_result
        return types.CallToolResult(content=[types.TextContent(type="text", text="ok")])


class FakeClient:
    instances: list[FakeClient] = []
    protocol_version: ClassVar[str] = "2026-07-28"

    def __init__(self, transport: object, **kwargs: object) -> None:
        self.transport = transport
        self.kwargs = kwargs
        self.session = FakeSession(
            object(), object(), message_handler=kwargs.get("message_handler")
        )
        self.session.protocol_version = self.__class__.protocol_version
        self.__class__.instances.append(self)

    async def __aenter__(self) -> FakeClient:
        await self.session.initialize()
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.session.__aexit__()


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
    FakeClient.instances = []
    FakeClient.protocol_version = "2026-07-28"
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
    FakeSession.call_result = None
    FakeSession.initialize_error = None
    FakeSession.list_error = None


@pytest.mark.asyncio
async def test_start_initializes_and_fetches_all_tool_pages() -> None:
    provider = RemoteMcpProvider(
        settings(),
        http_client_factory=lambda **kwargs: FakeHttpClient(kwargs["headers"]),
        transport_factory=fake_transport,
        client_factory=FakeClient,
    )

    await provider.start()

    assert len(FakeSession.instances) == 1
    assert FakeSession.instances[0].initialized is True
    assert FakeSession.instances[0].initialize_count == 1
    assert FakeClient.instances[0].kwargs["mode"] == "auto"
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
        client_factory=FakeClient,
    )

    await provider.start()

    assert calls == 0
    assert provider.upstream_tools == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol_version", ["2026-07-28", "2025-11-25"])
async def test_client_uses_sdk_auto_negotiation_for_modern_and_legacy(
    protocol_version: str,
) -> None:
    FakeClient.protocol_version = protocol_version
    provider = RemoteMcpProvider(
        settings(),
        http_client_factory=lambda **kwargs: FakeHttpClient(kwargs["headers"]),
        transport_factory=fake_transport,
        client_factory=FakeClient,
    )

    await provider.start()

    assert FakeClient.instances[0].kwargs["mode"] == "auto"
    assert FakeSession.instances[0].initialized is True
    assert provider.upstream_tools
    await provider.close()


@pytest.mark.asyncio
async def test_mcp_http_client_uses_recommended_timeouts_and_auth_header() -> None:
    client = create_mcp_http_client(headers={"Authorization": "Bearer secret-pat"})
    try:
        assert client.timeout.connect == 30.0
        assert client.timeout.write == 30.0
        assert client.timeout.pool == 30.0
        assert client.timeout.read == 300.0
        assert client.headers["Authorization"] == "Bearer secret-pat"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_initialize_failure_is_fail_fast_and_does_not_echo_pat(
    caplog: pytest.LogCaptureFixture,
) -> None:
    FakeSession.initialize_error = RuntimeError("upstream rejected authentication")
    provider = RemoteMcpProvider(
        settings(),
        http_client_factory=lambda **kwargs: FakeHttpClient(kwargs["headers"]),
        transport_factory=fake_transport,
        client_factory=FakeClient,
    )

    with pytest.raises(RemoteMcpError, match="startup failed") as exc_info:
        await provider.start()

    assert "secret-pat" not in str(exc_info.value)
    assert "secret-pat" not in caplog.text
    assert provider.connected is False


@pytest.mark.asyncio
async def test_tools_list_failure_is_fail_fast() -> None:
    FakeSession.list_error = RuntimeError("listing failed")
    provider = RemoteMcpProvider(
        settings(),
        http_client_factory=lambda **kwargs: FakeHttpClient(kwargs["headers"]),
        transport_factory=fake_transport,
        client_factory=FakeClient,
    )

    with pytest.raises(RemoteMcpError, match="startup failed"):
        await provider.start()


@pytest.mark.asyncio
async def test_call_forwards_original_name_and_arguments_without_result_rewrite() -> None:
    provider = RemoteMcpProvider(
        settings(),
        http_client_factory=lambda **kwargs: FakeHttpClient(kwargs["headers"]),
        transport_factory=fake_transport,
        client_factory=FakeClient,
    )
    await provider.start()

    result = await provider.call_tool("get_file_contents", {"path": "README.md"})

    assert result.content[0].text == "ok"
    assert FakeSession.instances[0].calls[0][:2] == (
        "get_file_contents",
        {"path": "README.md"},
    )
    assert FakeSession.instances[0].calls[0][2] == {
        "input_responses": None,
        "request_state": None,
        "meta": None,
        "allow_input_required": True,
    }

    await provider.close()


@pytest.mark.asyncio
async def test_input_required_result_is_forwarded_without_disconnect_or_unknown() -> None:
    input_required = types.InputRequiredResult(
        inputRequests={},
        requestState="upstream-state",
    )
    FakeSession.call_result = input_required
    provider = RemoteMcpProvider(
        settings(),
        http_client_factory=lambda **kwargs: FakeHttpClient(kwargs["headers"]),
        transport_factory=fake_transport,
        client_factory=FakeClient,
    )
    await provider.start()

    result = await provider.call_tool(
        "needs_input",
        {"path": "README.md"},
        input_responses={},
        request_state="prior-state",
        meta={"request": "metadata"},
    )

    assert result is input_required
    assert provider.connected is True
    assert FakeSession.instances[0].calls == [
        (
            "needs_input",
            {"path": "README.md"},
            {
                "input_responses": {},
                "request_state": "prior-state",
                "meta": {"request": "metadata"},
                "allow_input_required": True,
            },
        )
    ]
    await provider.close()


@pytest.mark.asyncio
async def test_call_error_after_send_is_not_retried() -> None:
    FakeSession.call_error = TimeoutError("network timeout")
    provider = RemoteMcpProvider(
        settings(),
        http_client_factory=lambda **kwargs: FakeHttpClient(kwargs["headers"]),
        transport_factory=fake_transport,
        client_factory=FakeClient,
    )
    await provider.start()

    with pytest.raises(RemoteMcpError, match="outcome unknown"):
        await provider.call_tool("write_tool", {})

    assert len(FakeSession.instances) == 1
    assert len(FakeSession.instances[0].calls) == 1
    await provider.close()


@pytest.mark.asyncio
async def test_normal_mcp_error_is_known_failure_without_disconnect_or_unknown() -> None:
    FakeSession.call_error = MCPError(
        code=types.INVALID_PARAMS,
        message="invalid request secret-pat",
        data={"detail": "secret-pat"},
    )
    provider = RemoteMcpProvider(
        settings(),
        http_client_factory=lambda **kwargs: FakeHttpClient(kwargs["headers"]),
        transport_factory=fake_transport,
        client_factory=FakeClient,
    )
    await provider.start()

    with pytest.raises(RemoteMcpError) as exc_info:
        await provider.call_tool("read_tool", {})

    error = exc_info.value
    assert error.error_code == types.INVALID_PARAMS
    assert error.error_data == {"detail": "[redacted]"}
    assert "outcome unknown" not in str(error)
    assert "secret-pat" not in str(error)
    assert provider.connected is True
    await provider.close()


@pytest.mark.asyncio
async def test_request_timeout_is_outcome_unknown_without_retry_or_disconnect() -> None:
    FakeSession.call_error = MCPError(
        code=types.REQUEST_TIMEOUT,
        message="request timed out",
    )
    provider = RemoteMcpProvider(
        settings(),
        http_client_factory=lambda **kwargs: FakeHttpClient(kwargs["headers"]),
        transport_factory=fake_transport,
        client_factory=FakeClient,
    )
    await provider.start()

    with pytest.raises(RemoteMcpError, match="outcome unknown"):
        await provider.call_tool("read_tool", {})

    assert provider.connected is True
    assert len(FakeSession.instances[0].calls) == 1
    await provider.close()


@pytest.mark.asyncio
async def test_connection_closed_is_outcome_unknown_and_disconnects_without_retry() -> None:
    FakeSession.call_error = MCPError(
        code=types.CONNECTION_CLOSED,
        message="connection closed",
    )
    provider = RemoteMcpProvider(
        settings(),
        http_client_factory=lambda **kwargs: FakeHttpClient(kwargs["headers"]),
        transport_factory=fake_transport,
        client_factory=FakeClient,
    )
    await provider.start()

    with pytest.raises(RemoteMcpError, match="outcome unknown"):
        await provider.call_tool("read_tool", {})

    assert provider.connected is False
    assert len(FakeSession.instances[0].calls) == 1
    await provider.close()


@pytest.mark.asyncio
async def test_disconnect_keeps_provider_reconnectable_before_next_call() -> None:
    provider = RemoteMcpProvider(
        settings(),
        http_client_factory=lambda **kwargs: FakeHttpClient(kwargs["headers"]),
        transport_factory=fake_transport,
        client_factory=FakeClient,
    )
    await provider.start()
    await provider.handle_transport_failure(ConnectionError("closed"))

    await provider.call_tool("read_tool", {})

    assert len(FakeSession.instances) == 2
    assert FakeSession.instances[0].list_call_count == 2
    assert FakeSession.instances[1].list_call_count == 0
    assert FakeSession.instances[1].calls[0][:2] == ("read_tool", {})
    await provider.close()
