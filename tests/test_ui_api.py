from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from starlette.requests import Request

from codex_bridge.activity import ActivityStore
from codex_bridge.config import BridgeConfig
from codex_bridge.history import HistoryValidationError
from codex_bridge.jsonrpc import JsonRpcClosedError, JsonRpcRemoteError
from codex_bridge.paths import PathPolicyError
from codex_bridge.ui_api import create_ui_app


class FakeBridge:
    def __init__(self) -> None:
        self.app_server_ready = True
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def threads(
        self,
        thread_id: str | None = None,
        *,
        include_history: bool = False,
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "threads",
                (thread_id,),
                {"include_history": include_history, "limit": limit, "cursor": cursor},
            )
        )
        if thread_id == "outside":
            raise PathPolicyError("outside allowed roots")
        if thread_id is not None:
            return {
                "thread": {
                    "id": thread_id,
                    "cwd": "C:/allowed",
                    "name": "safe name",
                    "preview": "safe preview",
                    "turns": [],
                    "raw": "must not expose",
                }
            }
        return {
            "threads": [
                {
                    "id": "thread-1",
                    "cwd": "C:/allowed",
                    "name": "safe name",
                    "preview": "safe preview",
                    "raw": "must not expose",
                }
            ],
            "next_cursor": "next",
            "backwards_cursor": "back",
        }

    async def read_thread_turns(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("turns", args, kwargs))
        return {
            "thread_id": args[0],
            "history_mode": "paginated",
            "turns": [],
            "next_cursor": None,
            "backwards_cursor": None,
            "truncated": False,
        }

    async def read_thread_items(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("items", args, kwargs))
        return {
            "thread_id": args[0],
            "turn_id": kwargs.get("turn_id"),
            "history_mode": "paginated",
            "items": [],
            "next_cursor": None,
            "backwards_cursor": None,
            "truncated": False,
        }

    async def status(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("status", args, kwargs))
        return {
            "thread_id": args[0],
            "turn_id": kwargs.get("turn_id"),
            "state": "completed",
            "recent_activities": [],
        }


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


def _request(
    path: str,
    query: str = "",
    host: str = "127.0.0.1",
    path_params: dict[str, str] | None = None,
    *,
    method: str = "GET",
    authorization: str | None = None,
) -> Request:
    headers = [(b"host", host.encode())]
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": query.encode(),
            "headers": headers,
            "path_params": path_params or {},
        }
    )


def _route(app, path: str):
    return next(route for route in app.routes if getattr(route, "path", None) == path)


async def _json_response(response) -> dict[str, Any]:
    return json.loads(response.body)


@pytest.mark.asyncio
async def test_ui_api_get_endpoints_delegate_to_read_only_bridge(tmp_path) -> None:
    bridge = FakeBridge()
    app = create_ui_app(bridge, ActivityStore(), config(tmp_path))

    health = await _route(app, "/healthz").endpoint(_request("/healthz"))
    status = await _route(app, "/ui-api/status").endpoint(_request("/ui-api/status"))
    threads = await _route(app, "/ui-api/threads").endpoint(
        _request("/ui-api/threads", "limit=3&cursor=next")
    )
    detail = await _route(app, "/ui-api/threads/{thread_id}").endpoint(
        _request("/ui-api/threads/thread-1", path_params={"thread_id": "thread-1"})
    )
    turns = await _route(app, "/ui-api/threads/{thread_id}/turns").endpoint(
        _request(
            "/ui-api/threads/thread-1/turns",
            "limit=4&cursor=c&sort_direction=asc",
            path_params={"thread_id": "thread-1"},
        )
    )
    items = await _route(app, "/ui-api/threads/{thread_id}/items").endpoint(
        _request(
            "/ui-api/threads/thread-1/items",
            "turn_id=turn-1&limit=5",
            path_params={"thread_id": "thread-1"},
        )
    )
    thread_status = await _route(app, "/ui-api/threads/{thread_id}/status").endpoint(
        _request(
            "/ui-api/threads/thread-1/status",
            "turn_id=turn-1&activity_limit=2",
            path_params={"thread_id": "thread-1"},
        )
    )

    assert await _json_response(health) == {"status": "ok"}
    assert (await _json_response(status))["ui_host"] == "127.0.0.1"
    assert (await _json_response(status))["ui_port"] == 8001
    assert (await _json_response(threads))["threads"][0] == {
        "id": "thread-1",
        "cwd": "C:/allowed",
        "name": "safe name",
        "preview": "safe preview",
    }
    assert "must not expose" not in str(await _json_response(detail))
    assert (await _json_response(turns))["history_mode"] == "paginated"
    assert (await _json_response(items))["thread_id"] == "thread-1"
    assert (await _json_response(thread_status))["state"] == "completed"
    assert [call[0] for call in bridge.calls] == [
        "threads",
        "threads",
        "turns",
        "items",
        "status",
    ]


