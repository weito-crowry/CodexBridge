from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any

import pytest
from starlette.requests import Request

from codex_bridge.activity import ActivityStore
from codex_bridge.bridge import BridgeError
from codex_bridge.config import BridgeConfig
from codex_bridge.jsonrpc import JsonRpcClosedError
from codex_bridge.ui_api import create_ui_app


class FakeBridge:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.cwd = ""
        self.state: dict[str, Any] = {
            "state": "completed",
            "pending_request": None,
        }
        self.history_turns: list[dict[str, Any]] = []
        self.start_gate: asyncio.Event | None = None
        self.start_error: Exception | None = None
        self.capabilities_error: Exception | None = None
        self.status_turn_id: str | None = None
        self.operation_gate: asyncio.Event | None = None
        self.operation_entered: dict[str, asyncio.Event] = {}
        self.operation_error: Exception | None = None

    async def model_capabilities(self) -> dict[str, Any]:
        self.calls.append(("model_capabilities", (), {}))
        if self.capabilities_error is not None:
            raise self.capabilities_error
        return {
            "models": [
                {
                    "model": "model-a",
                    "display_name": "Model A",
                    "description": None,
                    "reasoning_efforts": [{"id": "high", "description": None}],
                    "default_reasoning_effort": "high",
                }
            ],
            "defaults": {"model": "model-a", "reasoning_effort": "high"},
            "execution_modes": [
                {"id": "inherit", "display_name": "Default"},
                {"id": "danger-full-access", "display_name": "Full access"},
            ],
        }

    async def start(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("start", args, kwargs))
        if self.start_gate is not None:
            await self.start_gate.wait()
        if self.start_error is not None:
            raise self.start_error
        title = kwargs.get("title") or "Auto title"
        return {
            "thread_id": "native-thread-1",
            "turn_id": "native-turn-1",
            "state": "in_progress",
            "title_assignment": {"status": "assigned", "name": title},
            "latest_agent_message": "must not leak",
        }

    async def threads(self, thread_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("threads", (thread_id,), kwargs))
        if thread_id is None:
            return {"threads": []}
        return {"thread": {"id": thread_id, "cwd": self.cwd}}

    async def status(self, thread_id: str, turn_id: str | None = None) -> dict[str, Any]:
        self.calls.append(("status", (thread_id, turn_id), {}))
        return {
            "thread_id": thread_id,
            "turn_id": self.status_turn_id or turn_id,
            **self.state,
        }

    async def read_thread_turns(self, thread_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("read_thread_turns", (thread_id,), kwargs))
        return {"thread_id": thread_id, "turns": self.history_turns, "next_cursor": None}

    async def continue_thread(self, thread_id: str, prompt: str) -> dict[str, Any]:
        self.calls.append(("continue_thread", (thread_id, prompt), {}))
        await self._wait_for_operation(thread_id)
        return {
            "thread_id": thread_id,
            "turn_id": "continued-turn",
            "state": "in_progress",
            "latest_agent_message": "must not leak",
        }

    async def interrupt(self, thread_id: str, turn_id: str) -> dict[str, Any]:
        self.calls.append(("interrupt", (thread_id, turn_id), {}))
        await self._wait_for_operation(thread_id)
        return {"thread_id": thread_id, "turn_id": turn_id, "state": "in_progress"}

    async def _wait_for_operation(self, thread_id: str) -> None:
        if self.operation_gate is not None:
            self.operation_entered.setdefault(thread_id, asyncio.Event()).set()
            await self.operation_gate.wait()
        if self.operation_error is not None:
            error = self.operation_error
            self.operation_error = None
            raise error


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
        control_token="local-control-token",
    )


