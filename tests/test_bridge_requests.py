from __future__ import annotations

import asyncio
from typing import Any

import pytest

from codex_bridge.bridge import ApprovalConflictError, Bridge
from codex_bridge.paths import AllowedPathPolicy
from codex_bridge.state import StateStore
from tests.bridge_test_fakes import FakeAppServer, make_bridge

pytest_plugins = ("tests.bridge_test_support",)


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
async def test_concurrent_approval_attempts_send_only_one_app_server_response(allowed_dir) -> None:
    class BlockingAppServer(FakeAppServer):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def respond(self, request_id: int | str, result: dict[str, Any]) -> None:
            self.responses.append((request_id, result))
            self.entered.set()
            await self.release.wait()

    app = BlockingAppServer()
    bridge = Bridge(app, StateStore(), AllowedPathPolicy((str(allowed_dir),)))
    await bridge.handle_server_request(
        {
            "id": "approval-race",
            "method": "item/fileChange/requestApproval",
            "params": {"threadId": "thread", "turnId": "turn"},
        }
    )

    first = asyncio.create_task(bridge.approve("approval-race", "accept"))
    await app.entered.wait()

    second_started = asyncio.Event()

    async def second_approval():
        second_started.set()
        return await bridge.approve("approval-race", "decline")

    second = asyncio.create_task(second_approval())
    await second_started.wait()
    app.release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)

    assert sum(not isinstance(result, BaseException) for result in results) == 1
    assert sum(isinstance(result, ApprovalConflictError) for result in results) == 1
    assert app.responses == [("approval-race", {"decision": "accept"})]


@pytest.mark.asyncio
async def test_has_pending_request_is_read_only_and_matches_exact_id(allowed_dir) -> None:
    bridge, app, store = make_bridge(allowed_dir)
    request_id = "notebook::request::eyJyZXF1ZXN0X2lkIjoxfQ"
    await bridge.handle_server_request(
        {
            "id": request_id,
            "method": "item/fileChange/requestApproval",
            "params": {"itemId": "item", "threadId": "thread", "turnId": "turn"},
        }
    )
    pending = store.get_pending_request(request_id)

    assert bridge.has_pending_request(request_id) is True
    assert bridge.has_pending_request("notebook::request::other") is False
    assert store.get_pending_request(request_id) is pending
    assert app.responses == []


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
