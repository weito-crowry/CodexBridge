from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any

import pytest
from starlette.requests import Request

from codex_bridge.activity import ActivityStore
from codex_bridge.bridge import ApprovalConflictError
from codex_bridge.config import BridgeConfig
from codex_bridge.history import HistoryValidationError
from codex_bridge.jsonrpc import JsonRpcClosedError, JsonRpcRemoteError
from codex_bridge.paths import PathPolicyError
from codex_bridge.ui_api import create_ui_app


class FakeBridge:
    def __init__(self) -> None:
        self.app_server_ready = True
        self.app_server_os_pid = 220132
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.approval_error: Exception | None = None

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

    async def rename_thread(
        self, thread_id: str, name: str, *, only_if_untitled: bool = False
    ) -> dict[str, Any]:
        kwargs = {"only_if_untitled": True} if only_if_untitled else {}
        self.calls.append(("rename_thread", (thread_id, name), kwargs))
        if only_if_untitled:
            return {"status": "skipped"}
        return {"thread": {"id": thread_id, "name": name}}

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

    async def rate_limits(self) -> dict[str, Any]:
        self.calls.append(("rate_limits", (), {}))
        return {
            "rateLimits": {
                "primary": {"windowDurationMins": 300, "usedPercent": 28},
                "secondary": {"windowDurationMins": 10080, "usedPercent": 39},
            }
        }

    def account_auth_status(self) -> dict[str, object]:
        return {
            "state": "signed_in",
            "account_type": "chatgpt",
            "auth_mode": "chatgpt",
            "plan_type": "plus",
            "login_state": "idle",
            "login_result": "none",
            "login_revision": 0,
            "auth_warning": "none",
            "revision": 1,
            "issue": None,
            "email": "private@example.test",
            "user_id": "private-user-id",
        }

    async def refresh_account_auth(self) -> dict[str, object]:
        self.calls.append(("refresh_account_auth", (), {}))
        return self.account_auth_status()

    async def start_account_login(self, flow_type: str) -> dict[str, object]:
        self.calls.append(("start_account_login", (flow_type,), {}))
        return {
            "type": flow_type,
            "loginId": "private-login-id",
            "login_revision": 1,
            "revision": 2,
            "state": "login_required",
            "login_state": "waiting",
            "login_result": "none",
            "auth_warning": "none",
            "issue": None,
            "authUrl": "https://auth.openai.com/oauth/authorize?state=private-url",
            "verificationUrl": "https://auth.openai.com/codex/device",
            "userCode": "PRIVATE-CODE",
        }

    async def cancel_account_login(
        self, login_id: str | None, login_revision: int
    ) -> dict[str, object]:
        self.calls.append(("cancel_account_login", (login_id, login_revision), {}))
        return {
            **self.account_auth_status(),
            "login_state": "cancelled",
            "login_result": "cancelled",
            "login_revision": login_revision,
            "revision": 3,
            "loginId": "private-login-id",
        }

    async def approve(self, request_id: int | str, decision: str) -> dict[str, Any]:
        self.calls.append(("approve", (request_id, decision), {}))
        if self.approval_error is not None:
            raise self.approval_error
        return {"state": "in_progress"}


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
    origin: str | None = None,
    client_host: str = "127.0.0.1",
    body: bytes = b"",
) -> Request:
    headers = [(b"host", host.encode())]
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    if body:
        headers.append((b"content-type", b"application/json"))
    request = Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": query.encode(),
            "headers": headers,
            "path_params": path_params or {},
            "client": (client_host, 54321),
        }
    )
    request._body = body
    return request


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
    assert (await _json_response(thread_status))["app_server_os_pid"] == 220132
    assert bridge.calls[-1] == (
        "status",
        ("thread-1",),
        {"turn_id": "turn-1", "activity_limit": 2},
    )
    assert [call[0] for call in bridge.calls] == [
        "threads",
        "threads",
        "turns",
        "items",
        "status",
    ]


