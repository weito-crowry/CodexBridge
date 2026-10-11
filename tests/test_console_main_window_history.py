from __future__ import annotations

import pytest
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QTextBrowser,
)

from codex_bridge.console.main_window import MainWindow
from tests.console_main_window_test_support import (
    FakeClient,
    _activity,
    _application,
    _complete_history_snapshot,
    _config,
    _history_activity,
    _history_items,
    _item,
    _usage_window,
    _wait_for_qt_timer,
)


def test_main_window_requests_snapshot_and_applies_connected_status() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)

    window.select_thread("thread-a")
    keys = {key for key, _, _ in client.requests}
    assert any(key.endswith(":detail") for key in keys)
    assert any(key.endswith(":turns") for key in keys)
    assert any(key.endswith(":items") for key in keys)
    status_key = next(key for key in keys if key.endswith(":status"))
    client.result("bridge-status", {"bridge": "ready", "app_server": "ready"})
    client.result(status_key, {"state": "completed", "recent_activities": []})

    assert "connected" in window.bridge_status_label.text().casefold()
    assert "ready" in window.app_server_status_label.text().casefold()
    assert client.streams == [("thread-a", 1)]
    window.close()


def test_history_activity_refresh_is_debounced_and_does_not_request_status() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)
    window.select_thread("thread-a")
    _complete_history_snapshot(client, window._selection_generation)
    client.requests.clear()

    non_target_activity = _history_activity("approval_requested", "approval", "thread-a")
    non_target_activity["status"] = "requested"
    client.activity(window._selection_generation, non_target_activity)
    assert not window._history_refresh_timer.isActive()
    assert any(path.endswith("/status") for _, path, _ in client.requests)
    client.requests.clear()

    for activity_type in ("command_started", "agent_commentary", "file_change_completed"):
        client.activity(
            window._selection_generation,
            _history_activity(activity_type, activity_type, "thread-a"),
        )

    assert window._history_refresh_timer.isActive()
    assert window._history_refresh_timer.isSingleShot()
    assert window._history_refresh_timer.interval() == 250
    assert client.requests == []

    _wait_for_qt_timer(300)

    history_requests = [request for request in client.requests if ":history:" in request[0]]
    assert {key.rsplit(":", 1)[-1] for key, _, _ in history_requests} == {"turns", "items"}
    assert len(history_requests) == 2
    assert all(path.endswith(("/turns", "/items")) for _, path, _ in history_requests)
    assert not any(path.endswith("/status") for _, path, _ in client.requests)
    window.close()


def test_transient_item_events_refresh_history_without_activity_pane_append() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)
    window.select_thread("thread-a")
    _complete_history_snapshot(client, window._selection_generation)
    client.requests.clear()
    activity_count = window.activity_pane.activity_list.count()

    client.activity(
        window._selection_generation,
        _history_activity("item_started", "item-start", "thread-a"),
    )
    assert window.activity_pane.activity_list.count() == activity_count
    assert window._history_refresh_timer.isActive()
    assert window._history_refresh_timer.interval() == 250

    client.activity(
        window._selection_generation,
        _history_activity("item_completed", "item-complete", "thread-a"),
    )
    _wait_for_qt_timer(300)
    history_requests = [request for request in client.requests if ":history:" in request[0]]
    assert len(history_requests) == 2
    assert {key.rsplit(":", 1)[-1] for key, _, _ in history_requests} == {"turns", "items"}
    assert window.activity_pane.activity_list.count() == activity_count
    window.close()


