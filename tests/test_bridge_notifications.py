# The imported fixture intentionally shares its name with pytest test parameters.
# ruff: noqa: F811
from __future__ import annotations

import asyncio

import pytest

from tests.bridge_test_fakes import make_activity_bridge, make_bridge
from tests.bridge_test_support import allowed_dir  # noqa: F401


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
async def test_wait_uses_fifty_second_default(allowed_dir, monkeypatch) -> None:
    bridge, _, store = make_bridge(allowed_dir)
    store.ensure_turn("thread", "turn")
    observed_timeouts: list[float] = []

    async def wait_for_change(_thread_id: str, _turn_id: str, timeout: float) -> bool:
        observed_timeouts.append(timeout)
        return False

    monkeypatch.setattr(store, "wait_for_change", wait_for_change)

    result = await bridge.wait("thread", "turn")

    assert observed_timeouts == pytest.approx([50.0], abs=0.01)
    assert result["state"] == "in_progress"


@pytest.mark.asyncio
async def test_wait_request_is_capped_at_fifty_five_seconds(allowed_dir, monkeypatch) -> None:
    bridge, _, store = make_bridge(allowed_dir)
    store.ensure_turn("thread", "turn")
    observed_timeouts: list[float] = []

    async def wait_for_change(_thread_id: str, _turn_id: str, timeout: float) -> bool:
        observed_timeouts.append(timeout)
        return False

    monkeypatch.setattr(store, "wait_for_change", wait_for_change)

    result = await bridge.wait("thread", "turn", 60.0)

    assert observed_timeouts == pytest.approx([55.0], abs=0.01)
    assert result["state"] == "in_progress"


@pytest.mark.asyncio
async def test_active_wait_timeout_returns_in_progress_without_rpc(allowed_dir) -> None:
    bridge, app, store = make_bridge(allowed_dir)
    store.ensure_turn("thread", "turn")

    result = await bridge.wait("thread", "turn", 0.01)

    assert result["state"] == "in_progress"
    assert app.methods == []


@pytest.mark.asyncio
async def test_same_active_turn_can_be_waited_on_repeatedly(allowed_dir) -> None:
    bridge, app, store = make_bridge(allowed_dir)
    store.ensure_turn("thread", "turn")

    first = await bridge.wait("thread", "turn", 0)
    second = await bridge.wait("thread", "turn", 0)

    assert first["state"] == "in_progress"
    assert second["state"] == "in_progress"
    assert app.methods == []


