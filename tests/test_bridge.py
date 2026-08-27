from __future__ import annotations

import asyncio
from typing import Any

import pytest

from codex_bridge.bridge import Bridge
from codex_bridge.paths import AllowedPathPolicy
from codex_bridge.state import StateStore


class FakeAppServer:
    def __init__(self) -> None:
        self.methods: list[str] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses: list[tuple[int | str, dict[str, Any]]] = []

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.methods.append(method)
        self.calls.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "native-thread"}}
        if method == "thread/resume":
            return {"thread": {"id": params["threadId"]}}
        if method == "turn/start":
            return {"turn": {"id": "native-turn", "status": "inProgress"}}
        if method == "turn/steer":
            return {"turnId": params["expectedTurnId"]}
        if method == "turn/interrupt":
            return {}
        if method == "thread/list":
            return {"data": []}
        if method == "thread/read":
            return {
                "thread": {
                    "id": params["threadId"],
                    "turns": [],
                    "raw": "do not expose",
                    "chain_of_thought": "do not expose",
                }
            }
        raise AssertionError(f"unexpected method {method}")

    async def respond(self, request_id: int | str, result: dict[str, Any]) -> None:
        self.responses.append((request_id, result))


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

    await bridge.continue_thread("persisted-thread", "continue")

    assert app.methods == ["thread/resume", "turn/start"]


@pytest.mark.asyncio
async def test_steer_uses_expected_turn_id(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)

    await bridge.steer("thread", "turn", "change direction")

    assert app.calls[-1][1]["expectedTurnId"] == "turn"


@pytest.mark.asyncio
async def test_threads_list_and_read_are_bounded_and_sanitized(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)

    listed = await bridge.threads(limit=5, cursor="next")
    detail = await bridge.threads("native-thread", include_history=True)

    assert listed["threads"] == []
    assert app.calls[-2:] == [
        ("thread/list", {"limit": 5, "cursor": "next"}),
        ("thread/read", {"threadId": "native-thread", "includeTurns": True}),
    ]
    assert detail["thread"] == {"id": "native-thread", "turns": []}


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