@pytest.mark.asyncio
async def test_ui_api_rename_delegates_target_and_name_to_bridge(tmp_path) -> None:
    bridge = FakeBridge()
    app = create_ui_app(bridge, ActivityStore(), config(tmp_path))
    response = await _route(app, "/ui-api/threads/{thread_id}/name").endpoint(
        _request(
            "/ui-api/threads/thread-b/name",
            path_params={"thread_id": "thread-b"},
            method="POST",
            body=b'{"name":"Renamed"}',
        )
    )

    assert response.status_code == 200
    assert await _json_response(response) == {
        "thread_id": "thread-b",
        "name": "Renamed",
    }
    assert bridge.calls[-1] == ("rename_thread", ("thread-b", "Renamed"), {})


@pytest.mark.asyncio
async def test_ui_api_conditional_rename_returns_batch_status(tmp_path) -> None:
    bridge = FakeBridge()
    app = create_ui_app(bridge, ActivityStore(), config(tmp_path))
    response = await _route(app, "/ui-api/threads/{thread_id}/name").endpoint(
        _request(
            "/ui-api/threads/thread-b/name",
            path_params={"thread_id": "thread-b"},
            method="POST",
            body=b'{"name":"Renamed","only_if_untitled":true}',
        )
    )

    assert response.status_code == 200
    assert await _json_response(response) == {"thread_id": "thread-b", "status": "skipped"}
    assert bridge.calls[-1] == (
        "rename_thread",
        ("thread-b", "Renamed"),
        {"only_if_untitled": True},
    )


@pytest.mark.asyncio
async def test_ui_api_conditional_rename_requires_a_strict_boolean_flag(tmp_path) -> None:
    bridge = FakeBridge()
    app = create_ui_app(bridge, ActivityStore(), config(tmp_path))
    response = await _route(app, "/ui-api/threads/{thread_id}/name").endpoint(
        _request(
            "/ui-api/threads/thread-b/name",
            path_params={"thread_id": "thread-b"},
            method="POST",
            body=b'{"name":"Renamed","only_if_untitled":1}',
        )
    )

    assert response.status_code == 400
    assert await _json_response(response) == {"error": "invalid request"}
    assert not bridge.calls


@pytest.mark.asyncio
async def test_ui_api_conditional_rename_hides_invalid_title_input(tmp_path) -> None:
    bridge = FakeBridge()
    app = create_ui_app(bridge, ActivityStore(), config(tmp_path))
    response = await _route(app, "/ui-api/threads/{thread_id}/name").endpoint(
        _request(
            "/ui-api/threads/thread-b/name",
            path_params={"thread_id": "thread-b"},
            method="POST",
            body=b'{"name":"secret\\u0001value","only_if_untitled":true}',
        )
    )

    assert response.status_code == 400
    assert await _json_response(response) == {"error": "invalid request"}
    assert "secret" not in response.body.decode("utf-8")
    assert not bridge.calls


@pytest.mark.asyncio
async def test_ui_api_rate_limits_delegates_to_read_only_bridge(tmp_path) -> None:
    bridge = FakeBridge()
    app = create_ui_app(bridge, ActivityStore(), config(tmp_path))

    response = await _route(app, "/ui-api/account/rate-limits").endpoint(
        _request("/ui-api/account/rate-limits")
    )

    assert response.status_code == 200
    assert (await _json_response(response))["rateLimits"]["primary"]["usedPercent"] == 28
    assert bridge.calls[-1] == ("rate_limits", (), {})


@pytest.mark.asyncio
async def test_account_auth_status_projects_only_non_secret_enums(tmp_path) -> None:
    bridge = FakeBridge()
    app = create_ui_app(
        bridge, ActivityStore(), replace(config(tmp_path), control_token="control-secret")
    )

    response = await _route(app, "/ui-api/account/auth").endpoint(_request("/ui-api/account/auth"))

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = await _json_response(response)
    assert body == {
        "state": "signed_in",
        "account_type": "chatgpt",
        "auth_mode": "chatgpt",
        "plan_type": "plus",
        "login_state": "idle",
        "login_result": "none",
        "login_revision": 0,
        "auth_warning": "none",
        "revision": 1,
        "issue": None,
    }
    assert "private" not in response.body.decode("utf-8")