@pytest.mark.asyncio
async def test_wait_returns_immediately_for_pending_approval(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    await bridge.handle_server_request(
        {
            "id": "approval-1",
            "method": "item/fileChange/requestApproval",
            "params": {"threadId": "thread", "turnId": "turn"},
        }
    )

    result = await asyncio.wait_for(bridge.wait("thread", "turn", 50.0), timeout=0.5)

    assert result["state"] == "needs_approval"
    assert result["pending_request"]["request_id"] == "approval-1"
    assert app.methods == []


@pytest.mark.asyncio
async def test_wait_returns_immediately_for_pending_user_input(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
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

    result = await asyncio.wait_for(bridge.wait("thread", "turn", 50.0), timeout=0.5)

    assert result["state"] == "needs_input"
    assert result["pending_request"]["request_id"] == "input-1"
    assert app.methods == []


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

    public_snapshot = await bridge.wait("thread", "turn", 0)
    assert {
        "state",
        "latest_agent_message",
        "current_diff",
        "pending_request",
        "error",
    }.issubset(public_snapshot)
    assert public_snapshot["state"] == "in_progress"
    assert public_snapshot["latest_agent_message"] == "hello"
    assert public_snapshot["current_diff"] == "diff"
    assert public_snapshot["pending_request"] is None
    assert public_snapshot["error"] is None


@pytest.mark.asyncio
async def test_item_notifications_are_normalized_without_raw_output_or_diff(allowed_dir) -> None:
    bridge, _, _, activities = make_activity_bridge(allowed_dir)
    file_path = str(allowed_dir / "src" / "changed.py")
    outside_path = str(allowed_dir.parent / "outside-secret.py")

    bridge.handle_notification(
        {
            "method": "turn/started",
            "params": {
                "threadId": "thread",
                "turn": {"id": "turn", "status": "inProgress"},
            },
        }
    )
    bridge.handle_notification(
        {
            "method": "item/started",
            "params": {
                "threadId": "thread",
                "turnId": "turn",
                "startedAtMs": 1,
                "item": {
                    "id": "command-item",
                    "type": "commandExecution",
                    "command": "pytest tests/test_bridge.py",
                    "commandActions": [],
                    "cwd": str(allowed_dir),
                    "status": "inProgress",
                    "aggregatedOutput": "secret command output",
                },
            },
        }
    )
    bridge.handle_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread",
                "turnId": "turn",
                "completedAtMs": 2,
                "item": {
                    "id": "command-item",
                    "type": "commandExecution",
                    "command": "pytest tests/test_bridge.py",
                    "commandActions": [],
                    "cwd": str(allowed_dir),
                    "status": "completed",
                    "exitCode": 0,
                    "aggregatedOutput": "secret command output",
                },
            },
        }
    )
    bridge.handle_notification(
        {
            "method": "item/started",
            "params": {
                "threadId": "thread",
                "turnId": "turn",
                "startedAtMs": 3,
                "item": {
                    "id": "file-item",
                    "type": "fileChange",
                    "status": "inProgress",
                    "changes": [
                        {"path": file_path, "kind": {"type": "update"}, "diff": "secret-diff"},
                        {"path": outside_path, "kind": {"type": "update"}, "diff": "outside-diff"},
                    ],
                },
            },
        }
    )
    bridge.handle_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread",
                "turnId": "turn",
                "completedAtMs": 4,
                "item": {
                    "id": "file-item",
                    "type": "fileChange",
                    "status": "completed",
                    "changes": [
                        {"path": file_path, "kind": {"type": "update"}, "diff": "secret-diff"},
                        {"path": outside_path, "kind": {"type": "update"}, "diff": "outside-diff"},
                    ],
                },
            },
        }
    )

    recent = [activity.to_dict() for activity in activities.get_recent("thread", "turn")]

    assert [activity["type"] for activity in recent] == [
        "turn_started",
        "command_started",
        "command_completed",
        "file_change_started",
        "file_change_completed",
    ]
    assert recent[2]["details"] == {"exit_code": 0}
    assert recent[4]["details"] == {"paths": ["src/changed.py"]}
    assert "secret command output" not in str(recent)
    assert "secret-diff" not in str(recent)
    assert "outside-secret.py" not in str(recent)


@pytest.mark.asyncio
async def test_turn_terminal_notifications_are_normalized_as_activities(allowed_dir) -> None:
    bridge, _, _, activities = make_activity_bridge(allowed_dir)
    for native_status, expected_type, expected_status in (
        ("completed", "turn_completed", "completed"),
        ("failed", "turn_failed", "failed"),
        ("interrupted", "turn_interrupted", "interrupted"),
    ):
        bridge.handle_notification(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread",
                    "turn": {
                        "id": native_status,
                        "status": native_status,
                        "error": {"message": "bounded failure"},
                    },
                },
            }
        )
        latest = activities.latest("thread", native_status)
        assert latest is not None
        assert latest.type == expected_type
        assert latest.status == expected_status


@pytest.mark.asyncio
async def test_error_notification_records_only_bounded_error_summary(allowed_dir) -> None:
    bridge, _, store, activities = make_activity_bridge(allowed_dir)
    store.ensure_turn("thread", "turn")

    bridge.handle_notification(
        {
            "method": "error",
            "params": {
                "threadId": "thread",
                "turnId": "turn",
                "willRetry": False,
                "error": {"message": "App Server request failed"},
                "raw": "must not be retained",
            },
        }
    )

    public = activities.latest("thread", "turn")
    assert public is not None
    assert public.type == "error"
    assert public.status == "failed"
    assert public.summary == "App Server request failed"
    assert "must not be retained" not in str(public.to_dict())
    assert (await bridge.wait("thread", "turn", 0))["state"] == "failed"


@pytest.mark.asyncio
async def test_retryable_error_keeps_turn_active_until_completion(allowed_dir) -> None:
    bridge, _, store, activities = make_activity_bridge(allowed_dir)
    store.ensure_turn("thread", "turn")

    bridge.handle_notification(
        {
            "method": "error",
            "params": {
                "threadId": "thread",
                "turnId": "turn",
                "willRetry": True,
                "error": {"message": "transient App Server error"},
            },
        }
    )

    result = await bridge.wait("thread", "turn", 0)
    activity = activities.latest("thread", "turn")

    assert result["state"] == "in_progress"
    assert activity is not None
    assert activity.type == "error"
    assert activity.status == "in_progress"

    bridge.handle_notification(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thread",
                "turn": {"id": "turn", "status": "completed"},
            },
        }
    )

    assert (await bridge.wait("thread", "turn", 0))["state"] == "completed"