def test_history_refresh_keeps_one_pending_refresh_while_requests_are_in_flight() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)
    window.select_thread("thread-a")
    _complete_history_snapshot(client, window._selection_generation)
    client.requests.clear()
    generation = window._selection_generation

    client.activity(generation, _history_activity("command_started", "first", "thread-a"))
    _wait_for_qt_timer(300)
    first_requests = [request for request in client.requests if ":history:" in request[0]]
    assert len(first_requests) == 2

    client.activity(generation, _history_activity("item_completed", "second", "thread-a"))
    _wait_for_qt_timer(300)
    assert len([request for request in client.requests if ":history:" in request[0]]) == 2

    for key, _, _ in first_requests:
        if key.endswith(":turns"):
            client.failure(key, "Bridge unavailable")
        else:
            client.result(key, {"items": []})
    assert window._history_refresh_timer.isActive()

    _wait_for_qt_timer(300)
    all_history_requests = [request for request in client.requests if ":history:" in request[0]]
    assert len(all_history_requests) == 4
    second_requests = all_history_requests[2:]
    for key, _, _ in second_requests:
        if key.endswith(":turns"):
            client.result(key, {"turns": []})
        else:
            client.result(key, {"items": []})
    _wait_for_qt_timer(300)
    assert len([request for request in client.requests if ":history:" in request[0]]) == 4
    window.close()


def test_history_refresh_state_and_responses_are_isolated_by_thread_selection() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)
    window.select_thread("thread-a")
    _complete_history_snapshot(client, window._selection_generation)
    client.requests.clear()
    first_generation = window._selection_generation

    client.activity(
        first_generation,
        _history_activity("agent_message", "thread-a-event", "thread-a"),
    )
    _wait_for_qt_timer(300)
    first_requests = [request for request in client.requests if ":history:" in request[0]]
    assert len(first_requests) == 2
    client.activity(
        first_generation,
        _history_activity("command_completed", "pending-a", "thread-a"),
    )
    _wait_for_qt_timer(300)
    window.select_thread("thread-b")
    assert not window._history_refresh_timer.isActive()
    assert window._history_refresh_pending is False

    _complete_history_snapshot(client, window._selection_generation)

    client.activity(
        first_generation,
        _history_activity("item_started", "stale-item", "thread-a"),
    )
    assert not window._history_refresh_timer.isActive()

    old_items_key = next(key for key, _, _ in first_requests if key.endswith(":items"))
    client.result(
        old_items_key,
        {"items": [{"turn_id": "old", "item": _item("stale", "agentMessage", text="STALE")}]},
    )
    assert window._timeline_entries == []

    client.requests.clear()
    client.activity(
        window._selection_generation,
        _history_activity("command_started", "thread-b-event", "thread-b"),
    )
    _wait_for_qt_timer(300)
    current_requests = [request for request in client.requests if ":history:" in request[0]]
    assert len(current_requests) == 2
    assert all(
        key.startswith(f"selection:{window._selection_generation}:history:")
        for key, _, _ in current_requests
    )
    window.close()