@pytest.mark.asyncio
async def test_account_auth_private_control_requires_token_origin_host_and_loopback(
    tmp_path,
) -> None:
    bridge = FakeBridge()
    app = create_ui_app(
        bridge, ActivityStore(), replace(config(tmp_path), control_token="control-secret")
    )
    route = _route(app, "/ui-api/control/account/auth/refresh")

    missing_token = await route.endpoint(
        _request("/ui-api/control/account/auth/refresh", method="POST")
    )
    wrong_origin = await route.endpoint(
        _request(
            "/ui-api/control/account/auth/refresh",
            method="POST",
            authorization="Bearer control-secret",
            origin="http://evil.example:8001",
        )
    )
    wrong_host = await route.endpoint(
        _request(
            "/ui-api/control/account/auth/refresh",
            method="POST",
            host="evil.example:8001",
            authorization="Bearer control-secret",
        )
    )
    non_loopback = await route.endpoint(
        _request(
            "/ui-api/control/account/auth/refresh",
            method="POST",
            authorization="Bearer control-secret",
            client_host="192.0.2.15",
        )
    )
    cpp_token = await route.endpoint(
        _request(
            "/ui-api/control/account/auth/refresh",
            method="POST",
            authorization="Bearer cpp-token",
        )
    )
    wrong_port = await route.endpoint(
        _request(
            "/ui-api/control/account/auth/refresh",
            method="POST",
            authorization="Bearer control-secret",
            origin="http://127.0.0.1:8002",
        )
    )

    assert [
        missing_token.status_code,
        wrong_origin.status_code,
        wrong_host.status_code,
        non_loopback.status_code,
        cpp_token.status_code,
        wrong_port.status_code,
    ] == [403] * 6
    assert not bridge.calls