@pytest.mark.asyncio
async def test_retryable_error_preserves_pending_request(allowed_dir) -> None:
    bridge, _, _, _ = make_activity_bridge(allowed_dir)
    await bridge.handle_server_request(
        {
            "id": "approval-1",
            "method": "item/fileChange/requestApproval",
            "params": {"threadId": "thread", "turnId": "turn", "reason": "write"},
        }
    )

    bridge.handle_notification(
        {
            "method": "error",
            "params": {
                "threadId": "thread",
                "turnId": "turn",
                "willRetry": True,
                "error": {"message": "transient App Server error"},
            },
        }
    )

    result = await bridge.wait("thread", "turn", 0)

    assert result["state"] == "needs_approval"
    assert result["pending_request"]["request_id"] == "approval-1"


@pytest.mark.asyncio
async def test_malformed_will_retry_fails_closed(allowed_dir) -> None:
    bridge, _, store, activities = make_activity_bridge(allowed_dir)
    store.ensure_turn("thread", "turn")

    bridge.handle_notification(
        {
            "method": "error",
            "params": {
                "threadId": "thread",
                "turnId": "turn",
                "willRetry": "true",
                "error": {"message": "malformed retry flag"},
            },
        }
    )

    result = await bridge.wait("thread", "turn", 0)
    activity = activities.latest("thread", "turn")

    assert result["state"] == "failed"
    assert activity is not None
    assert activity.status == "failed"


@pytest.mark.asyncio
async def test_agent_deltas_update_state_but_create_one_completed_activity(allowed_dir) -> None:
    bridge, _, _, activities = make_activity_bridge(allowed_dir)

    for _ in range(100):
        bridge.handle_notification(
            {
                "method": "item/agentMessage/delta",
                "params": {"threadId": "thread", "turnId": "turn", "itemId": "agent", "delta": "x"},
            }
        )
    bridge.handle_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread",
                "turnId": "turn",
                "completedAtMs": 1,
                "item": {
                    "id": "commentary",
                    "type": "agentMessage",
                    "text": "progress update",
                    "phase": "commentary",
                },
            },
        }
    )
    bridge.handle_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread",
                "turnId": "turn",
                "completedAtMs": 2,
                "item": {
                    "id": "agent",
                    "type": "agentMessage",
                    "text": "final message",
                    "phase": "final_answer",
                },
            },
        }
    )

    recent = activities.get_recent("thread", "turn")
    assert [activity.type for activity in recent] == ["agent_commentary", "agent_message"]
    assert [activity.summary for activity in recent] == ["progress update", "final message"]


@pytest.mark.asyncio
async def test_approval_and_user_input_lifecycle_are_activities(allowed_dir) -> None:
    bridge, _, _, activities = make_activity_bridge(allowed_dir)
    await bridge.handle_server_request(
        {
            "id": "approval-1",
            "method": "item/fileChange/requestApproval",
            "params": {
                "itemId": "file-item",
                "threadId": "thread",
                "turnId": "turn",
                "reason": "write",
            },
        }
    )
    await bridge.approve("approval-1", "accept")
    await bridge.handle_server_request(
        {
            "id": "input-1",
            "method": "item/tool/requestUserInput",
            "params": {
                "itemId": "input-item",
                "threadId": "thread",
                "turnId": "turn",
                "questions": [{"header": "Choice", "id": "choice", "question": "Pick one"}],
            },
        }
    )
    await bridge.answer_user_input("input-1", {"choice": ["yes"]})

    assert [activity.type for activity in activities.get_recent("thread", "turn")] == [
        "approval_requested",
        "approval_resolved",
        "user_input_requested",
        "user_input_resolved",
    ]
    assert "yes" not in str([activity.to_dict() for activity in activities.get_recent("thread")])


def test_app_server_failure_records_bounded_error_and_failed_turn(allowed_dir) -> None:
    bridge, _, store, activities = make_activity_bridge(allowed_dir)
    store.ensure_turn("thread", "turn")

    bridge.handle_app_server_failure("JSON-RPC transport closed with secret trace")

    assert store.snapshot("thread", "turn")["state"] == "failed"
    assert [activity.type for activity in activities.get_recent("thread", "turn")] == [
        "turn_failed",
        "error",
    ]
    public = [activity.to_dict() for activity in activities.get_recent("thread", "turn")]
    assert "secret trace" not in str(public)


