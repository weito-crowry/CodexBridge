from __future__ import annotations

import asyncio
from typing import Any

import pytest

from codex_bridge.bridge import Bridge
from codex_bridge.paths import AllowedPathPolicy, PathPolicyError
from codex_bridge.state import StateStore


class FakeAppServer:
    def __init__(self) -> None:
        self.methods: list[str] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses: list[tuple[int | str, dict[str, Any]]] = []
        self.rejections: list[tuple[int | str, int, str]] = []
        self.thread_cwds: dict[str, str] = {}
        self.thread_list: list[dict[str, Any]] = []

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.methods.append(method)
        self.calls.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "native-thread"}}
        if method == "thread/resume":
            return {
                "thread": {"id": params["threadId"]},
                "cwd": self.thread_cwds[params["threadId"]],
            }
        if method == "turn/start":
            return {"turn": {"id": "native-turn", "status": "inProgress"}}
        if method == "turn/steer":
            return {"turnId": params["expectedTurnId"]}
        if method == "turn/interrupt":
            return {}
        if method == "thread/list":
            return {"data": self.thread_list}
        if method == "thread/read":
            thread: dict[str, Any] = {
                "id": params["threadId"],
                "turns": [],
                "raw": "do not expose",
                "chain_of_thought": "do not expose",
            }
            if params["threadId"] in self.thread_cwds:
                thread["cwd"] = self.thread_cwds[params["threadId"]]
            return {"thread": thread}
        raise AssertionError(f"unexpected method {method}")

    async def respond(self, request_id: int | str, result: dict[str, Any]) -> None:
        self.responses.append((request_id, result))

    async def reject(self, request_id: int | str, code: int, message: str) -> None:
        self.rejections.append((request_id, code, message))


@pytest.fixture
def allowed_dir(tmp_path):
    return tmp_path


def make_bridge(allowed_dir) -> tuple[Bridge, FakeAppServer, StateStore]:
    app = FakeAppServer()
    store = StateStore()
    bridge = Bridge(app, store, AllowedPathPolicy((str(allowed_dir),)))
    return bridge, app, store


@pytest.mark.asyncio
async def test_start_returns_native_ids_without_waiting_for_completion(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)

    result = await bridge.start(str(allowed_dir), "inspect this")

    assert result["thread_id"] == "native-thread"
    assert result["turn_id"] == "native-turn"
    assert result["state"] == "in_progress"
    assert app.methods == ["thread/start", "turn/start"]
    assert app.calls[1][1]["input"] == [{"type": "text", "text": "inspect this"}]