@pytest.mark.asyncio
async def test_account_auth_login_start_keeps_secret_response_local_and_uncached(tmp_path) -> None:
    bridge = FakeBridge()
    app = create_ui_app(
        bridge, ActivityStore(), replace(config(tmp_path), control_token="control-secret")
    )
    route = _route(app, "/ui-api/control/account/auth/login/start")
    response = await route.endpoint(
        _request(
            "/ui-api/control/account/auth/login/start",
            method="POST",
            authorization="Bearer control-secret",
            body=b'{"flow_type":"chatgpt"}',
        )
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert await _json_response(response) == {
        "login_id": "private-login-id",
        "flow_type": "chatgpt",
        "state": "login_required",
        "account_type": None,
        "auth_mode": None,
        "plan_type": None,
        "login_state": "waiting",
        "login_result": "none",
        "login_revision": 1,
        "auth_warning": "none",
        "revision": 2,
        "issue": None,
        "auth_url": "https://auth.openai.com/oauth/authorize?state=private-url",
    }
    assert bridge.calls == [("start_account_login", ("chatgpt",), {})]


@pytest.mark.asyncio
async def test_device_code_start_returns_only_private_local_flow_fields(tmp_path) -> None:
    bridge = FakeBridge()
    app = create_ui_app(
        bridge,
        ActivityStore(),
        replace(config(tmp_path), control_token="control-secret"),
    )
    route = _route(app, "/ui-api/control/account/auth/login/start")
    response = await route.endpoint(
        _request(
            "/ui-api/control/account/auth/login/start",
            method="POST",
            authorization="Bearer control-secret",
            body=b'{"flow_type":"chatgptDeviceCode"}',
        )
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert await _json_response(response) == {
        "login_id": "private-login-id",
        "flow_type": "chatgptDeviceCode",
        "state": "login_required",
        "account_type": None,
        "auth_mode": None,
        "plan_type": None,
        "login_state": "waiting",
        "login_result": "none",
        "login_revision": 1,
        "auth_warning": "none",
        "revision": 2,
        "issue": None,
        "verification_url": "https://auth.openai.com/codex/device",
        "user_code": "PRIVATE-CODE",
    }


@pytest.mark.asyncio
async def test_invalid_login_start_url_is_canceled_without_returning_url_or_code(tmp_path) -> None:
    bridge = FakeBridge()

    async def unsafe_login(_flow_type: str) -> dict[str, object]:
        return {
            "type": "chatgptDeviceCode",
            "loginId": "private-login-id",
            "login_revision": 4,
            "state": "login_required",
            "login_state": "waiting",
            "login_result": "none",
            "auth_warning": "none",
            "revision": 10,
            "verificationUrl": "https://auth.openai.com@evil.example/codex/device",
            "userCode": "PRIVATE-CODE",
        }

    bridge.start_account_login = unsafe_login  # type: ignore[method-assign]
    app = create_ui_app(
        bridge, ActivityStore(), replace(config(tmp_path), control_token="control-secret")
    )
    response = await _route(app, "/ui-api/control/account/auth/login/start").endpoint(
        _request(
            "/ui-api/control/account/auth/login/start",
            method="POST",
            authorization="Bearer control-secret",
            body=b'{"flow_type":"chatgptDeviceCode"}',
        )
    )

    body = await _json_response(response)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert body["start_issue"] == "unsupported_login_response"
    assert body["login_id"] == "private-login-id"
    assert body["login_revision"] == 4
    assert "verification_url" not in body
    assert "user_code" not in body
    assert "evil.example" not in response.body.decode("utf-8")
    assert "PRIVATE-CODE" not in response.body.decode("utf-8")
    assert bridge.calls == [("cancel_account_login", ("private-login-id", 4), {})]


@pytest.mark.asyncio
async def test_completed_before_login_start_response_does_not_return_auth_url(tmp_path) -> None:
    bridge = FakeBridge()

    async def completed_login(_flow_type: str) -> dict[str, object]:
        return {
            "type": "chatgpt",
            "loginId": "private-login-id",
            "login_revision": 1,
            "state": "signed_in",
            "login_state": "succeeded",
            "login_result": "succeeded",
            "auth_warning": "none",
            "revision": 4,
        }

    bridge.start_account_login = completed_login  # type: ignore[method-assign]
    app = create_ui_app(
        bridge,
        ActivityStore(),
        replace(config(tmp_path), control_token="control-secret"),
    )
    response = await _route(app, "/ui-api/control/account/auth/login/start").endpoint(
        _request(
            "/ui-api/control/account/auth/login/start",
            method="POST",
            authorization="Bearer control-secret",
            body=b'{"flow_type":"chatgpt"}',
        )
    )

    assert response.status_code == 200
    assert await _json_response(response) == {
        "login_id": "private-login-id",
        "flow_type": "chatgpt",
        "state": "signed_in",
        "account_type": None,
        "auth_mode": None,
        "plan_type": None,
        "login_state": "succeeded",
        "login_result": "succeeded",
        "login_revision": 1,
        "auth_warning": "none",
        "revision": 4,
        "issue": None,
    }


@pytest.mark.asyncio
async def test_account_auth_login_cancel_uses_private_control_and_sanitizes_result(
    tmp_path,
) -> None:
    bridge = FakeBridge()
    app = create_ui_app(
        bridge, ActivityStore(), replace(config(tmp_path), control_token="control-secret")
    )
    route = _route(app, "/ui-api/control/account/auth/login/cancel")
    response = await route.endpoint(
        _request(
            "/ui-api/control/account/auth/login/cancel",
            method="POST",
            authorization="Bearer control-secret",
            body=b'{"login_id":"private-login-id","login_revision":1}',
        )
    )

    assert response.status_code == 200
    assert await _json_response(response) == {
        "state": "signed_in",
        "account_type": "chatgpt",
        "auth_mode": "chatgpt",
        "plan_type": "plus",
        "login_state": "cancelled",
        "login_result": "cancelled",
        "login_revision": 1,
        "auth_warning": "none",
        "revision": 3,
        "issue": None,
    }
    assert "private-login-id" not in response.body.decode("utf-8")
    assert bridge.calls == [("cancel_account_login", ("private-login-id", 1), {})]


@pytest.mark.asyncio
async def test_cancel_control_can_use_nonsecret_revision_when_login_id_is_unknown(tmp_path) -> None:
    bridge = FakeBridge()
    app = create_ui_app(
        bridge, ActivityStore(), replace(config(tmp_path), control_token="control-secret")
    )
    response = await _route(app, "/ui-api/control/account/auth/login/cancel").endpoint(
        _request(
            "/ui-api/control/account/auth/login/cancel",
            method="POST",
            authorization="Bearer control-secret",
            body=b'{"login_revision":1}',
        )
    )

    assert response.status_code == 200
    assert "private-login-id" not in response.body.decode("utf-8")
    assert bridge.calls == [("cancel_account_login", (None, 1), {})]


@pytest.mark.asyncio
async def test_account_auth_http_error_does_not_return_raw_exception(tmp_path) -> None:
    bridge = FakeBridge()

    async def fail_login(_flow_type: str) -> dict[str, str]:
        raise RuntimeError("PRIVATE-URL PRIVATE-CODE PRIVATE-LOGIN-ID")

    bridge.start_account_login = fail_login  # type: ignore[method-assign]
    app = create_ui_app(
        bridge, ActivityStore(), replace(config(tmp_path), control_token="control-secret")
    )
    route = _route(app, "/ui-api/control/account/auth/login/start")
    response = await route.endpoint(
        _request(
            "/ui-api/control/account/auth/login/start",
            method="POST",
            authorization="Bearer control-secret",
            body=b'{"flow_type":"chatgpt"}',
        )
    )

    assert response.status_code == 503
    assert await _json_response(response) == {"error": "account_login_failed"}
    assert "PRIVATE" not in response.body.decode("utf-8")


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
async def test_sse_stream_ends_when_activity_store_closes_subscriptions(tmp_path) -> None:
    activities = ActivityStore()
    app = create_ui_app(FakeBridge(), activities, config(tmp_path))
    response = await _route(app, "/ui-api/events").endpoint(_request("/ui-api/events"))
    next_chunk = asyncio.create_task(response.body_iterator.__anext__())
    await asyncio.sleep(0)

    activities.close_subscriptions()

    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(next_chunk, timeout=0.1)
    assert not activities._subscribers


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
async def test_sse_subscribe_does_not_replay_historical_notification_activities(tmp_path) -> None:
    bridge = FakeBridge()
    activities = ActivityStore()
    activities.add(
        thread_id="thread-1", turn_id="old-turn", type="turn_completed", status="completed"
    )
    activities.add(
        thread_id="thread-1",
        turn_id="old-turn",
        type="approval_requested",
        status="requested",
        details={"request_id": "old-request"},
    )
    app = create_ui_app(bridge, activities, config(tmp_path))
    response = await _route(app, "/ui-api/events").endpoint(_request("/ui-api/events"))
    next_chunk = asyncio.create_task(response.body_iterator.__anext__())
    await asyncio.sleep(0)
    activities.add(
        thread_id="thread-2", turn_id="live-turn", type="turn_completed", status="completed"
    )
    chunk = await asyncio.wait_for(next_chunk, timeout=0.1)
    await response.body_iterator.aclose()

    assert b"live-turn" in chunk
    assert b"old-turn" not in chunk
    assert b"old-request" not in chunk


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


def test_ui_app_has_only_read_routes_plus_thread_rename_and_no_mcp_route(tmp_path) -> None:
    app = create_ui_app(FakeBridge(), ActivityStore(), config(tmp_path))

    route_paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert "/mcp" not in route_paths
    assert all(
        route.methods <= {"GET", "HEAD"}
        or (route.path == "/ui-api/threads/{thread_id}/name" and route.methods == {"POST"})
        for route in app.routes
        if hasattr(route, "methods")
    )
    assert all(middleware.cls.__name__ != "CORSMiddleware" for middleware in app.user_middleware)


@pytest.mark.asyncio
async def test_control_route_is_not_registered_without_token_or_callback(tmp_path) -> None:
    app = create_ui_app(FakeBridge(), ActivityStore(), config(tmp_path))

    assert "/ui-api/control/shutdown" not in {
        route.path for route in app.routes if hasattr(route, "path")
    }
    assert "/ui-api/control/approval" not in {
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
@pytest.mark.parametrize("decision", ["accept", "acceptForSession", "decline", "cancel"])
async def test_approval_control_route_authenticates_and_uses_bridge_approval(
    tmp_path, decision: str
) -> None:
    token = "approval-control-token"
    bridge = FakeBridge()
    app = create_ui_app(
        bridge,
        ActivityStore(),
        replace(config(tmp_path), control_token=token),
    )
    route = _route(app, "/ui-api/control/approval")
    response = await route.endpoint(
        _request(
            "/ui-api/control/approval",
            method="POST",
            authorization=f"Bearer {token}",
            body=json.dumps({"request_id": 123, "decision": decision}).encode(),
        )
    )

    assert response.status_code == 200
    assert await _json_response(response) == {"status": "approval_resolved"}
    assert bridge.calls == [("approve", (123, decision), {})]


@pytest.mark.asyncio
@pytest.mark.parametrize("authorization", [None, "Bearer", "Basic token", "Bearer wrong"])
async def test_approval_control_route_rejects_missing_or_invalid_token(
    tmp_path, authorization: str | None
) -> None:
    bridge = FakeBridge()
    app = create_ui_app(
        bridge,
        ActivityStore(),
        replace(config(tmp_path), control_token="valid-token"),
    )

    response = await _route(app, "/ui-api/control/approval").endpoint(
        _request(
            "/ui-api/control/approval",
            method="POST",
            authorization=authorization,
            body=b'{"request_id":1,"decision":"accept"}',
        )
    )

    assert response.status_code == 403
    assert bridge.calls == []


@pytest.mark.asyncio
async def test_approval_control_route_rejects_unknown_decision_and_malformed_request(
    tmp_path,
) -> None:
    bridge = FakeBridge()
    app = create_ui_app(
        bridge,
        ActivityStore(),
        replace(config(tmp_path), control_token="valid-token"),
    )
    route = _route(app, "/ui-api/control/approval")

    invalid_decision = await route.endpoint(
        _request(
            "/ui-api/control/approval",
            method="POST",
            authorization="Bearer valid-token",
            body=b'{"request_id":1,"decision":"run arbitrary"}',
        )
    )
    malformed_request = await route.endpoint(
        _request(
            "/ui-api/control/approval",
            method="POST",
            authorization="Bearer valid-token",
            body=b'{"request_id":true,"decision":"accept"}',
        )
    )

    assert invalid_decision.status_code == 400
    assert malformed_request.status_code == 400
    assert bridge.calls == []


@pytest.mark.asyncio
async def test_approval_control_route_reports_already_resolved_conflict(tmp_path) -> None:
    bridge = FakeBridge()
    bridge.approval_error = ApprovalConflictError("unknown approval request")
    app = create_ui_app(
        bridge,
        ActivityStore(),
        replace(config(tmp_path), control_token="valid-token"),
    )

    response = await _route(app, "/ui-api/control/approval").endpoint(
        _request(
            "/ui-api/control/approval",
            method="POST",
            authorization="Bearer valid-token",
            body=b'{"request_id":"approval-1","decision":"accept"}',
        )
    )

    assert response.status_code == 409
    assert await _json_response(response) == {"error": "already_resolved"}


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