@pytest.mark.parametrize("failed_suffix", ["items", "turns"])
def test_live_history_failure_preserves_timeline_and_scroll_position(failed_suffix: str) -> None:
    application = _application()
    client = FakeClient()
    window = _usage_window(client)
    window.resize(1_000, 650)
    window.show()
    window.select_thread("thread-a")
    generation = window._selection_generation
    client.result(
        f"selection:{generation}:turns",
        {"turns": [{"id": "turn-a", "status": "completed"}]},
    )
    client.result(f"selection:{generation}:items", _history_items(32))
    _wait_for_qt_timer(50)

    scrollbar = window.history_pane._scroll.verticalScrollBar()
    assert scrollbar.maximum() > 0
    scrollbar.setValue(scrollbar.maximum() // 2)
    for _ in range(4):
        application.processEvents()
    old_value = scrollbar.value()
    old_entries = list(window._timeline_entries)
    window.history_pane._follow_newest = False
    assert not window.history_pane._follow_newest

    client.activity(generation, _history_activity("agent_commentary", "live", "thread-a"))
    _wait_for_qt_timer(300)
    live_requests = [request for request in client.requests if ":history:" in request[0]]
    assert len(live_requests) == 2
    failed_key = next(key for key, _, _ in live_requests if key.endswith(failed_suffix))
    client.failure(failed_key, "Temporary live history failure")

    assert window._timeline_entries == old_entries
    assert any(
        widget.objectName() == "historyCard"
        for widget in window.history_pane._content.findChildren(QFrame)
    )
    assert not window.history_pane._empty_label.isVisible()
    assert scrollbar.value() == old_value
    assert not window.history_pane._follow_newest
    assert "Temporary live history failure" in window.bottom_status_label.text()

    remaining_key = next(key for key, _, _ in live_requests if key != failed_key)
    if remaining_key.endswith(":items"):
        client.result(remaining_key, {"items": []})
    else:
        client.result(remaining_key, {"turns": []})
    window.close()


def test_initial_history_request_is_serialized_before_live_reconciliation() -> None:
    _application()
    client = FakeClient()
    window = _usage_window(client)
    window.select_thread("thread-a")
    generation = window._selection_generation
    initial_items_key = f"selection:{generation}:items"
    initial_turns_key = f"selection:{generation}:turns"

    client.activity(generation, _history_activity("agent_message", "new-message", "thread-a"))
    _wait_for_qt_timer(300)

    assert not any(":history:" in key for key, _, _ in client.requests)
    assert window._history_refresh_pending

    client.result(initial_turns_key, {"turns": [{"id": "turn-a", "status": "in_progress"}]})
    client.result(
        initial_items_key,
        {"items": [{"turn_id": "turn-a", "item": _item("old", "agentMessage", text="old")}]},
    )
    _wait_for_qt_timer(300)
    live_requests = [request for request in client.requests if ":history:" in request[0]]
    assert len(live_requests) == 2

    for key, _, _ in live_requests:
        if key.endswith(":turns"):
            client.result(key, {"turns": [{"id": "turn-a", "status": "completed"}]})
        else:
            client.result(
                key,
                {
                    "items": [
                        {"turn_id": "turn-a", "item": _item("new", "agentMessage", text="new")}
                    ]
                },
            )

    assert [entry.item_id for entry in window._timeline_entries] == ["old", "new"]
    assert window._turn_statuses["turn-a"] == "completed"
    window.close()


def test_manual_snapshot_waits_for_active_history_request_and_replaces_it() -> None:
    _application()
    client = FakeClient()
    window = _usage_window(client)
    window.select_thread("thread-a")
    generation = window._selection_generation
    items_key = f"selection:{generation}:items"
    turns_key = f"selection:{generation}:turns"

    window.refresh()
    assert sum(key == items_key for key, _, _ in client.requests) == 1
    assert sum(key == turns_key for key, _, _ in client.requests) == 1

    client.result(turns_key, {"turns": [{"id": "turn-a", "status": "in_progress"}]})
    client.result(
        items_key,
        {"items": [{"turn_id": "turn-a", "item": _item("old", "agentMessage", text="old")}]},
    )
    assert sum(key == items_key for key, _, _ in client.requests) == 2
    assert sum(key == turns_key for key, _, _ in client.requests) == 2

    client.result(turns_key, {"turns": [{"id": "turn-a", "status": "completed"}]})
    client.result(
        items_key,
        {"items": [{"turn_id": "turn-a", "item": _item("new", "agentMessage", text="new")}]},
    )
    assert [entry.item_id for entry in window._timeline_entries] == ["new"]
    window.close()


def test_live_turn_statuses_merge_and_explicit_snapshot_replaces() -> None:
    _application()
    client = FakeClient()
    window = _usage_window(client)
    window.select_thread("thread-a")
    generation = window._selection_generation
    client.result(
        f"selection:{generation}:turns",
        {
            "turns": [
                {"id": "turn-old", "status": "completed"},
                {"id": "turn-latest", "status": "in_progress"},
            ]
        },
    )
    client.result(
        f"selection:{generation}:items",
        {
            "items": [
                {"turn_id": "turn-latest", "item": _item("latest", "agentMessage", text="latest")}
            ],
            "next_cursor": "older-turn",
        },
    )
    window.load_older()
    older_key = next(key for key, _, _ in client.requests if ":older:" in key)
    client.result(
        older_key,
        {
            "items": [
                {"turn_id": "turn-latest", "item": _item("latest", "agentMessage", text="latest")},
                {"turn_id": "turn-old", "item": _item("old", "agentMessage", text="old")},
            ],
            "next_cursor": None,
        },
    )

    client.activity(generation, _history_activity("turn_completed", "completed", "thread-a"))
    _wait_for_qt_timer(300)
    live_requests = [request for request in client.requests if ":history:" in request[0]]
    for key, _, _ in live_requests:
        if key.endswith(":turns"):
            client.result(key, {"turns": [{"id": "turn-latest", "status": "completed"}]})
        else:
            client.result(
                key,
                {
                    "items": [
                        {
                            "turn_id": "turn-latest",
                            "item": _item("latest", "agentMessage", text="latest updated"),
                        }
                    ]
                },
            )

    assert window._turn_statuses == {
        "turn-old": "completed",
        "turn-latest": "completed",
    }
    headers = [label.text() for label in window.history_pane.findChildren(QLabel, "turnSeparator")]
    assert any("completed" in header for header in headers)

    window._request_snapshot(generation)
    client.result(
        f"selection:{generation}:turns",
        {"turns": [{"id": "turn-latest", "status": "failed"}]},
    )
    client.result(
        f"selection:{generation}:items",
        {
            "items": [
                {
                    "turn_id": "turn-latest",
                    "item": _item("latest", "agentMessage", text="latest"),
                }
            ]
        },
    )
    assert window._turn_statuses == {"turn-latest": "failed"}
    window.close()


def test_sse_reconnect_schedules_one_history_reconciliation_after_first_connect() -> None:
    _application()
    client = FakeClient()
    window = _usage_window(client)
    window.select_thread("thread-a")
    generation = window._selection_generation
    _complete_history_snapshot(client, generation)
    status_key = f"selection:{generation}:status"
    client.result(status_key, {"thread_id": "thread-a", "state": "running"})

    client.stream_state_changed.emit(generation, "connected")
    assert not window._history_refresh_timer.isActive()
    assert not any(":history:" in key for key, _, _ in client.requests)

    client.stream_state_changed.emit(generation, "disconnected")
    client.stream_state_changed.emit(generation, "reconnecting")
    client.stream_state_changed.emit(generation, "connected")
    client.stream_state_changed.emit(generation, "connected")
    client.stream_state_changed.emit(generation, "connected")
    assert window._history_refresh_timer.isActive()

    _wait_for_qt_timer(300)
    history_requests = [request for request in client.requests if ":history:" in request[0]]
    assert len(history_requests) == 2
    assert {key.rsplit(":", 1)[-1] for key, _, _ in history_requests} == {"turns", "items"}
    window.close()


def test_main_window_resolves_pending_approval_and_refreshes_on_sse() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)
    window.select_thread("thread-a")
    status_key = next(key for key, _, _ in client.requests if key.endswith(":status"))
    client.result(
        status_key,
        {
            "thread_id": "thread-a",
            "state": "needs_approval",
            "current_diff": "",
            "pending_request": {
                "request_id": "approval-1",
                "method": "item/commandExecution/requestApproval",
                "thread_id": "thread-a",
                "turn_id": "turn-a",
                "summary": "run checks",
            },
            "recent_activities": [],
        },
    )
    window._runtime_state = "console_started"
    window._control_token = "owned-bridge-token"
    window._sync_approval_controls()

    assert window.activity_pane.allow_once_button.isEnabled()
    window.activity_pane.allow_once_button.click()

    assert client.approval_requests == [
        ("owned-bridge-token", "approval-1", "accept", "control:approval")
    ]
    assert not window.activity_pane.allow_once_button.isEnabled()
    client.control_success("control:approval")
    assert window.activity_pane._approval_feedback.text() == "Approval sent. Refreshing status…"

    before_refresh = len(client.requests)
    client.activity(
        window._selection_generation,
        {
            "activity_id": "approval-resolved",
            "thread_id": "thread-a",
            "turn_id": "turn-a",
            "type": "approval_resolved",
            "status": "resolved",
            "summary": "run checks",
            "details": {},
        },
    )
    assert len(client.requests) > before_refresh
    assert not window.activity_pane.allow_once_button.isEnabled()
    window.close()


