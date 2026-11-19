from __future__ import annotations

import asyncio
import json
import socket
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

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

FULL_TOKEN = "cpp_phase2_full_" + "f" * 32
CPP_TOKEN = "cpp_phase2_token_" + "c" * 32
ORIGIN = "chrome-extension://" + "a" * 32
CPP_TOOLS = {
    "codex_targets",
    "codex_threads",
    "codex_status",
    "codex_wait",
}


def test_cpp_token_is_environment_only_validated_and_distinct() -> None:
    settings = BridgeConfig.from_sources(
        environ={"CODEX_BRIDGE_CPP_MCP_TOKEN": CPP_TOKEN}, config_data={}
    )
    assert settings.cpp_mcp_token == CPP_TOKEN
    assert CPP_TOKEN not in repr(settings)

    for token in ("too_short", "c" * 32 + "\n", "あ" * 32):
        with pytest.raises(ConfigurationError) as error:
            BridgeConfig.from_sources(environ={"CODEX_BRIDGE_CPP_MCP_TOKEN": token}, config_data={})
        assert token not in str(error.value)

    with pytest.raises(ConfigurationError) as error:
        BridgeConfig.from_sources(
            environ={
                "CODEX_BRIDGE_MCP_TOKEN": FULL_TOKEN,
                "CODEX_BRIDGE_CPP_MCP_TOKEN": FULL_TOKEN,
            },
            config_data={},
        )
    assert FULL_TOKEN not in str(error.value)


@asynccontextmanager
async def _serving(app):
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


def _rpc_payload(response: httpx.Response) -> dict[str, Any]:
    if "application/json" in response.headers.get("content-type", ""):
        return response.json()
    for line in response.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line.removeprefix("data:").strip())
    raise AssertionError(f"MCP response contained no JSON-RPC event: {response.text!r}")


async def _post_rpc(
    url: str,
    *,
    session_id: str,
    protocol_version: str,
    token: str,
    request_id: int,
    method: str,
    params: dict[str, Any],
) -> httpx.Response:
    async with httpx.AsyncClient() as client:
        return await client.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Mcp-Session-Id": session_id,
                "Mcp-Protocol-Version": protocol_version,
                "Origin": ORIGIN,
                "Accept": "application/json, text/event-stream",
                "X-CodexBridge-Mcp-Principal": "full",
            },
            json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
        )