def request(
    path: str,
    *,
    body: bytes = b"",
    authorization: str | None = None,
    origin: str | None = None,
    content_type: str | None = "application/json",
    path_params: dict[str, str] | None = None,
    method: str = "POST",
) -> Request:
    headers = [(b"host", b"127.0.0.1:8001"), (b"content-type", b"application/json")]
    if content_type is None:
        headers = [(b"host", b"127.0.0.1:8001")]
    elif content_type != "application/json":
        headers[-1] = (b"content-type", content_type.encode())
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    result = Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": b"",
            "headers": headers,
            "path_params": path_params or {},
        }
    )
    result._body = body
    return result


def route(app, path: str):
    return next(route for route in app.routes if getattr(route, "path", None) == path)


async def send_continue(app, thread_id: str, request_id: str, prompt: str = "go"):
    endpoint = route(app, "/ui-api/control/threads/{thread_id}/turns").endpoint
    return await endpoint(
        request(
            f"/ui-api/control/threads/{thread_id}/turns",
            body=json.dumps({"client_request_id": request_id, "prompt": prompt}).encode(),
            authorization="Bearer local-control-token",
            path_params={"thread_id": thread_id},
        )
    )


async def send_interrupt(app, thread_id: str, turn_id: str, request_id: str):
    endpoint = route(app, "/ui-api/control/threads/{thread_id}/turns/{turn_id}/interrupt").endpoint
    return await endpoint(
        request(
            f"/ui-api/control/threads/{thread_id}/turns/{turn_id}/interrupt",
            body=json.dumps({"client_request_id": request_id}).encode(),
            authorization="Bearer local-control-token",
            path_params={"thread_id": thread_id, "turn_id": turn_id},
        )
    )


@pytest.mark.asyncio
async def test_control_create_thread_starts_native_thread_and_returns_safe_ids(tmp_path) -> None:
    bridge = FakeBridge()
    app = create_ui_app(bridge, ActivityStore(), config(tmp_path))
    body = json.dumps(
        {
            "client_request_id": "request-0001",
            "cwd": str(tmp_path),
            "prompt": "日本語の指示\n2行目です",
            "model": "model-a",
            "reasoning_effort": "high",
            "sandbox_mode": "inherit",
            "title": "  Title   Label  ",
        },
        ensure_ascii=False,
    ).encode()

    response = await route(app, "/ui-api/control/threads").endpoint(
        request(
            "/ui-api/control/threads",
            body=body,
            authorization="Bearer local-control-token",
        )
    )

    assert response.status_code == 200
    assert json.loads(response.body) == {
        "thread_id": "native-thread-1",
        "turn_id": "native-turn-1",
        "state": "in_progress",
        "title_assignment": {"status": "assigned", "name": "Title Label"},
    }
    assert bridge.calls[-1] == (
        "start",
        (str(tmp_path), "日本語の指示\n2行目です"),
        {
            "model": "model-a",
            "reasoning_effort": "high",
            "sandbox_mode": None,
            "title": "Title Label",
        },
    )


@pytest.mark.asyncio
async def test_control_routes_require_token_and_allow_absent_origin(tmp_path) -> None:
    absent = create_ui_app(
        FakeBridge(), ActivityStore(), replace(config(tmp_path), control_token=None)
    )
    absent_paths = {getattr(item, "path", "") for item in absent.routes}
    assert (
        not {
            "/ui-api/control/capabilities",
            "/ui-api/control/threads",
            "/ui-api/control/threads/{thread_id}/turns",
            "/ui-api/control/threads/{thread_id}/turns/{turn_id}/interrupt",
        }
        & absent_paths
    )

    bridge = FakeBridge()
    bridge.cwd = str(tmp_path)
    app = create_ui_app(bridge, ActivityStore(), config(tmp_path))
    endpoint = route(app, "/ui-api/control/threads").endpoint
    body = json.dumps(
        {"client_request_id": "request-auth-1", "cwd": str(tmp_path), "prompt": "go"}
    ).encode()
    denied = await endpoint(request("/ui-api/control/threads", body=body))
    bad_origin = await endpoint(
        request(
            "/ui-api/control/threads",
            body=body,
            authorization="Bearer local-control-token",
            origin="https://127.0.0.1:8001",
        )
    )
    accepted = await endpoint(
        request(
            "/ui-api/control/threads",
            body=body,
            authorization="Bearer local-control-token",
        )
    )

    assert denied.status_code == bad_origin.status_code == 403
    assert accepted.status_code == 200
    assert len([call for call in bridge.calls if call[0] == "start"]) == 1