def test_external_bridge_approval_is_read_only_and_conflict_is_nonfatal() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)
    window.select_thread("thread-a")
    status_key = next(key for key, _, _ in client.requests if key.endswith(":status"))
    pending_snapshot = {
        "thread_id": "thread-a",
        "state": "needs_approval",
        "pending_request": {
            "request_id": 9,
            "method": "item/fileChange/requestApproval",
            "thread_id": "thread-a",
            "turn_id": "turn-a",
            "summary": "change files",
        },
        "recent_activities": [],
    }
    client.result(status_key, pending_snapshot)
    assert not window.activity_pane.allow_once_button.isEnabled()
    assert window.activity_pane.approval_control_message.text().startswith(
        "Resolve from MCP client"
    )
    window.activity_pane.allow_once_button.click()
    assert client.approval_requests == []

    window._runtime_state = "console_started"
    window._control_token = "owned-token"
    window._sync_approval_controls()
    window.activity_pane.allow_once_button.click()
    assert client.approval_requests == [("owned-token", 9, "accept", "control:approval")]
    client.control_failure("control:approval", "Approval already resolved")

    assert window.runtime_state == "console_started"
    assert "already resolved" in window.activity_pane._approval_feedback.text().lower()
    assert any(key.endswith(":status") for key, _, _ in client.requests)
    window.close()


