from __future__ import annotations

import asyncio
import socket
from contextlib import asynccontextmanager
from dataclasses import replace

import httpx2 as httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from codex_bridge.config import BridgeConfig, ConfigurationError
from codex_bridge.observability import ObservabilityLogger
from codex_bridge.server import create_app
from tests.bridge_test_fakes import make_bridge
from tests.test_server import FakeRuntime, config

TOKEN = "test_lan_token_" + "a" * 32
ORIGIN = "chrome-extension://" + "a" * 32


def test_lan_is_explicit_and_token_is_environment_only(tmp_path):
    settings = BridgeConfig.from_sources(
        environ={"CODEX_BRIDGE_MCP_TOKEN": TOKEN},
        config_data={"bridge": {"lan_enabled": True}},
    )
    assert settings.lan_enabled is True
    assert settings.mcp_token == TOKEN
    assert TOKEN not in repr(settings)
    with pytest.raises(ConfigurationError, match="MCP_TOKEN"):
        BridgeConfig.from_sources(environ={}, config_data={"bridge": {"lan_enabled": True}})
    with pytest.raises(ConfigurationError, match="LAN"):
        create_app(
            replace(config(tmp_path), host="0.0.0.0", allowed_hosts=("bridge.test:*",)),
            runtime_factory=lambda _: FakeRuntime(),
        )


def test_lan_settings_load_from_toml_and_plaintext_token_is_rejected(tmp_path):
    path = tmp_path / "lan.toml"
    path.write_text('[bridge]\nhost = "0.0.0.0"\nlan_enabled = true\n', encoding="utf-8")
    settings = BridgeConfig.from_sources(
        environ={"CODEX_BRIDGE_CONFIG": str(path), "CODEX_BRIDGE_MCP_TOKEN": TOKEN}
    )
    assert settings.lan_enabled is True and settings.host == "0.0.0.0"
    path.write_text('[bridge]\nmcp_token = "' + TOKEN + '"\n', encoding="utf-8")
    with pytest.raises(ConfigurationError, match="must not be stored") as error:
        BridgeConfig.from_sources(environ={"CODEX_BRIDGE_CONFIG": str(path)})
    assert TOKEN not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "Basic abc", "Bearer café"])