@pytest.mark.asyncio
async def test_control_capabilities_uses_bearer_and_local_origin(tmp_path) -> None:
    bridge = FakeBridge()
    app = create_ui_app(bridge, ActivityStore(), config(tmp_path))
    endpoint = route(app, "/ui-api/control/capabilities").endpoint
    denied = await endpoint(
        request(
            "/ui-api/control/capabilities",
            authorization="Bearer wrong-token",
            method="GET",
        )
    )
    wrong_port = await endpoint(
        request(
            "/ui-api/control/capabilities",
            authorization="Bearer local-control-token",
            origin="http://localhost:8002",
            method="GET",
        )
    )
    allowed = await endpoint(
        request(
            "/ui-api/control/capabilities",
            authorization="Bearer local-control-token",
            origin="http://localhost:8001",
            method="GET",
        )
    )

    assert denied.status_code == wrong_port.status_code == 403
    assert allowed.status_code == 200
    assert json.loads(allowed.body)["models"][0]["model"] == "model-a"
    assert bridge.calls == [("model_capabilities", (), {})]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body,content_type",
    [
        (b"{", "application/json"),
        (b'{"client_request_id":"request-bad-1","cwd":"x","prompt":" "}', "application/json"),
        (
            b'{"client_request_id":"request-bad-1","cwd":"x","prompt":"go","model":"x"}',
            "text/plain",
        ),
        (
            b'{"client_request_id":"request-bad-1","cwd":"x","prompt":"go","extra":1}',
            "application/json",
        ),
    ],
)
async def test_control_create_rejects_invalid_json_media_prompt_and_fields(
    tmp_path, body: bytes, content_type: str
) -> None:
    bridge = FakeBridge()
    app = create_ui_app(bridge, ActivityStore(), config(tmp_path))
    response = await route(app, "/ui-api/control/threads").endpoint(
        request(
            "/ui-api/control/threads",
            body=body,
            content_type=content_type,
            authorization="Bearer local-control-token",
        )
    )

    assert response.status_code == 400
    assert not any(call[0] == "start" for call in bridge.calls)


@pytest.mark.asyncio
async def test_control_create_enforces_body_and_prompt_limits(tmp_path) -> None:
    app = create_ui_app(FakeBridge(), ActivityStore(), config(tmp_path))
    endpoint = route(app, "/ui-api/control/threads").endpoint
    too_large = await endpoint(
        request(
            "/ui-api/control/threads",
            body=b'{"client_request_id":"request-big-1","cwd":"x","prompt":"go"}'
            + b" " * (64 * 1024),
            authorization="Bearer local-control-token",
        )
    )
    too_long = await endpoint(
        request(
            "/ui-api/control/threads",
            body=json.dumps(
                {
                    "client_request_id": "request-big-2",
                    "cwd": str(tmp_path),
                    "prompt": "a" * 32_001,
                }
            ).encode(),
            authorization="Bearer local-control-token",
        )
    )

    assert too_large.status_code == too_long.status_code == 400