@pytest.mark.asyncio
async def test_continue_resumes_a_thread_not_loaded_in_this_process(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_cwds["persisted-thread"] = str(allowed_dir)

    await bridge.continue_thread("persisted-thread", "continue")

    assert app.methods == ["thread/read", "thread/resume", "turn/start"]


@pytest.mark.asyncio
async def test_continue_validates_persisted_thread_cwd_before_resume(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_cwds["persisted-thread"] = str(allowed_dir)

    await bridge.continue_thread("persisted-thread", "continue")

    assert app.methods == ["thread/read", "thread/resume", "turn/start"]


@pytest.mark.asyncio
async def test_continue_rejects_out_of_root_thread_before_resume(allowed_dir, tmp_path) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    outside = tmp_path.parent / f"{tmp_path.name}-outside-continue"
    outside.mkdir()
    app.thread_cwds["outside-thread"] = str(outside)

    with pytest.raises(PathPolicyError):
        await bridge.continue_thread("outside-thread", "continue")

    assert app.methods == ["thread/read"]


@pytest.mark.asyncio
async def test_threads_rejects_out_of_root_detail_before_history(allowed_dir, tmp_path) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    outside = tmp_path.parent / f"{tmp_path.name}-outside-read"
    outside.mkdir()
    app.thread_cwds["outside-thread"] = str(outside)

    with pytest.raises(PathPolicyError):
        await bridge.threads("outside-thread", include_history=True)

    assert app.methods == ["thread/read"]


@pytest.mark.asyncio
async def test_threads_filters_list_rows_by_canonical_cwd(allowed_dir, tmp_path) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    outside = tmp_path.parent / f"{tmp_path.name}-outside-list"
    outside.mkdir()
    app.thread_list = [
        {"id": "allowed", "cwd": str(allowed_dir)},
        {"id": "outside", "cwd": str(outside)},
    ]

    result = await bridge.threads(limit=10)

    assert [thread["id"] for thread in result["threads"]] == ["allowed"]


@pytest.mark.asyncio
async def test_persisted_thread_path_policy_rejects_sibling_prefix_and_case_variant(
    allowed_dir, tmp_path
) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    sibling = allowed_dir.parent / f"{allowed_dir.name}-sibling"
    sibling.mkdir()
    app.thread_cwds["sibling-thread"] = str(sibling)
    with pytest.raises(PathPolicyError):
        await bridge.continue_thread("sibling-thread", "continue")

    case_variant = str(sibling).swapcase()
    app.thread_cwds["case-thread"] = case_variant
    with pytest.raises(PathPolicyError):
        await bridge.continue_thread("case-thread", "continue")


@pytest.mark.asyncio
async def test_persisted_thread_path_policy_rejects_symlink_escape(allowed_dir, tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link = allowed_dir / "linked-thread-cwd"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink unavailable: {exc}")

    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_cwds["linked-thread"] = str(link)

    with pytest.raises(PathPolicyError):
        await bridge.continue_thread("linked-thread", "continue")


@pytest.mark.asyncio
async def test_steer_uses_expected_turn_id(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)

    await bridge.steer("thread", "turn", "change direction")

    assert app.calls[-1][1]["expectedTurnId"] == "turn"


@pytest.mark.asyncio
async def test_threads_list_and_read_are_bounded_and_sanitized(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_cwds["native-thread"] = str(allowed_dir)

    listed = await bridge.threads(limit=5, cursor="next")
    detail = await bridge.threads("native-thread", include_history=True)

    assert listed["threads"] == []
    assert app.calls == [
        ("thread/list", {"limit": 5, "cursor": "next"}),
        ("thread/read", {"threadId": "native-thread", "includeTurns": False}),
        ("thread/read", {"threadId": "native-thread", "includeTurns": True}),
    ]
    assert detail["thread"] == {
        "id": "native-thread",
        "cwd": str(allowed_dir.resolve()),
        "turns": [],
    }


@pytest.mark.asyncio
async def test_wait_returns_immediately_for_terminal_event(allowed_dir) -> None:
    bridge, _, _ = make_bridge(allowed_dir)
    await bridge.start(str(allowed_dir), "prompt")

    bridge.handle_notification(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "native-thread",
                "turn": {"id": "native-turn", "status": "completed"},
            },
        }
    )

    result = await bridge.wait("native-thread", "native-turn", 0.1)

    assert result["state"] == "completed"


@pytest.mark.asyncio
async def test_wait_wakes_when_matching_turn_completes(allowed_dir) -> None:
    bridge, _, _ = make_bridge(allowed_dir)
    await bridge.start(str(allowed_dir), "prompt")
    waiter = asyncio.create_task(bridge.wait("native-thread", "native-turn", 1.0))
    await asyncio.sleep(0)

    bridge.handle_notification(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "native-thread",
                "turn": {"id": "native-turn", "status": "completed"},
            },
        }
    )

    assert (await waiter)["state"] == "completed"


@pytest.mark.asyncio
async def test_terminal_statuses_are_normalized(allowed_dir) -> None:
    bridge, _, store = make_bridge(allowed_dir)
    for native, expected in (
        ("completed", "completed"),
        ("interrupted", "interrupted"),
        ("failed", "failed"),
    ):
        store.ensure_turn("thread", "turn")
        bridge.handle_notification(
            {
                "method": "turn/completed",
                "params": {"threadId": "thread", "turn": {"id": "turn", "status": native}},
            }
        )
        assert store.snapshot("thread", "turn")["state"] == expected


@pytest.mark.asyncio
async def test_agent_delta_and_diff_are_retained(allowed_dir) -> None:
    bridge, _, store = make_bridge(allowed_dir)

    bridge.handle_notification(
        {
            "method": "item/agentMessage/delta",
            "params": {"threadId": "thread", "turnId": "turn", "itemId": "item", "delta": "hello"},
        }
    )
    bridge.handle_notification(
        {
            "method": "turn/diff/updated",
            "params": {"threadId": "thread", "turnId": "turn", "diff": "diff"},
        }
    )

    snapshot = store.snapshot("thread", "turn")
    assert snapshot["latest_agent_message"] == "hello"
    assert snapshot["current_diff"] == "diff"


