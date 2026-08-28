from __future__ import annotations

import asyncio
from typing import Any

import pytest

from codex_bridge.activity import ActivityStore
from codex_bridge.bridge import Bridge
from codex_bridge.history import HistoryValidationError
from codex_bridge.paths import AllowedPathPolicy, PathPolicyError
from codex_bridge.state import StateStore


class FakeAppServer:
    def __init__(self) -> None:
        self.methods: list[str] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses: list[tuple[int | str, dict[str, Any]]] = []
        self.rejections: list[tuple[int | str, int, str]] = []
        self.thread_cwds: dict[str, str] = {}
        self.thread_histories: dict[str, list[dict[str, Any]]] = {}
        self.thread_history_modes: dict[str, str] = {}
        self.thread_list: list[dict[str, Any]] = []
        self.turns_response: dict[str, Any] = {"data": []}
        self.items_response: dict[str, Any] = {"data": []}

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
            if params.get("includeTurns") and params["threadId"] in self.thread_histories:
                thread["turns"] = self.thread_histories[params["threadId"]]
            if params["threadId"] in self.thread_history_modes:
                thread["historyMode"] = self.thread_history_modes[params["threadId"]]
            return {"thread": thread}
        if method == "thread/turns/list":
            return self.turns_response
        if method == "thread/items/list":
            return self.items_response
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