@pytest.mark.asyncio
async def test_control_create_validates_root_model_effort_and_full_access(tmp_path) -> None:
    bridge = FakeBridge()
    app = create_ui_app(bridge, ActivityStore(), config(tmp_path))
    endpoint = route(app, "/ui-api/control/threads").endpoint

    async def submit(request_id: str, **fields: Any):
        payload = {"client_request_id": request_id, "cwd": str(tmp_path), "prompt": "go", **fields}
        return await endpoint(
            request(
                "/ui-api/control/threads",
                body=json.dumps(payload).encode(),
                authorization="Bearer local-control-token",
            )
        )

    outside = await submit("request-outside-1", cwd=str(tmp_path.parent))
    unknown_model = await submit("request-model-1", model="similar-model")
    unknown_effort = await submit("request-effort-1", model="model-a", reasoning_effort="max")
    effort_only = await submit("request-effort-2", reasoning_effort="high")
    unconfirmed = await submit(
        "request-full-1", sandbox_mode="danger-full-access", confirm_full_access=False
    )
    confirmed = await submit(
        "request-full-2", sandbox_mode="danger-full-access", confirm_full_access=True
    )

    assert outside.status_code == 404
    assert [unknown_model.status_code, unknown_effort.status_code, effort_only.status_code] == [
        400,
        400,
        400,
    ]
    assert unconfirmed.status_code == 400
    assert confirmed.status_code == 200
    started = [call for call in bridge.calls if call[0] == "start"]
    assert len(started) == 1
    assert started[0][2]["sandbox_mode"] == "danger-full-access"


@pytest.mark.asyncio
async def test_control_create_refuses_to_start_when_model_catalog_is_unavailable(tmp_path) -> None:
    bridge = FakeBridge()
    bridge.capabilities_error = BridgeError("unavailable model catalog")
    app = create_ui_app(bridge, ActivityStore(), config(tmp_path))
    response = await route(app, "/ui-api/control/threads").endpoint(
        request(
            "/ui-api/control/threads",
            body=json.dumps(
                {
                    "client_request_id": "request-catalog-1",
                    "cwd": str(tmp_path),
                    "prompt": "go",
                    "model": "model-a",
                }
            ).encode(),
            authorization="Bearer local-control-token",
        )
    )

    assert response.status_code == 502
    assert not any(call[0] == "start" for call in bridge.calls)


@pytest.mark.asyncio
async def test_control_create_reports_app_server_disconnect_as_unavailable(tmp_path) -> None:
    bridge = FakeBridge()
    bridge.capabilities_error = JsonRpcClosedError("private transport detail")
    app = create_ui_app(bridge, ActivityStore(), config(tmp_path))
    response = await route(app, "/ui-api/control/threads").endpoint(
        request(
            "/ui-api/control/threads",
            body=json.dumps(
                {
                    "client_request_id": "request-disconnect-1",
                    "cwd": str(tmp_path),
                    "prompt": "go",
                    "model": "model-a",
                }
            ).encode(),
            authorization="Bearer local-control-token",
        )
    )

    assert response.status_code == 503
    assert "private transport detail" not in response.body.decode()
    assert not any(call[0] == "start" for call in bridge.calls)


@pytest.mark.asyncio
async def test_control_create_leaves_automatic_title_assignment_to_bridge(tmp_path) -> None:
    bridge = FakeBridge()
    app = create_ui_app(bridge, ActivityStore(), config(tmp_path))
    response = await route(app, "/ui-api/control/threads").endpoint(
        request(
            "/ui-api/control/threads",
            body=json.dumps(
                {
                    "client_request_id": "request-title-1",
                    "cwd": str(tmp_path),
                    "prompt": "# A meaningful heading\nContinue here.",
                }
            ).encode(),
            authorization="Bearer local-control-token",
        )
    )

    assert response.status_code == 200
    assert [call for call in bridge.calls if call[0] == "start"][0][2]["title"] is None
    assert json.loads(response.body)["title_assignment"] == {
        "status": "assigned",
        "name": "Auto title",
    }


@pytest.mark.asyncio
async def test_control_create_reuses_success_and_rejects_changed_payload(tmp_path) -> None:
    bridge = FakeBridge()
    app = create_ui_app(bridge, ActivityStore(), config(tmp_path))
    endpoint = route(app, "/ui-api/control/threads").endpoint

    async def send(prompt: str):
        body = json.dumps(
            {"client_request_id": "request-repeat-1", "cwd": str(tmp_path), "prompt": prompt}
        ).encode()
        return await endpoint(
            request(
                "/ui-api/control/threads",
                body=body,
                authorization="Bearer local-control-token",
            )
        )

    first = await send("go")
    repeated = await send("go")
    changed = await send("different")

    assert first.status_code == repeated.status_code == 200
    assert first.body == repeated.body
    assert changed.status_code == 409
    assert len([call for call in bridge.calls if call[0] == "start"]) == 1