@pytest.mark.asyncio
async def test_approval_response_is_scoped_to_request_id(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    await bridge.handle_server_request(
        {
            "id": "approval-1",
            "method": "item/fileChange/requestApproval",
            "params": {"itemId": "item", "threadId": "thread", "turnId": "turn", "reason": "write"},
        }
    )

    result = await bridge.approve("approval-1", "accept")

    assert result["state"] == "in_progress"
    assert app.responses == [("approval-1", {"decision": "accept"})]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decision",
    ["accept", "acceptForSession", "decline", "cancel"],
)
async def test_permission_approval_maps_to_schema_response(allowed_dir, decision) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    requested = {
        "fileSystem": {
            "entries": [
                {
                    "access": "read",
                    "path": {"type": "path", "path": str(allowed_dir)},
                }
            ]
        },
        "network": {"enabled": False},
    }
    request_profile = {**requested, "credential": "must not be retained"}
    await bridge.handle_server_request(
        {
            "id": "permission-1",
            "method": "item/permissions/requestApproval",
            "params": {
                "itemId": "item",
                "threadId": "thread",
                "turnId": "turn",
                "cwd": str(allowed_dir),
                "environmentId": "environment-1",
                "permissions": request_profile,
                "reason": "need read access",
                "startedAtMs": 1,
            },
        }
    )

    pending = (await bridge.wait("thread", "turn", 0))["pending_request"]
    assert pending["permission"]["requested_permissions"] == requested
    assert pending["permission"]["cwd"] == str(allowed_dir.resolve())
    assert pending["permission"]["environment_id"] == "environment-1"
    assert pending["permission"]["reason"] == "need read access"
    assert pending["permission"]["allowed_scopes"] == ["turn", "session"]

    await bridge.approve("permission-1", decision)

    if decision in {"accept", "acceptForSession"}:
        expected = {
            "permissions": requested,
            "scope": "session" if decision == "acceptForSession" else "turn",
        }
    else:
        expected = {
            "permissions": {"fileSystem": None, "network": None},
            "scope": "turn",
        }
    assert app.responses[-1] == ("permission-1", expected)


@pytest.mark.asyncio
async def test_user_input_response_matches_question_ids(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    await bridge.handle_server_request(
        {
            "id": "input-1",
            "method": "item/tool/requestUserInput",
            "params": {
                "itemId": "item",
                "threadId": "thread",
                "turnId": "turn",
                "questions": [{"header": "Choice", "id": "choice", "question": "Pick one"}],
            },
        }
    )

    await bridge.answer_user_input("input-1", {"choice": ["yes"]})

    assert app.responses == [("input-1", {"answers": {"choice": {"answers": ["yes"]}}})]


@pytest.mark.asyncio
async def test_unknown_user_input_question_is_rejected(allowed_dir) -> None:
    bridge, _, _ = make_bridge(allowed_dir)
    await bridge.handle_server_request(
        {
            "id": "input-1",
            "method": "item/tool/requestUserInput",
            "params": {
                "itemId": "item",
                "threadId": "thread",
                "turnId": "turn",
                "questions": [{"header": "Choice", "id": "choice", "question": "Pick one"}],
            },
        }
    )

    with pytest.raises(ValueError, match="question"):
        await bridge.answer_user_input("input-1", {"other": ["no"]})


@pytest.mark.asyncio
async def test_approval_pending_is_cleared_by_server_request_resolved(allowed_dir) -> None:
    bridge, _, _ = make_bridge(allowed_dir)
    await bridge.handle_server_request(
        {
            "id": "approval-1",
            "method": "item/fileChange/requestApproval",
            "params": {"threadId": "thread", "turnId": "turn"},
        }
    )

    bridge.handle_notification(
        {
            "method": "serverRequest/resolved",
            "params": {"requestId": "approval-1", "threadId": "thread"},
        }
    )

    snapshot = await bridge.wait("thread", "turn", 0)
    assert snapshot["pending_request"] is None
    assert snapshot["state"] == "in_progress"


@pytest.mark.asyncio
async def test_user_input_pending_is_cleared_by_server_request_resolved(allowed_dir) -> None:
    bridge, _, _ = make_bridge(allowed_dir)
    await bridge.handle_server_request(
        {
            "id": "input-1",
            "method": "item/tool/requestUserInput",
            "params": {
                "threadId": "thread",
                "turnId": "turn",
                "questions": [{"header": "Choice", "id": "choice", "question": "Pick one"}],
            },
        }
    )

    bridge.handle_notification(
        {
            "method": "serverRequest/resolved",
            "params": {"requestId": "input-1", "threadId": "thread"},
        }
    )

    snapshot = await bridge.wait("thread", "turn", 0)
    assert snapshot["pending_request"] is None
    assert snapshot["state"] == "in_progress"


@pytest.mark.asyncio
async def test_pending_request_is_purged_when_turn_completes(allowed_dir) -> None:
    bridge, _, _ = make_bridge(allowed_dir)
    await bridge.handle_server_request(
        {
            "id": "approval-1",
            "method": "item/fileChange/requestApproval",
            "params": {"threadId": "thread", "turnId": "turn"},
        }
    )

    bridge.handle_notification(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thread",
                "turn": {"id": "turn", "status": "completed"},
            },
        }
    )
    bridge.handle_notification(
        {
            "method": "serverRequest/resolved",
            "params": {"requestId": "approval-1", "threadId": "thread"},
        }
    )

    snapshot = await bridge.wait("thread", "turn", 0)
    assert snapshot["state"] == "completed"
    assert snapshot["pending_request"] is None