def test_historical_items_render_commentary_and_final_agent_separately() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)

    window.select_thread("thread-a")
    items_key = next(key for key, _, _ in client.requests if key.endswith(":items"))
    client.result(
        items_key,
        {
            "items": [
                {
                    "turn_id": "turn-1",
                    "item": _item(
                        "agent-final", "agentMessage", text="final answer", phase="final_answer"
                    ),
                },
                {
                    "turn_id": "turn-1",
                    "item": _item(
                        "agent-commentary",
                        "agentMessage",
                        text="progress update",
                        phase="commentary",
                    ),
                },
                {
                    "turn_id": "turn-1",
                    "item": _item("plan", "plan", text="planned work"),
                },
                {"turn_id": "turn-1", "item": _item("user", "userMessage", text="question")},
            ]
        },
    )

    assert [entry.title for entry in window._timeline_entries] == [
        "User",
        "Plan",
        "Commentary",
        "Agent",
    ]
    visible_text = [label.text() for label in window.history_pane._content.findChildren(QLabel)]
    assert "Commentary" in visible_text
    assert "Agent" in visible_text
    window.close()


def test_live_activity_rows_render_commentary_and_final_agent_separately() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)
    window.select_thread("thread-a")

    client.activity(
        1,
        {
            "activity_id": "commentary",
            "thread_id": "thread-a",
            "type": "agent_commentary",
            "status": "completed",
            "summary": "progress update",
        },
    )
    client.activity(
        1,
        {
            "activity_id": "final",
            "thread_id": "thread-a",
            "type": "agent_message",
            "status": "completed",
            "summary": "final answer",
        },
    )

    rows = [window.activity_pane.activity_list.item(index).text() for index in range(2)]
    assert "Commentary" in rows[0]
    assert "Agent" in rows[1]
    window.close()


def test_history_header_includes_turn_model_metadata() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)

    window.select_thread("thread-a")
    status_key = next(key for key, _, _ in client.requests if key.endswith(":status"))
    items_key = next(key for key, _, _ in client.requests if key.endswith(":items"))
    client.result(
        status_key,
        {
            "thread_id": "thread-a",
            "thread_metadata": {"model": "gpt-5", "reasoning_effort": "high"},
            "state": "completed",
            "recent_activities": [],
        },
    )
    client.result(
        items_key,
        {
            "items": [
                {
                    "turn_id": "turn-1",
                    "item": {"id": "agent-1", "type": "agentMessage", "text": "answer"},
                }
            ],
            "turn_model_metadata": {
                "turn-1": {
                    "model_candidates": [{"model": "gpt-5", "reasoning_effort": "high"}],
                    "model_resolution_status": "resolved",
                }
            },
        },
    )

    headers = {label.text() for label in window.history_pane._content.findChildren(QLabel)}
    assert "Turn · Model: gpt-5 (high)" in headers
    assert "Agent · Model: gpt-5 · Reasoning: high" not in headers
    window.close()