@pytest.mark.asyncio
async def test_control_create_concurrent_duplicate_dispatches_once(tmp_path) -> None:
    bridge = FakeBridge()
    bridge.start_gate = asyncio.Event()
    app = create_ui_app(bridge, ActivityStore(), config(tmp_path))
    endpoint = route(app, "/ui-api/control/threads").endpoint
    body = json.dumps(
        {"client_request_id": "request-concurrent-1", "cwd": str(tmp_path), "prompt": "go"}
    ).encode()

    first = asyncio.create_task(
        endpoint(
            request(
                "/ui-api/control/threads",
                body=body,
                authorization="Bearer local-control-token",
            )
        )
    )
    while not any(call[0] == "start" for call in bridge.calls):
        await asyncio.sleep(0)
    second = asyncio.create_task(
        endpoint(
            request(
                "/ui-api/control/threads",
                body=body,
                authorization="Bearer local-control-token",
            )
        )
    )
    await asyncio.sleep(0)
    bridge.start_gate.set()
    responses = await asyncio.gather(first, second)

    assert [response.status_code for response in responses] == [200, 200]
    assert responses[0].body == responses[1].body
    assert len([call for call in bridge.calls if call[0] == "start"]) == 1


@pytest.mark.asyncio
async def test_control_create_unknown_result_is_not_retried(tmp_path) -> None:
    bridge = FakeBridge()
    bridge.start_error = BridgeError("private upstream details")
    app = create_ui_app(bridge, ActivityStore(), config(tmp_path))
    endpoint = route(app, "/ui-api/control/threads").endpoint
    body = json.dumps(
        {"client_request_id": "request-uncertain-1", "cwd": str(tmp_path), "prompt": "go"}
    ).encode()

    first = await endpoint(
        request("/ui-api/control/threads", body=body, authorization="Bearer local-control-token")
    )
    repeated = await endpoint(
        request("/ui-api/control/threads", body=body, authorization="Bearer local-control-token")
    )

    assert first.status_code == 502
    assert repeated.status_code == 409
    assert "private upstream details" not in first.body.decode()
    assert len([call for call in bridge.calls if call[0] == "start"]) == 1