@pytest.mark.asyncio
async def test_interrupt_does_not_terminalize_pending_turn_but_completion_cleans_it(
    allowed_dir,
) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    await bridge.handle_server_request(
        {
            "id": "approval-1",
            "method": "item/fileChange/requestApproval",
            "params": {"threadId": "thread", "turnId": "turn"},
        }
    )

    interrupted = await bridge.interrupt("thread", "turn")
    assert interrupted["state"] == "needs_approval"
    assert app.methods == ["turn/interrupt"]

    bridge.handle_notification(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thread",
                "turn": {"id": "turn", "status": "interrupted"},
            },
        }
    )

    snapshot = await bridge.wait("thread", "turn", 0)
    assert snapshot["state"] == "interrupted"
    assert snapshot["pending_request"] is None


@pytest.mark.asyncio
async def test_duplicate_or_unknown_resolved_does_not_clear_other_pending_request(
    allowed_dir,
) -> None:
    bridge, _, _ = make_bridge(allowed_dir)
    await bridge.handle_server_request(
        {
            "id": "approval-1",
            "method": "item/fileChange/requestApproval",
            "params": {"threadId": "thread", "turnId": "turn"},
        }
    )

    resolved = {
        "method": "serverRequest/resolved",
        "params": {"requestId": "unknown", "threadId": "thread"},
    }
    bridge.handle_notification(resolved)
    bridge.handle_notification(
        {
            "method": "serverRequest/resolved",
            "params": {"requestId": "approval-1", "threadId": "other-thread"},
        }
    )

    snapshot = await bridge.wait("thread", "turn", 0)
    assert snapshot["pending_request"]["request_id"] == "approval-1"
    bridge.handle_notification(
        {
            "method": "serverRequest/resolved",
            "params": {"requestId": "approval-1", "threadId": "thread"},
        }
    )
    bridge.handle_notification(resolved)
    assert (await bridge.wait("thread", "turn", 0))["pending_request"] is None


@pytest.mark.asyncio
async def test_interrupt_response_does_not_become_terminal_state(allowed_dir) -> None:
    bridge, app, store = make_bridge(allowed_dir)
    store.ensure_turn("thread", "turn")

    result = await bridge.interrupt("thread", "turn")

    assert result["state"] == "in_progress"
    assert app.methods == ["turn/interrupt"]


def test_app_server_failure_marks_active_turn_failed(allowed_dir) -> None:
    bridge, _, store = make_bridge(allowed_dir)
    store.ensure_turn("thread", "turn")

    bridge.handle_app_server_failure("JSON-RPC transport closed")

    snapshot = store.snapshot("thread", "turn")
    assert snapshot["state"] == "failed"
    assert snapshot["error"] == "JSON-RPC transport closed"


@pytest.mark.asyncio
async def test_interrupt_active_turns_waits_for_terminal_notification(allowed_dir) -> None:
    bridge, app, store = make_bridge(allowed_dir)
    store.ensure_turn("thread", "turn")
    interruption = asyncio.create_task(bridge.interrupt_active_turns(wait_seconds=0.5))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert app.methods == ["turn/interrupt"]
    bridge.handle_notification(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thread",
                "turn": {"id": "turn", "status": "interrupted"},
            },
        }
    )

    await interruption
    assert store.snapshot("thread", "turn")["state"] == "interrupted"


@pytest.mark.asyncio
async def test_interrupt_active_turns_bounds_hung_interrupt_request(allowed_dir) -> None:
    bridge, app, store = make_bridge(allowed_dir)
    store.ensure_turn("thread", "turn")
    original_request = app.request

    async def hanging_interrupt(method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "turn/interrupt":
            await asyncio.sleep(10)
        return await original_request(method, params)

    app.request = hanging_interrupt

    await asyncio.wait_for(bridge.interrupt_active_turns(wait_seconds=0.05), timeout=0.5)


@pytest.mark.asyncio
async def test_unsupported_server_request_is_rejected_without_raising(allowed_dir) -> None:
    bridge, app, store = make_bridge(allowed_dir)
    store.ensure_turn("thread", "turn")

    await bridge.handle_server_request(
        {
            "id": "unknown-1",
            "method": "future/request",
            "params": {"threadId": "thread", "turnId": "turn"},
        }
    )

    assert app.rejections == [("unknown-1", -32601, "unsupported App Server request")]
    assert store.snapshot("thread", "turn")["state"] == "in_progress"


@pytest.mark.asyncio
async def test_mcp_server_elicitation_is_cancelled_with_schema_response(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)

    await bridge.handle_server_request(
        {
            "id": "elicitation-1",
            "method": "mcpServer/elicitation/request",
            "params": {
                "serverName": "example-mcp",
                "threadId": "thread",
                "turnId": None,
                "message": "Choose a value",
                "mode": "form",
                "requestedSchema": {"type": "object", "properties": {}},
            },
        }
    )

    assert app.responses == [("elicitation-1", {"action": "cancel"})]