def _phase2_config(tmp_path: Path, inbox: Path) -> BridgeConfig:
    return replace(
        config(tmp_path),
        lan_enabled=True,
        mcp_token=FULL_TOKEN,
        cpp_mcp_token=CPP_TOKEN,
        file_put_dir=str(inbox),
        allowed_hosts=("127.0.0.1:*",),
        allowed_origins=(ORIGIN,),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("json_response", [False, True], ids=["sse", "json"])
async def test_request_identity_survives_shared_session_and_concurrent_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, json_response: bool
) -> None:
    from mcp.server.lowlevel.server import Server as LowLevelServer

    if json_response:
        original = LowLevelServer.streamable_http_app

        def json_transport(self, **kwargs):
            kwargs["json_response"] = True
            return original(self, **kwargs)

        monkeypatch.setattr(LowLevelServer, "streamable_http_app", json_transport)

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    bridge, app_server, _state = make_bridge(tmp_path)
    app_server.thread_cwds["desktop"] = str(tmp_path)
    app_server.thread_list = [{"id": "desktop", "cwd": str(tmp_path), "name": "Desktop"}]
    runtime = FakeRuntime()
    runtime.bridge = bridge
    log_path = tmp_path / "observability.jsonl"
    app = create_app(
        _phase2_config(tmp_path, inbox),
        runtime_factory=lambda _: runtime,
        observability=ObservabilityLogger(log_path),
    )

    async def remote_peer(scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "client": ("192.168.1.20", 1234)}
        await app(scope, receive, send)

    session_ids: list[str] = []

    async def capture(response: httpx.Response) -> None:
        value = response.headers.get("mcp-session-id")
        if value:
            session_ids.append(value)

    async with _serving(remote_peer) as url:
        async with httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {FULL_TOKEN}",
                "Origin": ORIGIN,
                "Connection": "close",
            },
            event_hooks={"response": [capture]},
        ) as http:
            async with streamable_http_client(url, http_client=http) as (read, write):
                async with ClientSession(read, write) as session:
                    initialized = await session.initialize()
                    full_catalog = await session.list_tools()
                    full_names = {tool.name for tool in full_catalog.tools}
                    assert "file_put" in full_names and "codex_start" in full_names
                    assert session_ids

                    full_write = await _post_rpc(
                        url,
                        session_id=session_ids[0],
                        protocol_version=initialized.protocol_version,
                        token=FULL_TOKEN,
                        request_id=30,
                        method="tools/call",
                        params={
                            "name": "file_put",
                            "arguments": {"filename": "full-token.txt", "content": "isolated"},
                        },
                    )
                    assert "error" not in _rpc_payload(full_write)
                    assert (inbox / "full-token.txt").read_text(encoding="utf-8") == "isolated"

                    # Both principals deliberately reuse the same SDK session.
                    cpp_response, full_response = await asyncio.gather(
                        _post_rpc(
                            url,
                            session_id=session_ids[0],
                            protocol_version=initialized.protocol_version,
                            token=CPP_TOKEN,
                            request_id=31,
                            method="tools/list",
                            params={},
                        ),
                        _post_rpc(
                            url,
                            session_id=session_ids[0],
                            protocol_version=initialized.protocol_version,
                            token=FULL_TOKEN,
                            request_id=32,
                            method="tools/list",
                            params={},
                        ),
                    )
                    assert cpp_response.status_code == full_response.status_code == 200
                    cpp_names = {
                        tool["name"] for tool in _rpc_payload(cpp_response)["result"]["tools"]
                    }
                    replayed_full_names = {
                        tool["name"] for tool in _rpc_payload(full_response)["result"]["tools"]
                    }
                    assert cpp_names == CPP_TOOLS
                    assert "file_put" in replayed_full_names
                    assert replayed_full_names > CPP_TOOLS

                    permitted = await _post_rpc(
                        url,
                        session_id=session_ids[0],
                        protocol_version=initialized.protocol_version,
                        token=CPP_TOKEN,
                        request_id=33,
                        method="tools/call",
                        params={"name": "codex_targets", "arguments": {}},
                    )
                    assert "error" not in _rpc_payload(permitted)

                    permitted_threads = await _post_rpc(
                        url,
                        session_id=session_ids[0],
                        protocol_version=initialized.protocol_version,
                        token=CPP_TOKEN,
                        request_id=37,
                        method="tools/call",
                        params={
                            "name": "codex_threads",
                            "arguments": {"limit": 20, "include_history": False},
                        },
                    )
                    assert "error" not in _rpc_payload(permitted_threads)

                    permitted_status = await _post_rpc(
                        url,
                        session_id=session_ids[0],
                        protocol_version=initialized.protocol_version,
                        token=CPP_TOKEN,
                        request_id=38,
                        method="tools/call",
                        params={"name": "codex_status", "arguments": {"thread_id": "desktop"}},
                    )
                    assert "error" not in _rpc_payload(permitted_status)
                    _state.set_terminal("desktop", "turn-1", "completed")
                    permitted_wait = await _post_rpc(
                        url,
                        session_id=session_ids[0],
                        protocol_version=initialized.protocol_version,
                        token=CPP_TOKEN,
                        request_id=40,
                        method="tools/call",
                        params={
                            "name": "codex_wait",
                            "arguments": {
                                "thread_id": "desktop",
                                "turn_id": "turn-1",
                                "timeout_seconds": 0,
                            },
                        },
                    )
                    assert "error" not in _rpc_payload(permitted_wait)
                    methods_before_denials = list(app_server.methods)

                    denied_file = await _post_rpc(
                        url,
                        session_id=session_ids[0],
                        protocol_version=initialized.protocol_version,
                        token=CPP_TOKEN,
                        request_id=34,
                        method="tools/call",
                        params={
                            "name": "file_put",
                            "arguments": {"filename": "must-not-exist.txt", "content": "secret"},
                        },
                    )
                    assert "error" in _rpc_payload(denied_file)
                    assert CPP_TOKEN not in denied_file.text
                    assert not (inbox / "must-not-exist.txt").exists()

                    denied_calls = (
                        (35, "codex_start", {"cwd": str(tmp_path), "prompt": "must not start"}),
                        (
                            36,
                            "codex_start",
                            {
                                "cwd": str(tmp_path),
                                "prompt": "must not start",
                                "sandbox_mode": "danger-full-access",
                            },
                        ),
                        (
                            39,
                            "codex_continue",
                            {"thread_id": "desktop", "prompt": "must not continue"},
                        ),
                        (
                            41,
                            "github_get_file_contents",
                            {"owner": "example", "repo": "repo", "path": "file.txt"},
                        ),
                        (42, "git_status", {"repo_path": str(tmp_path)}),
                        (43, "workflow_runs_list", {}),
                        (44, "codex_approval", {"request_id": 1, "decision": "accept"}),
                        (45, "codex_interrupt", {"thread_id": "desktop", "turn_id": "turn-1"}),
                        (
                            46,
                            "codex_steer",
                            {"thread_id": "desktop", "turn_id": "turn-1", "prompt": "no"},
                        ),
                    )
                    for request_id, denied_name, arguments in denied_calls:
                        denied = await _post_rpc(
                            url,
                            session_id=session_ids[0],
                            protocol_version=initialized.protocol_version,
                            token=CPP_TOKEN,
                            request_id=request_id,
                            method="tools/call",
                            params={"name": denied_name, "arguments": arguments},
                        )
                        assert "error" in _rpc_payload(denied)
                        assert CPP_TOKEN not in denied.text
                    assert app_server.methods == methods_before_denials

    logs = log_path.read_text(encoding="utf-8")
    assert FULL_TOKEN not in logs
    assert CPP_TOKEN not in logs