@pytest.mark.asyncio
async def test_control_continue_checks_thread_state_and_rejects_overrides(tmp_path) -> None:
    bridge = FakeBridge()
    bridge.cwd = str(tmp_path)
    app = create_ui_app(bridge, ActivityStore(), config(tmp_path))
    endpoint = route(app, "/ui-api/control/threads/{thread_id}/turns").endpoint
    path_params = {"thread_id": "thread-1"}

    async def send(request_id: str, payload: dict[str, Any]):
        return await endpoint(
            request(
                "/ui-api/control/threads/thread-1/turns",
                body=json.dumps({"client_request_id": request_id, **payload}).encode(),
                authorization="Bearer local-control-token",
                path_params=path_params,
            )
        )

    bridge.state = {"state": "in_progress", "pending_request": None}
    active = await send("request-active-1", {"prompt": "wait"})
    bridge.state = {"state": "in_progress", "pending_request": {"method": "approval"}}
    pending = await send("request-pending-1", {"prompt": "wait"})
    bridge.state = {"state": "completed", "pending_request": None}
    override = await send("request-override-1", {"prompt": "go", "model": "model-a"})
    continued = await send("request-continue-1", {"prompt": "追加の指示\n次の行"})

    assert active.status_code == pending.status_code == 409
    assert override.status_code == 400
    assert continued.status_code == 200
    assert bridge.calls[-1] == ("continue_thread", ("thread-1", "追加の指示\n次の行"), {})


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
async def test_control_continue_rejects_concurrent_thread_operation_and_releases_slot(
    tmp_path, outcome: str
) -> None:
    bridge = FakeBridge()
    bridge.cwd = str(tmp_path)
    bridge.operation_gate = asyncio.Event()
    if outcome == "failure":
        bridge.operation_error = BridgeError("private bridge error")
    app = create_ui_app(bridge, ActivityStore(), config(tmp_path))

    first_task = asyncio.create_task(send_continue(app, "thread-1", "request-first-1"))
    await bridge.operation_entered.setdefault("thread-1", asyncio.Event()).wait()
    second_task = asyncio.create_task(send_continue(app, "thread-1", "request-second-1"))
    try:
        await asyncio.wait_for(asyncio.shield(second_task), timeout=0.5)
        second_finished_before_release = True
    except TimeoutError:
        second_finished_before_release = False
    dispatches_before_release = sum(call[0] == "continue_thread" for call in bridge.calls)

    if outcome == "cancel":
        first_task.cancel()
        first_result = await asyncio.gather(first_task, return_exceptions=True)
    else:
        bridge.operation_gate.set()
        first_result = [await first_task]
    bridge.operation_gate.set()
    second_response = await second_task
    follow_up_response = await send_continue(app, "thread-1", "request-follow-up-1")

    assert second_finished_before_release
    assert dispatches_before_release == 1
    assert second_response.status_code == 409
    if outcome == "cancel":
        assert isinstance(first_result[0], asyncio.CancelledError)
    elif outcome == "failure":
        assert first_result[0].status_code != 200
    else:
        assert first_result[0].status_code == 200
    assert follow_up_response.status_code == 200
    assert sum(call[0] == "continue_thread" for call in bridge.calls) == 2


@pytest.mark.asyncio
async def test_control_continue_and_interrupt_conflict_on_same_thread(tmp_path) -> None:
    bridge = FakeBridge()
    bridge.cwd = str(tmp_path)
    bridge.operation_gate = asyncio.Event()
    app = create_ui_app(bridge, ActivityStore(), config(tmp_path))

    continue_task = asyncio.create_task(send_continue(app, "thread-1", "request-cont-1"))
    await bridge.operation_entered.setdefault("thread-1", asyncio.Event()).wait()
    bridge.state = {"state": "in_progress", "pending_request": None}
    interrupt_task = asyncio.create_task(
        send_interrupt(app, "thread-1", "continued-turn", "request-int-0001")
    )
    try:
        await asyncio.wait_for(asyncio.shield(interrupt_task), timeout=0.5)
        interrupt_finished_before_release = True
    except TimeoutError:
        interrupt_finished_before_release = False
    bridge.operation_gate.set()
    continue_response, interrupt_response = await asyncio.gather(continue_task, interrupt_task)

    assert continue_response.status_code == 200
    assert interrupt_finished_before_release
    assert interrupt_response.status_code == 409
    assert not any(call[0] == "interrupt" for call in bridge.calls)


@pytest.mark.asyncio
async def test_control_operations_on_different_threads_do_not_block_each_other(tmp_path) -> None:
    bridge = FakeBridge()
    bridge.cwd = str(tmp_path)
    bridge.operation_gate = asyncio.Event()
    app = create_ui_app(bridge, ActivityStore(), config(tmp_path))
    first_task = asyncio.create_task(send_continue(app, "thread-1", "request-alpha-1"))
    second_task = asyncio.create_task(send_continue(app, "thread-2", "request-bravo-1"))
    both_entered = asyncio.gather(
        bridge.operation_entered.setdefault("thread-1", asyncio.Event()).wait(),
        bridge.operation_entered.setdefault("thread-2", asyncio.Event()).wait(),
    )
    try:
        await asyncio.wait_for(both_entered, timeout=1)
        entered_concurrently = True
    except TimeoutError:
        entered_concurrently = False
    bridge.operation_gate.set()
    responses = await asyncio.gather(first_task, second_task)

    assert entered_concurrently
    assert [response.status_code for response in responses] == [200, 200]