def make_activity_bridge(allowed_dir) -> tuple[Bridge, FakeAppServer, StateStore, ActivityStore]:
    app = FakeAppServer()
    store = StateStore()
    activities = ActivityStore()
    bridge = Bridge(
        app,
        store,
        AllowedPathPolicy((str(allowed_dir),)),
        activity_store=activities,
    )
    return bridge, app, store, activities


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
async def test_threads_history_omits_reasoning_thread_items(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_cwds["native-thread"] = str(allowed_dir)
    app.thread_histories["native-thread"] = [
        {
            "id": "turn",
            "items": [
                {
                    "id": "user-item",
                    "type": "userMessage",
                    "content": [{"type": "text", "text": "normal userMessage"}],
                },
                {
                    "id": "reasoning-item",
                    "type": "reasoning",
                    "summary": "reasoning summary",
                    "content": ["reasoning content"],
                },
                {"id": "agent-item", "type": "agentMessage", "text": "normal agentMessage"},
            ],
        }
    ]

    detail = await bridge.threads("native-thread", include_history=True)

    items = detail["thread"]["turns"][0]["items"]
    assert [item["type"] for item in items] == ["userMessage", "agentMessage"]
    assert "reasoning item" not in str(detail)
    assert "reasoning summary" not in str(detail)
    assert "reasoning content" not in str(detail)


@pytest.mark.asyncio
async def test_read_thread_turns_preflights_metadata_and_maps_paginated_page(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_cwds["paginated-thread"] = str(allowed_dir)
    app.thread_history_modes["paginated-thread"] = "paginated"
    app.turns_response = {
        "data": [
            {
                "id": "turn-1",
                "status": "completed",
                "startedAt": 1,
                "completedAt": 2,
                "durationMs": 1,
                "itemsView": "notLoaded",
            }
        ],
        "nextCursor": "next",
        "backwardsCursor": "back",
    }

    result = await bridge.read_thread_turns(
        "paginated-thread", limit=2, cursor="cursor", sort_direction="asc"
    )

    assert app.calls == [
        ("thread/read", {"threadId": "paginated-thread", "includeTurns": False}),
        (
            "thread/turns/list",
            {
                "threadId": "paginated-thread",
                "limit": 2,
                "cursor": "cursor",
                "sortDirection": "asc",
                "itemsView": "notLoaded",
            },
        ),
    ]
    assert result["history_mode"] == "paginated"
    assert result["next_cursor"] == "next"
    assert result["backwards_cursor"] == "back"
    assert result["turns"][0]["items_view"] == "notLoaded"


@pytest.mark.asyncio
async def test_read_thread_items_preflights_metadata_and_preserves_entry_turn_id(
    allowed_dir,
) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_cwds["paginated-thread"] = str(allowed_dir)
    app.thread_history_modes["paginated-thread"] = "paginated"
    app.items_response = {
        "data": [
            {
                "turnId": "turn-1",
                "item": {"id": "agent-1", "type": "agentMessage", "text": "hello"},
            }
        ],
        "nextCursor": "next",
        "backwardsCursor": "back",
    }

    result = await bridge.read_thread_items(
        "paginated-thread", turn_id="turn-1", limit=3, cursor="cursor", sort_direction="asc"
    )

    assert app.calls == [
        ("thread/read", {"threadId": "paginated-thread", "includeTurns": False}),
        (
            "thread/items/list",
            {
                "threadId": "paginated-thread",
                "turnId": "turn-1",
                "limit": 3,
                "cursor": "cursor",
                "sortDirection": "asc",
            },
        ),
    ]
    assert result["items"][0]["turn_id"] == "turn-1"
    assert result["next_cursor"] == "next"
    assert result["backwards_cursor"] == "back"


@pytest.mark.asyncio
async def test_read_thread_history_uses_legacy_fallback_without_cursor(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_cwds["legacy-thread"] = str(allowed_dir)
    app.thread_histories["legacy-thread"] = [
        {
            "id": "turn-1",
            "status": "completed",
            "items": [
                {"id": "agent-1", "type": "agentMessage", "text": "safe"},
                {"id": "reasoning-1", "type": "reasoning", "summary": ["private"]},
            ],
        }
    ]

    turns = await bridge.read_thread_turns("legacy-thread", limit=1)
    items = await bridge.read_thread_items("legacy-thread", limit=1)

    assert turns["history_mode"] == "legacy"
    assert turns["turns"][0]["items"][0]["text"] == "safe"
    assert items["items"][0]["item"]["text"] == "safe"
    assert app.methods == [
        "thread/read",
        "thread/read",
        "thread/read",
        "thread/read",
    ]
    assert app.calls[1][1] == {"threadId": "legacy-thread", "includeTurns": True}
    assert app.calls[3][1] == {"threadId": "legacy-thread", "includeTurns": True}
    assert turns["next_cursor"] is None
    assert items["next_cursor"] is None
    assert "private" not in str(turns)


@pytest.mark.asyncio
async def test_read_legacy_history_passes_sort_direction_to_projection(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_cwds["legacy-thread"] = str(allowed_dir)
    app.thread_histories["legacy-thread"] = [
        {"id": "turn-1", "status": "completed", "items": []},
        {"id": "turn-2", "status": "completed", "items": []},
        {"id": "turn-3", "status": "completed", "items": []},
    ]

    descending = await bridge.read_thread_turns("legacy-thread", limit=2, sort_direction="desc")
    ascending = await bridge.read_thread_turns("legacy-thread", limit=2, sort_direction="asc")

    assert [turn["id"] for turn in descending["turns"]] == ["turn-3", "turn-2"]
    assert [turn["id"] for turn in ascending["turns"]] == ["turn-1", "turn-2"]


@pytest.mark.asyncio
async def test_read_legacy_items_passes_sort_direction_to_projection(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_cwds["legacy-thread"] = str(allowed_dir)
    app.thread_histories["legacy-thread"] = [
        {
            "id": "turn-1",
            "status": "completed",
            "items": [
                {"id": "item-1", "type": "agentMessage", "text": "old"},
                {"id": "private", "type": "reasoning", "summary": ["private"]},
                {"id": "item-2", "type": "agentMessage", "text": "new"},
            ],
        },
        {
            "id": "turn-2",
            "status": "completed",
            "items": [{"id": "item-3", "type": "agentMessage", "text": "newest"}],
        },
    ]

    descending = await bridge.read_thread_items("legacy-thread", limit=2, sort_direction="desc")
    ascending = await bridge.read_thread_items(
        "legacy-thread", turn_id="turn-1", limit=2, sort_direction="asc"
    )

    assert [entry["item"]["id"] for entry in descending["items"]] == ["item-3", "item-2"]
    assert [entry["item"]["id"] for entry in ascending["items"]] == ["item-1", "item-2"]


@pytest.mark.asyncio
async def test_read_legacy_history_rejects_cursor_after_metadata_preflight(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_cwds["legacy-thread"] = str(allowed_dir)

    with pytest.raises(HistoryValidationError, match="cursor"):
        await bridge.read_thread_turns("legacy-thread", cursor="fake")

    assert app.methods == ["thread/read"]


@pytest.mark.asyncio
async def test_read_history_rejects_invalid_query_without_rpc(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)

    with pytest.raises(HistoryValidationError):
        await bridge.read_thread_turns("thread", limit=0)
    with pytest.raises(HistoryValidationError):
        await bridge.read_thread_items("thread", sort_direction="sideways")
    with pytest.raises(HistoryValidationError, match="cursor"):
        await bridge.read_thread_items("thread", cursor=123)  # type: ignore[arg-type]

    assert app.methods == []


@pytest.mark.asyncio
async def test_read_paginated_history_rejects_disallowed_thread_before_history_rpc(
    allowed_dir, tmp_path
) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside-history"
    outside.mkdir()
    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_cwds["outside-thread"] = str(outside)
    app.thread_history_modes["outside-thread"] = "paginated"

    with pytest.raises(PathPolicyError):
        await bridge.read_thread_items("outside-thread")

    assert app.methods == ["thread/read"]


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
                "item": {"id": "agent", "type": "agentMessage", "text": "final message"},
            },
        }
    )

    recent = activities.get_recent("thread", "turn")
    assert len(recent) == 1
    assert recent[0].type == "agent_message"
    assert recent[0].summary == "final message"


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