@pytest.mark.asyncio
async def test_ui_api_rejects_invalid_queries_and_hides_disallowed_threads(tmp_path) -> None:
    bridge = FakeBridge()
    app = create_ui_app(bridge, ActivityStore(), config(tmp_path))

    invalid_limit = await _route(app, "/ui-api/threads/{thread_id}/turns").endpoint(
        _request(
            "/ui-api/threads/thread-1/turns",
            "limit=0",
            path_params={"thread_id": "thread-1"},
        )
    )
    invalid_sort = await _route(app, "/ui-api/threads/{thread_id}/items").endpoint(
        _request(
            "/ui-api/threads/thread-1/items",
            "sort_direction=sideways",
            path_params={"thread_id": "thread-1"},
        )
    )
    invalid_cursor = await _route(app, "/ui-api/threads").endpoint(
        _request("/ui-api/threads", "cursor=" + ("x" * 4097))
    )
    outside = await _route(app, "/ui-api/threads/{thread_id}").endpoint(
        _request("/ui-api/threads/outside", path_params={"thread_id": "outside"})
    )

    assert invalid_limit.status_code == 400
    assert invalid_sort.status_code == 400
    assert invalid_cursor.status_code == 400
    assert outside.status_code == 404
    assert "outside allowed roots" not in outside.body.decode()