@pytest.mark.asyncio
async def test_control_continue_same_request_id_replay_remains_idempotent(tmp_path) -> None:
    bridge = FakeBridge()
    bridge.cwd = str(tmp_path)
    bridge.operation_gate = asyncio.Event()
    app = create_ui_app(bridge, ActivityStore(), config(tmp_path))

    first_task = asyncio.create_task(send_continue(app, "thread-1", "request-replay-1"))
    await bridge.operation_entered.setdefault("thread-1", asyncio.Event()).wait()
    replay_task = asyncio.create_task(send_continue(app, "thread-1", "request-replay-1"))
    await asyncio.sleep(0)
    replay_waited_for_owner = not replay_task.done()
    bridge.operation_gate.set()
    first_response, replay_response = await asyncio.gather(first_task, replay_task)

    assert replay_waited_for_owner
    assert first_response.status_code == replay_response.status_code == 200
    assert sum(call[0] == "continue_thread" for call in bridge.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["needs_approval", "needs_input"])
async def test_control_continue_rejects_approval_and_user_input_waits(tmp_path, state: str) -> None:
    bridge = FakeBridge()
    bridge.cwd = str(tmp_path)
    bridge.state = {"state": state, "pending_request": None}
    app = create_ui_app(bridge, ActivityStore(), config(tmp_path))
    response = await route(app, "/ui-api/control/threads/{thread_id}/turns").endpoint(
        request(
            "/ui-api/control/threads/thread-1/turns",
            body=b'{"client_request_id":"request-waiting-1","prompt":"go"}',
            authorization="Bearer local-control-token",
            path_params={"thread_id": "thread-1"},
        )
    )

    assert response.status_code == 409
    assert not any(call[0] == "continue_thread" for call in bridge.calls)


@pytest.mark.asyncio
async def test_control_continue_rejects_thread_outside_allowed_root(tmp_path) -> None:
    bridge = FakeBridge()
    bridge.cwd = str(tmp_path.parent)
    bridge.state = {"state": "completed", "pending_request": None}
    app = create_ui_app(bridge, ActivityStore(), config(tmp_path))
    response = await route(app, "/ui-api/control/threads/{thread_id}/turns").endpoint(
        request(
            "/ui-api/control/threads/thread-1/turns",
            body=b'{"client_request_id":"request-outside-2","prompt":"go"}',
            authorization="Bearer local-control-token",
            path_params={"thread_id": "thread-1"},
        )
    )

    assert response.status_code == 404
    assert not any(call[0] == "continue_thread" for call in bridge.calls)


@pytest.mark.asyncio
async def test_control_continue_uses_history_when_thread_is_not_loaded(tmp_path) -> None:
    bridge = FakeBridge()
    bridge.cwd = str(tmp_path)
    bridge.state = {"state": "not_loaded", "pending_request": None}
    app = create_ui_app(bridge, ActivityStore(), config(tmp_path))
    endpoint = route(app, "/ui-api/control/threads/{thread_id}/turns").endpoint

    async def send(request_id: str):
        return await endpoint(
            request(
                "/ui-api/control/threads/thread-1/turns",
                body=json.dumps({"client_request_id": request_id, "prompt": "go"}).encode(),
                authorization="Bearer local-control-token",
                path_params={"thread_id": "thread-1"},
            )
        )

    bridge.history_turns = [{"id": "turn-active", "status": "in_progress"}]
    active = await send("request-history-1")
    bridge.history_turns = [{"id": "turn-done", "status": "completed"}]
    completed = await send("request-history-2")

    assert active.status_code == 409
    assert completed.status_code == 200
    assert any(call[0] == "read_thread_turns" for call in bridge.calls)