async def test_lan_rejects_credentials_even_with_forged_forwarded_loopback(tmp_path, authorization):
    settings = replace(config(tmp_path), lan_enabled=True, mcp_token=TOKEN)
    app = create_app(settings, runtime_factory=lambda _: FakeRuntime())
    headers = {"x-forwarded-for": "127.0.0.1"}
    if authorization:
        headers["authorization"] = authorization
    transport = httpx.ASGITransport(app=app, client=("192.168.1.20", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        # httpx requires ASCII request headers; malicious wire bytes are still rejected.
        if authorization == "Bearer café":
            headers["authorization"] = authorization.encode("utf-8")
        response = await client.post("/mcp", headers=headers, json={})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert TOKEN not in response.text
    assert "wrong" not in response.text


@pytest.mark.asyncio
async def test_lan_cors_is_exact_and_exposes_session_header(tmp_path):
    settings = replace(
        config(tmp_path), lan_enabled=True, mcp_token=TOKEN, allowed_origins=(ORIGIN,)
    )
    app = create_app(settings, runtime_factory=lambda _: FakeRuntime())
    transport = httpx.ASGITransport(app=app, client=("192.168.1.20", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        response = await client.options(
            "/mcp",
            headers={
                "origin": ORIGIN,
                "access-control-request-method": "POST",
                "access-control-request-headers": (
                    "authorization,mcp-session-id,mcp-protocol-version"
                ),
            },
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == ORIGIN
        rejected = await client.options(
            "/mcp",
            headers={"origin": "https://evil.test", "access-control-request-method": "POST"},
        )
        assert rejected.status_code == 400
        assert "access-control-allow-origin" not in rejected.headers


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_localhost_and_local_tunnel_remain_unauthenticated(tmp_path, enabled):
    settings = replace(config(tmp_path), lan_enabled=enabled, mcp_token=TOKEN if enabled else None)
    app = create_app(settings, runtime_factory=lambda _: FakeRuntime())
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            response = await client.post(
                "/mcp", headers={"x-forwarded-for": "203.0.113.5"}, json={}
            )
    assert response.status_code != 401


@asynccontextmanager
async def serving(app):
    # sse-starlette retains its process-wide shutdown flag between isolated servers.
    from sse_starlette.sse import AppStatus

    AppStatus.should_exit = False
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    server = uvicorn.Server(
        uvicorn.Config(
            app, log_level="critical", access_log=False, proxy_headers=False, lifespan="on"
        )
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{listener.getsockname()[1]}/mcp"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        listener.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("remote_peer", [False, True])
async def test_real_mcp_sessions_read_history_and_reinitialize_without_turn_replay(
    tmp_path, remote_peer
):
    bridge, app_server, state = make_bridge(tmp_path)
    app_server.thread_cwds["desktop"] = str(tmp_path)
    app_server.thread_list = [{"id": "desktop", "cwd": str(tmp_path), "name": "Desktop"}]
    app_server.thread_histories["desktop"] = [
        {
            "id": "last",
            "status": "completed",
            "items": [{"type": "agentMessage", "text": "final answer"}],
        }
    ]
    runtime = FakeRuntime()
    runtime.bridge = bridge
    settings = replace(
        config(tmp_path),
        lan_enabled=True,
        mcp_token=TOKEN,
        allowed_hosts=("127.0.0.1:*",),
        allowed_origins=(ORIGIN,),
    )
    log_path = tmp_path / "observability.jsonl"
    app = create_app(
        settings,
        runtime_factory=lambda _: runtime,
        observability=ObservabilityLogger(log_path),
    )

    async def endpoint(scope, receive, send):
        # Real local socket/protocol; model a LAN socket peer without opening a LAN listener.
        if remote_peer and scope["type"] == "http":
            scope = {**scope, "client": ("192.168.1.20", 1234)}
        await app(scope, receive, send)

    headers = {"Origin": ORIGIN, "Connection": "close"}
    if remote_peer:
        headers["Authorization"] = f"Bearer {TOKEN}"
    else:
        headers["X-Forwarded-For"] = "203.0.113.5"  # Existing local Tunnel forwarding.
    session_ids = []

    async def capture(response):
        if response.request.method == "POST" and response.headers.get("mcp-session-id"):
            session_ids.append(response.headers["mcp-session-id"])
            assert "Mcp-Session-Id" in response.headers["access-control-expose-headers"]

    async with serving(endpoint) as url:
        async with httpx.AsyncClient(headers=headers, event_hooks={"response": [capture]}) as http:
            for _ in range(2):
                async with streamable_http_client(url, http_client=http) as (read, write):
                    async with ClientSession(read, write) as session:
                        initialized = await session.initialize()
                        assert initialized.server_info.name == "CodexBridge"
                        listed = await session.list_tools()
                        assert {
                            "codex_threads",
                            "codex_status",
                            "codex_start",
                            "codex_continue",
                        }.issubset({tool.name for tool in listed.tools})
                        result = await session.call_tool("codex_threads", {})
                        assert not result.is_error
                        assert result.structured_content["threads"][0]["id"] == "desktop"
                        status = await session.call_tool("codex_status", {"thread_id": "desktop"})
                        assert status.structured_content["state"] == "not_loaded"
                        assert status.structured_content["last_turn"]["status"] == "completed"
                        assert status.structured_content["current_execution"]["state"] == "unknown"
                        probe = await session.call_tool("mcp_request_headers_probe", {})
                        assert probe.structured_content["protocol_version_matches_context"] is True
                        history = await session.call_tool(
                            "codex_threads", {"thread_id": "desktop", "include_history": True}
                        )
                        assert "final answer" in str(history.structured_content)
                        denied = await session.call_tool(
                            "codex_threads", {"thread_id": "outside", "include_history": True}
                        )
                        assert denied.is_error
                        # A new MCP session never sends a Codex turn implicitly.
                        assert not {"turn/start", "turn/steer", "thread/resume"} & set(
                            app_server.methods
                        )
            expired = await http.post(
                url,
                headers={
                    "Mcp-Session-Id": session_ids[-1],
                    "Mcp-Protocol-Version": initialized.protocol_version,
                },
                json={"jsonrpc": "2.0", "id": 99, "method": "tools/list", "params": {}},
            )
            assert expired.status_code == 404
        async with httpx.AsyncClient() as http:
            if remote_peer:
                response = await http.post(url, json={})
                assert response.status_code == 401
    assert not state.is_loaded("desktop")
    assert len(set(session_ids)) == 2
    assert TOKEN not in log_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_default_loopback_real_mcp_initialize_list_and_call(tmp_path):
    runtime = FakeRuntime()
    app = create_app(config(tmp_path), runtime_factory=lambda _: runtime)
    async with serving(app) as url:
        async with httpx.AsyncClient(headers={"Connection": "close"}) as http:
            async with streamable_http_client(url, http_client=http) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    assert (await session.list_tools()).tools
                    probe = await session.call_tool("mcp_request_headers_probe", {})
                    assert probe.structured_content["codex_invoked"] is False
    assert runtime.bridge.start_count == 0
    assert runtime.start_count == runtime.shutdown_count == 1


@pytest.mark.asyncio
async def test_run_server_does_not_trust_forwarded_headers(tmp_path, monkeypatch):
    from codex_bridge import server as module

    recorded = []

    class Server:
        def __init__(self, settings):
            recorded.append(settings)

        async def serve(self):
            return None

    monkeypatch.setattr(module, "prepare_config", lambda value: value)
    monkeypatch.setattr(module, "create_app", lambda *args, **kwargs: object())
    monkeypatch.setattr(uvicorn, "Server", Server)
    await module.run_server(config(tmp_path))
    assert recorded[0].proxy_headers is False


@pytest.mark.asyncio
async def test_duplicate_authorization_is_rejected_without_disclosing_token(tmp_path):
    settings = replace(config(tmp_path), lan_enabled=True, mcp_token=TOKEN)
    app = create_app(settings, runtime_factory=lambda _: FakeRuntime())
    transport = httpx.ASGITransport(app=app, client=("192.168.1.20", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        response = await client.post(
            "/mcp",
            headers=[("Authorization", f"Bearer {TOKEN}"), ("Authorization", f"Bearer {TOKEN}")],
            json={},
        )
    assert response.status_code == 401
    assert TOKEN not in response.text


@pytest.mark.parametrize("token", ["too_short", "a" * 32 + "\n", "あ" * 32])
def test_invalid_lan_token_configuration_never_echoes_credential(token):
    with pytest.raises(ConfigurationError) as error:
        BridgeConfig.from_sources(
            environ={"CODEX_BRIDGE_MCP_TOKEN": token},
            config_data={"bridge": {"lan_enabled": True}},
        )
    assert token not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers,expected",
    [
        ({"host": "evil.test"}, 421),
        ({"origin": "https://evil.test"}, 403),
    ],
)
async def test_authenticated_lan_keeps_transport_host_and_origin_checks(
    tmp_path, headers, expected
):
    settings = replace(
        config(tmp_path),
        lan_enabled=True,
        mcp_token=TOKEN,
        allowed_hosts=("localhost",),
        allowed_origins=(ORIGIN,),
    )
    app = create_app(settings, runtime_factory=lambda _: FakeRuntime())
    transport = httpx.ASGITransport(app=app, client=("192.168.1.20", 1234))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            response = await client.post(
                "/mcp",
                headers={
                    "authorization": f"Bearer {TOKEN}",
                    "accept": "application/json, text/event-stream",
                    **headers,
                },
                json={},
            )
    assert response.status_code == expected


@pytest.mark.asyncio
async def test_authenticated_mcp_explicit_start_and_continue_use_existing_paths(tmp_path):
    bridge, app_server, _ = make_bridge(tmp_path)
    app_server.thread_cwds["desktop"] = str(tmp_path)
    runtime = FakeRuntime()
    runtime.bridge = bridge
    settings = replace(config(tmp_path), lan_enabled=True, mcp_token=TOKEN)
    app = create_app(settings, runtime_factory=lambda _: runtime)

    async def endpoint(scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "client": ("192.168.1.20", 1234)}
        await app(scope, receive, send)

    async with serving(endpoint) as url:
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {TOKEN}", "Connection": "close"}
        ) as http:
            async with streamable_http_client(url, http_client=http) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    denied = await session.call_tool(
                        "codex_start",
                        {"cwd": str(tmp_path.parent), "prompt": "explicit test outside root"},
                    )
                    assert denied.is_error
                    assert "turn/start" not in app_server.methods
                    started = await session.call_tool(
                        "codex_start", {"cwd": str(tmp_path), "prompt": "explicit fake start"}
                    )
                    assert not started.is_error
                    continued = await session.call_tool(
                        "codex_continue",
                        {"thread_id": "desktop", "prompt": "explicit fake continuation"},
                    )
                    assert not continued.is_error
    assert app_server.methods.count("turn/start") == 2
    assert app_server.methods.count("thread/resume") == 1