@pytest.mark.asyncio
async def test_ui_api_maps_history_validation_and_upstream_failure_safely(tmp_path) -> None:
    bridge = FakeBridge()

    async def invalid(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise HistoryValidationError("bad history query")

    bridge.read_thread_turns = invalid  # type: ignore[method-assign]
    app = create_ui_app(bridge, ActivityStore(), config(tmp_path))
    response = await _route(app, "/ui-api/threads/{thread_id}/turns").endpoint(
        _request("/ui-api/threads/thread-1/turns", path_params={"thread_id": "thread-1"})
    )

    assert response.status_code == 400
    assert response.body == b'{"error":"invalid request"}'


@pytest.mark.asyncio
async def test_ui_api_maps_unavailable_and_unexpected_app_server_failures(tmp_path) -> None:
    bridge = FakeBridge()

    async def unavailable(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise JsonRpcClosedError("raw connection failure")

    bridge.read_thread_turns = unavailable  # type: ignore[method-assign]
    app = create_ui_app(bridge, ActivityStore(), config(tmp_path))
    unavailable_response = await _route(app, "/ui-api/threads/{thread_id}/turns").endpoint(
        _request("/ui-api/threads/thread-1/turns", path_params={"thread_id": "thread-1"})
    )

    async def unexpected(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise JsonRpcRemoteError("raw upstream failure")

    bridge.read_thread_turns = unexpected  # type: ignore[method-assign]
    unexpected_response = await _route(app, "/ui-api/threads/{thread_id}/turns").endpoint(
        _request("/ui-api/threads/thread-1/turns", path_params={"thread_id": "thread-1"})
    )

    assert unavailable_response.status_code == 503
    assert unavailable_response.body == b'{"error":"App Server unavailable"}'
    assert unexpected_response.status_code == 502
    assert b"raw upstream failure" not in unexpected_response.body


@pytest.mark.asyncio
async def test_sse_streams_only_safe_activity_and_closes_subscription(tmp_path) -> None:
    bridge = FakeBridge()
    activities = ActivityStore()
    app = create_ui_app(bridge, activities, config(tmp_path))
    response = await _route(app, "/ui-api/events").endpoint(_request("/ui-api/events"))

    next_chunk = asyncio.create_task(response.body_iterator.__anext__())
    await asyncio.sleep(0)
    activities.add(
        thread_id="thread-1",
        turn_id="turn-1",
        type="error",
        status="failed",
        summary="safe summary",
        details={"password": "secret", "exit_code": 1},
    )
    chunk = await asyncio.wait_for(next_chunk, timeout=0.1)
    await response.body_iterator.aclose()

    assert chunk.startswith(b"event: activity\nid: ")
    assert b"safe summary" in chunk
    assert b"secret" not in chunk
    assert b"password" not in chunk
    assert b"exit_code" in chunk
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"


@pytest.mark.asyncio
async def test_sse_thread_filter_only_delivers_selected_thread(tmp_path) -> None:
    bridge = FakeBridge()
    activities = ActivityStore()
    app = create_ui_app(bridge, activities, config(tmp_path))
    response = await _route(app, "/ui-api/events").endpoint(
        _request("/ui-api/events", "thread_id=thread-1")
    )

    next_chunk = asyncio.create_task(response.body_iterator.__anext__())
    await asyncio.sleep(0)
    activities.add(thread_id="thread-2", turn_id="turn", type="error", status="failed")
    activities.add(
        thread_id="thread-1", turn_id="turn", type="error", status="failed", summary="selected"
    )
    chunk = await asyncio.wait_for(next_chunk, timeout=0.1)
    await response.body_iterator.aclose()

    assert b"selected" in chunk
    assert b"thread-1" in chunk
    assert b"thread-2" not in chunk


@pytest.mark.asyncio
async def test_ui_status_reports_failed_app_server_without_diagnostics(tmp_path) -> None:
    bridge = FakeBridge()
    bridge.app_server_ready = False
    app = create_ui_app(bridge, ActivityStore(), config(tmp_path))

    response = await _route(app, "/ui-api/status").endpoint(_request("/ui-api/status"))

    assert await _json_response(response) == {
        "bridge": "ready",
        "app_server": "failed",
        "ui_host": "127.0.0.1",
        "ui_port": 8001,
    }


def test_ui_app_has_only_get_routes_and_no_mcp_route(tmp_path) -> None:
    app = create_ui_app(FakeBridge(), ActivityStore(), config(tmp_path))

    route_paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert "/mcp" not in route_paths
    assert all(
        route.methods <= {"GET", "HEAD"} for route in app.routes if hasattr(route, "methods")
    )
    assert all(middleware.cls.__name__ != "CORSMiddleware" for middleware in app.user_middleware)


@pytest.mark.asyncio
async def test_control_route_is_not_registered_without_token_or_callback(tmp_path) -> None:
    app = create_ui_app(FakeBridge(), ActivityStore(), config(tmp_path))

    assert "/ui-api/control/shutdown" not in {
        route.path for route in app.routes if hasattr(route, "path")
    }

    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "method": "POST",
            "path": "/ui-api/control/shutdown",
            "query_string": b"",
            "headers": [(b"host", b"127.0.0.1")],
        },
        receive,
        send,
    )

    assert sent[0]["status"] == 404


@pytest.mark.asyncio
async def test_control_route_authenticates_and_runs_shutdown_after_response(tmp_path) -> None:
    token = "valid-token_" * 4
    settings = config(tmp_path)
    settings = settings.__class__(
        host=settings.host,
        port=settings.port,
        ui_port=settings.ui_port,
        allowed_roots=settings.allowed_roots,
        allowed_hosts=settings.allowed_hosts,
        allowed_origins=settings.allowed_origins,
        codex_executable=settings.codex_executable,
        wait_default_seconds=settings.wait_default_seconds,
        wait_max_seconds=settings.wait_max_seconds,
        shutdown_grace_seconds=settings.shutdown_grace_seconds,
        control_token=token,
    )
    calls: list[str] = []

    async def shutdown() -> None:
        calls.append("shutdown")

    app = create_ui_app(FakeBridge(), ActivityStore(), settings, shutdown_callback=shutdown)
    route = _route(app, "/ui-api/control/shutdown")

    response = await route.endpoint(
        _request(
            "/ui-api/control/shutdown",
            method="POST",
            authorization=f"Bearer {token}",
        )
    )

    assert response.status_code == 202
    assert json.loads(response.body) == {"status": "shutdown_requested"}
    assert calls == []
    assert response.background is not None
    await response.background()
    assert calls == ["shutdown"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "authorization", [None, "Bearer", "Basic valid-token", "Bearer wrong-token"]
)
async def test_control_route_rejects_missing_malformed_and_wrong_tokens(
    tmp_path, authorization: str | None
) -> None:
    token = "valid-token_" * 4
    settings = config(tmp_path)
    settings = settings.__class__(
        host=settings.host,
        port=settings.port,
        ui_port=settings.ui_port,
        allowed_roots=settings.allowed_roots,
        allowed_hosts=settings.allowed_hosts,
        allowed_origins=settings.allowed_origins,
        codex_executable=settings.codex_executable,
        wait_default_seconds=settings.wait_default_seconds,
        wait_max_seconds=settings.wait_max_seconds,
        shutdown_grace_seconds=settings.shutdown_grace_seconds,
        control_token=token,
    )
    calls: list[str] = []

    def shutdown() -> None:
        calls.append("shutdown")

    app = create_ui_app(FakeBridge(), ActivityStore(), settings, shutdown_callback=shutdown)
    response = await _route(app, "/ui-api/control/shutdown").endpoint(
        _request(
            "/ui-api/control/shutdown",
            method="POST",
            authorization=authorization,
        )
    )

    assert response.status_code == 403
    assert response.body == b'{"error":"forbidden"}'
    assert token not in response.body.decode()
    assert calls == []


@pytest.mark.asyncio
async def test_ui_app_rejects_unexpected_host(tmp_path) -> None:
    app = create_ui_app(FakeBridge(), ActivityStore(), config(tmp_path))
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "method": "GET",
            "path": "/healthz",
            "query_string": b"",
            "headers": [(b"host", b"example.com")],
        },
        receive,
        send,
    )

    assert sent[0]["status"] == 400