@pytest.mark.asyncio
async def test_control_interrupt_targets_only_loaded_active_turn(tmp_path) -> None:
    bridge = FakeBridge()
    bridge.cwd = str(tmp_path)
    bridge.state = {"state": "in_progress", "pending_request": None}
    app = create_ui_app(bridge, ActivityStore(), config(tmp_path))
    endpoint = route(app, "/ui-api/control/threads/{thread_id}/turns/{turn_id}/interrupt").endpoint

    async def send(thread_id: str, turn_id: str, request_id: str):
        return await endpoint(
            request(
                f"/ui-api/control/threads/{thread_id}/turns/{turn_id}/interrupt",
                body=json.dumps({"client_request_id": request_id}).encode(),
                authorization="Bearer local-control-token",
                path_params={"thread_id": thread_id, "turn_id": turn_id},
            )
        )

    response = await send("thread-1", "turn-1", "request-interrupt-1")
    bridge.state = {"state": "completed", "pending_request": None}
    completed = await send("thread-1", "turn-2", "request-interrupt-2")
    bridge.state = {"state": "in_progress", "pending_request": {"method": "user_input"}}
    waiting = await send("thread-1", "turn-3", "request-interrupt-3")

    assert response.status_code == 200
    assert completed.status_code == waiting.status_code == 409
    assert [call for call in bridge.calls if call[0] == "interrupt"] == [
        ("interrupt", ("thread-1", "turn-1"), {})
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["needs_approval", "needs_input"])
async def test_control_interrupt_rejects_approval_and_user_input_waits(
    tmp_path, state: str
) -> None:
    bridge = FakeBridge()
    bridge.cwd = str(tmp_path)
    bridge.state = {"state": state, "pending_request": None}
    app = create_ui_app(bridge, ActivityStore(), config(tmp_path))
    response = await route(
        app, "/ui-api/control/threads/{thread_id}/turns/{turn_id}/interrupt"
    ).endpoint(
        request(
            "/ui-api/control/threads/thread-1/turns/turn-1/interrupt",
            body=b'{"client_request_id":"request-waiting-2"}',
            authorization="Bearer local-control-token",
            path_params={"thread_id": "thread-1", "turn_id": "turn-1"},
        )
    )

    assert response.status_code == 409
    assert not any(call[0] == "interrupt" for call in bridge.calls)


@pytest.mark.asyncio
async def test_control_interrupt_rejects_mismatched_turn_status(tmp_path) -> None:
    bridge = FakeBridge()
    bridge.cwd = str(tmp_path)
    bridge.state = {"state": "in_progress", "pending_request": None}
    bridge.status_turn_id = "different-turn"
    app = create_ui_app(bridge, ActivityStore(), config(tmp_path))
    response = await route(
        app, "/ui-api/control/threads/{thread_id}/turns/{turn_id}/interrupt"
    ).endpoint(
        request(
            "/ui-api/control/threads/thread-1/turns/turn-1/interrupt",
            body=b'{"client_request_id":"request-mismatch-1"}',
            authorization="Bearer local-control-token",
            path_params={"thread_id": "thread-1", "turn_id": "turn-1"},
        )
    )

    assert response.status_code == 409
    assert not any(call[0] == "interrupt" for call in bridge.calls)


@pytest.mark.asyncio
async def test_control_interrupt_rejects_unloaded_unknown_active_state(tmp_path) -> None:
    bridge = FakeBridge()
    bridge.cwd = str(tmp_path)
    bridge.state = {"state": "not_loaded", "pending_request": None}
    bridge.history_turns = [{"id": "turn-1", "status": "in_progress"}]
    app = create_ui_app(bridge, ActivityStore(), config(tmp_path))
    endpoint = route(app, "/ui-api/control/threads/{thread_id}/turns/{turn_id}/interrupt").endpoint
    response = await endpoint(
        request(
            "/ui-api/control/threads/thread-1/turns/turn-1/interrupt",
            body=b'{"client_request_id":"request-interrupt-4"}',
            authorization="Bearer local-control-token",
            path_params={"thread_id": "thread-1", "turn_id": "turn-1"},
        )
    )

    assert response.status_code == 409
    assert not any(call[0] == "interrupt" for call in bridge.calls)