def test_health_poll_also_refreshes_bridge_status_and_recovers_without_refresh() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)

    client.failure("bridge-status", "Bridge unavailable")
    assert window.bridge_status_label.text() == "Bridge: disconnected"
    assert window.app_server_status_label.text() == "App Server: failed"

    client.requests.clear()
    window._request_health()

    assert [key for key, _, _ in client.requests] == ["health", "bridge-status"]
    client.result("bridge-status", {"bridge": "ready", "app_server": "ready"})

    assert window.bridge_status_label.text() == "Bridge: connected"
    assert window.app_server_status_label.text() == "App Server: ready"
    window.close()


def test_old_selection_json_and_sse_cannot_update_new_selection() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)

    window.select_thread("thread-a")
    client.result(
        "selection:1:items",
        {"items": [{"turn_id": "a", "item": _item("a", "agentMessage", text="A only")}]},
    )
    window.select_thread("thread-b")
    client.result(
        "selection:1:items",
        {"items": [{"turn_id": "a", "item": _item("stale", "agentMessage", text="STALE A")}]},
    )
    client.activity(1, _activity("stale-activity", "thread-a"))
    client.activity(2, _activity("current-activity", "thread-b"))

    history_bodies = [
        body.toPlainText() for body in window.history_pane._content.findChildren(QTextBrowser)
    ]
    assert not {"A only", "STALE A"}.intersection(history_bodies)
    assert window.activity_pane.activity_list.count() == 1
    assert "current-activity" in window.activity_pane.activity_list.item(0).text()
    window.close()


def test_history_pagination_prepends_older_page_and_deduplicates() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)
    window.select_thread("thread-a")
    client.result("selection:1:turns", {"turns": []})
    client.result(
        "selection:1:items",
        {
            "items": [{"turn_id": "t2", "item": _item("new", "agentMessage", text="new")}],
            "next_cursor": "older",
            "turn_model_metadata": {
                "t2": {
                    "model_candidates": [{"model": "gpt-5.6-luna", "reasoning_effort": "xhigh"}],
                    "model_resolution_status": "resolved",
                }
            },
        },
    )
    window.history_pane.older_requested.emit()
    older_key = next(key for key, _, _ in client.requests if ":older:" in key)
    client.result(
        older_key,
        {
            "items": [
                {"turn_id": "t2", "item": _item("new", "agentMessage", text="new")},
                {"turn_id": "t1", "item": _item("old", "agentMessage", text="old")},
            ],
            "next_cursor": None,
            "turn_model_metadata": {
                "t2": {
                    "model_candidates": [{"model": "gpt-5.6-luna", "reasoning_effort": "xhigh"}],
                    "model_resolution_status": "resolved",
                },
                "t1": {
                    "model_candidates": [{"model": "gpt-5.6-sol", "reasoning_effort": "high"}],
                    "model_resolution_status": "resolved",
                },
            },
        },
    )

    assert window.history_pane.load_older_button.isVisible() is False
    assert len(window._timeline_entries) == 2
    assert [entry.item_id for entry in window._timeline_entries] == ["old", "new"]
    assert set(window._turn_model_metadata) == {"t1", "t2"}
    window.close()


