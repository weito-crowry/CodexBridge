from __future__ import annotations

from codex_bridge.console.main_window import MainWindow
from tests.console_main_window_test_support import (
    FakeClient,
    _application,
    _config,
    _history_activity,
    _item,
    _wait_for_qt_timer,
)


def _entry(model: str, effort: str) -> dict[str, object]:
    return {
        "model_candidates": [{"model": model, "reasoning_effort": effort}],
        "model_resolution_status": "resolved",
    }


def test_history_summary_tracks_older_pages_live_refresh_and_reload() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)
    window.thread_pane.thread_selected.emit("thread-a")
    client.result(
        "selection:1:status",
        {"thread_id": "thread-a", "state": "not_loaded", "thread_metadata": None},
    )
    client.result("selection:1:turns", {"turns": []})
    client.result(
        "selection:1:items",
        {
            "items": [{"turn_id": "t2", "item": _item("new", "agentMessage", text="new")}],
            "next_cursor": "older",
            "turn_model_metadata": {"t2": _entry("gpt-5", "high")},
            "turn_access_metadata": {
                "t2": {
                    "access_candidates": ["danger-full-access"],
                    "access_resolution_status": "resolved",
                }
            },
        },
    )
    widget = window.composer_widget
    assert widget.model_combo.itemText(0) == "Model: gpt-5"
    assert widget.access_combo.currentText() == "Access: Full Access"
    assert "現在の実行権限を保証しません" in widget.access_combo.toolTip()

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
                "t1": _entry("gpt-5", "high"),
                "t2": _entry("gpt-5", "high"),
            },
            "turn_access_metadata": {
                "t1": {
                    "access_candidates": ["workspace-write"],
                    "access_resolution_status": "resolved",
                },
                "t2": {
                    "access_candidates": ["danger-full-access"],
                    "access_resolution_status": "resolved",
                },
            },
        },
    )
    assert widget.model_combo.itemText(0) == "Model: gpt-5"
    assert "t1" in widget.model_combo.toolTip()
    assert "t2" in widget.model_combo.toolTip()
    assert widget.access_combo.itemText(0) == "Access: Multiple"
    assert "t1" in widget.access_combo.toolTip()
    assert "t2" in widget.access_combo.toolTip()

    client.activity(
        window._selection_generation,
        _history_activity("agent_message", "new-message", "thread-a"),
    )
    _wait_for_qt_timer(300)
    history_requests = [request for request in client.requests if ":history:" in request[0]]
    assert history_requests
    for key, _, _ in history_requests:
        if key.endswith(":turns"):
            client.result(key, {"turns": []})
        elif key.endswith(":items"):
            client.result(
                key,
                {
                    "items": [
                        {"turn_id": "t3", "item": _item("latest", "agentMessage", text="latest")}
                    ],
                    "turn_model_metadata": {"t3": _entry("gpt-5.6", "high")},
                    "turn_access_metadata": {
                        "t3": {
                            "access_candidates": ["read-only"],
                            "access_resolution_status": "resolved",
                        }
                    },
                },
            )
    assert widget.model_combo.itemText(0) == "Model: Multiple"
    assert "t3" in widget.model_combo.toolTip()

    window.thread_pane.thread_selected.emit("thread-a")
    assert widget.model_combo.itemText(0) == "Model: Unknown"
    client.result(
        "selection:2:status",
        {"thread_id": "thread-a", "state": "not_loaded", "thread_metadata": None},
    )
    client.result("selection:2:turns", {"turns": []})
    client.result(
        "selection:2:items",
        {
            "items": [{"turn_id": "t4", "item": _item("latest", "agentMessage", text="fresh")}],
            "turn_model_metadata": {"t4": _entry("gpt-6", "low")},
            "turn_access_metadata": {
                "t4": {
                    "access_candidates": ["read-only"],
                    "access_resolution_status": "resolved",
                }
            },
        },
    )
    assert widget.model_combo.itemText(0) == "Model: gpt-6"
    assert widget.access_combo.itemText(0) == "Access: read-only"
    assert "t1" not in widget.access_combo.toolTip()
    assert "t1" not in widget.model_combo.toolTip()
    window.close()


def test_loaded_thread_uses_current_sandbox_metadata_instead_of_history() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)
    window.thread_pane.thread_selected.emit("thread-a")
    client.result(
        "selection:1:status",
        {"thread_id": "thread-a", "state": "not_loaded", "thread_metadata": None},
    )
    client.result("selection:1:turns", {"turns": []})
    client.result(
        "selection:1:items",
        {
            "items": [{"turn_id": "t1", "item": _item("old", "agentMessage", text="old")}],
            "turn_access_metadata": {
                "t1": {
                    "access_candidates": ["danger-full-access"],
                    "access_resolution_status": "resolved",
                }
            },
        },
    )
    widget = window.composer_widget
    assert widget.access_combo.currentText() == "Access: Full Access"
    assert "履歴から観測したsandbox設定" in widget.access_combo.toolTip()

    client.result(
        "selection:1:status",
        {
            "thread_id": "thread-a",
            "state": "completed",
            "thread_metadata": {"sandbox_mode": "workspace-write"},
        },
    )

    assert widget.access_combo.itemText(0) == "Access: workspace-write"
    assert widget.access_combo.toolTip() == ""
    window.close()