@pytest.mark.asyncio
async def test_reasoning_items_are_not_recorded_or_returned(allowed_dir) -> None:
    bridge, _, store, activities = make_activity_bridge(allowed_dir)
    store.ensure_turn("thread", "turn")
    reasoning = {
        "id": "reasoning-item",
        "type": "reasoning",
        "raw": "fake raw payload",
        "encrypted": "fake encrypted payload",
        "chain_of_thought": "fake chain of thought",
        "reasoning": "fake reasoning body",
        "content": ["fake reasoning body"],
    }

    bridge.handle_notification(
        {
            "method": "item/started",
            "params": {"threadId": "thread", "turnId": "turn", "item": reasoning},
        }
    )
    bridge.handle_notification(
        {
            "method": "item/completed",
            "params": {"threadId": "thread", "turnId": "turn", "item": reasoning},
        }
    )

    result = await bridge.status("thread", "turn")
    assert activities.get_recent("thread", "turn") == ()
    assert all(
        value not in str(result)
        for value in (
            "fake raw payload",
            "fake encrypted payload",
            "fake chain of thought",
            "fake reasoning body",
        )
    )


@pytest.mark.asyncio
async def test_status_selects_active_or_latest_turn_without_fabricating_unknown_turn(
    allowed_dir,
) -> None:
    bridge, _, store, _ = make_activity_bridge(allowed_dir)
    await bridge.start(str(allowed_dir), "prompt")

    active = await bridge.status("native-thread")
    assert active["turn_id"] == "native-turn"
    assert active["state"] == "in_progress"
    assert active["recent_activities"][0]["type"] == "turn_started"

    bridge.handle_notification(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "native-thread",
                "turn": {"id": "native-turn", "status": "completed"},
            },
        }
    )
    latest = await bridge.status("native-thread", activity_limit=1)
    assert latest["turn_id"] == "native-turn"
    assert latest["state"] == "completed"
    assert len(latest["recent_activities"]) == 1

    unknown = await bridge.status("native-thread", "missing-turn")
    assert unknown["turn_id"] == "missing-turn"
    assert unknown["state"] == "not_loaded"
    assert unknown["recent_activities"] == []
    assert store.has_turn("native-thread", "missing-turn") is False


@pytest.mark.asyncio
async def test_status_validates_activity_limit_and_pending_approval(allowed_dir) -> None:
    bridge, _, _, _ = make_activity_bridge(allowed_dir)
    with pytest.raises(ValueError, match="activity_limit"):
        await bridge.status("thread", activity_limit=0)
    with pytest.raises(ValueError, match="activity_limit"):
        await bridge.status("thread", activity_limit=101)

    await bridge.handle_server_request(
        {
            "id": "approval-1",
            "method": "item/fileChange/requestApproval",
            "params": {"threadId": "thread", "turnId": "turn", "reason": "write"},
        }
    )
    result = await bridge.status("thread", "turn")
    assert result["state"] == "needs_approval"
    assert result["pending_request"]["request_id"] == "approval-1"


@pytest.mark.asyncio
async def test_status_for_unknown_thread_returns_not_loaded_without_state_creation(
    allowed_dir,
) -> None:
    bridge, _, store, _ = make_activity_bridge(allowed_dir)

    result = await bridge.status("unknown-thread")

    assert result == {
        "thread_id": "unknown-thread",
        "turn_id": None,
        "thread_metadata": {
            "model_provider": None,
            "model": None,
            "reasoning_effort": None,
            "cli_version": None,
            "sandbox_mode": None,
            "approval_policy": None,
            "approvals_reviewer": None,
        },
        "state": "not_loaded",
        "latest_agent_message": "",
        "current_diff": "",
        "pending_request": None,
        "error": None,
        "latest_activity": None,
        "recent_activities": [],
    }
    assert store.has_thread("unknown-thread") is False


@pytest.mark.asyncio
async def test_unknown_wait_does_not_create_ghost_or_override_latest_status(allowed_dir) -> None:
    bridge, _, store, _ = make_activity_bridge(allowed_dir)
    store.ensure_turn("thread-a", "known-turn")
    store.set_terminal("thread-a", "known-turn", "completed")

    wait_result = await bridge.wait("thread-a", "ghost-turn", 0)

    assert wait_result["turn_id"] == "ghost-turn"
    assert wait_result["state"] == "not_loaded"
    assert store.has_turn("thread-a", "ghost-turn") is False
    assert store.active_turn_for_thread("thread-a") is None
    assert store.latest_known_turn("thread-a") == "known-turn"

    status = await bridge.status("thread-a")
    assert status["turn_id"] == "known-turn"
    assert status["state"] == "completed"