@pytest.mark.asyncio
async def test_loopback_legacy_access_and_explicit_cpp_scope(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    app = create_app(_phase2_config(tmp_path, inbox), runtime_factory=lambda _: FakeRuntime())
    async with _serving(app) as url:
        async with httpx.AsyncClient(headers={"Origin": ORIGIN, "Connection": "close"}) as http:
            async with streamable_http_client(url, http_client=http) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    legacy = await session.list_tools()
                    assert "file_put" in {tool.name for tool in legacy.tools}
                    http.headers["Authorization"] = f"Bearer {CPP_TOKEN}"
                    limited = await session.list_tools()
                    assert {tool.name for tool in limited.tools} == CPP_TOOLS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        [("Authorization", "Bearer wrong")],
        [("Authorization", f"Bearer {CPP_TOKEN}"), ("Authorization", f"Bearer {CPP_TOKEN}")],
        [("Authorization", "Basic abc")],
    ],
)
async def test_loopback_explicit_invalid_authorization_is_401(tmp_path: Path, headers) -> None:
    app = create_app(
        replace(config(tmp_path), cpp_mcp_token=CPP_TOKEN), runtime_factory=lambda _: FakeRuntime()
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        response = await client.post("/mcp", headers=headers, json={})
    assert response.status_code == 401
    assert CPP_TOKEN not in response.text


@pytest.mark.asyncio
async def test_cpp_token_keeps_exact_cors_preflight_without_lan_enablement(tmp_path: Path) -> None:
    settings = replace(
        config(tmp_path),
        cpp_mcp_token=CPP_TOKEN,
        allowed_origins=(ORIGIN,),
    )
    app = create_app(settings, runtime_factory=lambda _: FakeRuntime())
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        allowed = await client.options(
            "/mcp",
            headers={
                "Origin": ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type,mcp-session-id",
            },
        )
        rejected = await client.options(
            "/mcp",
            headers={
                "Origin": "https://evil.test",
                "Access-Control-Request-Method": "POST",
            },
        )
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == ORIGIN
    assert rejected.status_code == 400
    assert "access-control-allow-origin" not in rejected.headers