def test_live_history_refresh_preserves_loaded_older_entries_and_order() -> None:
    _application()
    client = FakeClient()
    window = _usage_window(client)
    window.select_thread("thread-a")
    client.result("selection:1:turns", {"turns": []})
    client.result(
        "selection:1:items",
        {
            "items": [
                {"turn_id": "t2", "item": _item("latest", "agentMessage", text="latest")},
                {"turn_id": "t2", "item": _item("current", "agentMessage", text="current")},
            ],
            "next_cursor": "older-1",
        },
    )
    window.load_older()
    older_key = next(key for key, _, _ in client.requests if ":older:" in key)
    client.result(
        older_key,
        {
            "items": [
                {"turn_id": "t2", "item": _item("latest", "agentMessage", text="latest")},
                {"turn_id": "t2", "item": _item("current", "agentMessage", text="current")},
                {"turn_id": "t1", "item": _item("old", "agentMessage", text="old")},
            ],
            "next_cursor": None,
            "turn_model_metadata": {"t1": {"model_resolution_status": "resolved"}},
        },
    )

    client.requests.clear()
    client.activity(
        window._selection_generation,
        _history_activity("agent_message", "new-message", "thread-a"),
    )
    _wait_for_qt_timer(300)
    history_requests = [request for request in client.requests if ":history:" in request[0]]
    for key, _, _ in history_requests:
        if key.endswith(":turns"):
            client.result(key, {"turns": []})
        else:
            client.result(
                key,
                {
                    "items": [
                        {"turn_id": "t3", "item": _item("newest", "agentMessage", text="newest")},
                        {
                            "turn_id": "t2",
                            "item": _item("latest", "agentMessage", text="latest updated"),
                        },
                        {"turn_id": "t2", "item": _item("current", "agentMessage", text="current")},
                    ],
                    "next_cursor": "newer-page-cursor",
                    "turn_model_metadata": {"t3": {"model_resolution_status": "resolved"}},
                },
            )

    assert [(entry.turn_id, entry.item_id) for entry in window._timeline_entries] == [
        ("t1", "old"),
        ("t2", "current"),
        ("t2", "latest"),
        ("t3", "newest"),
    ]
    assert len({(entry.turn_id, entry.item_id) for entry in window._timeline_entries}) == 4
    assert set(window._turn_model_metadata) == {"t1", "t3"}
    assert window._next_cursor is None
    window.close()


def test_main_window_resets_turn_model_metadata_when_selection_changes() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)

    window.select_thread("thread-a")
    client.result(
        "selection:1:items",
        {
            "items": [],
            "turn_model_metadata": {
                "turn-a": {
                    "model_candidates": [{"model": "gpt-5", "reasoning_effort": None}],
                    "model_resolution_status": "resolved",
                }
            },
        },
    )
    assert "turn-a" in window._turn_model_metadata

    window.select_thread("thread-b")

    assert window._turn_model_metadata == {}
    window.close()


def test_main_window_keeps_history_when_turn_model_metadata_is_unavailable() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)

    window.select_thread("thread-a")
    client.result(
        "selection:1:items",
        {
            "items": [
                {"turn_id": "turn-1", "item": _item("agent-1", "agentMessage", text="answer")}
            ],
            "turn_model_metadata": {
                "turn-1": {
                    "model_candidates": [],
                    "model_resolution_status": "unavailable",
                }
            },
        },
    )

    cards = [
        card
        for card in window.history_pane._content.findChildren(QFrame)
        if card.objectName() == "historyCard"
    ]
    assert len(cards) == 1
    assert any(body.toPlainText() == "answer" for body in cards[0].findChildren(QTextBrowser))
    assert "Turn · Model: unavailable" in {
        label.text() for label in window.history_pane._content.findChildren(QLabel)
    }
    window.close()


def test_activity_is_deduplicated_and_bounded() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)
    window.select_thread("thread-b")
    for index in range(201):
        client.activity(1, _activity(f"activity-{index}"))
    client.activity(1, _activity("activity-200"))

    assert window.activity_pane.activity_list.count() == 200
    assert "activity-0" not in window.activity_pane.activity_list.item(0).text()
    window.close()


def test_periodic_status_snapshot_merges_with_sse_activities_without_loss_or_duplicates() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)
    window.select_thread("thread-b")
    status_key = next(key for key, _, _ in client.requests if key.endswith(":status"))
    activity_a = _activity("activity-a")
    activity_b = _activity("activity-b")

    client.result(
        status_key,
        {"thread_id": "thread-b", "state": "running", "recent_activities": [activity_a]},
    )
    client.activity(1, activity_b)
    client.result(
        status_key,
        {"thread_id": "thread-b", "state": "running", "recent_activities": [activity_a]},
    )

    assert window.activity_pane.activity_list.count() == 2
    assert "activity-a" in window.activity_pane.activity_list.item(0).text()
    assert "activity-b" in window.activity_pane.activity_list.item(1).text()

    client.result(
        status_key,
        {
            "thread_id": "thread-b",
            "state": "running",
            "recent_activities": [activity_a, activity_b],
        },
    )

    assert window.activity_pane.activity_list.count() == 2
    window.close()
