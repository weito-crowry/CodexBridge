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
        },
    )
    widget = window.composer_widget
    assert widget.model_combo.itemText(0) == "Model: gpt-5"

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
        },
    )
    assert widget.model_combo.itemText(0) == "Model: gpt-5"
    assert "t1" in widget.model_combo.toolTip()
    assert "t2" in widget.model_combo.toolTip()

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
        },
    )
    assert widget.model_combo.itemText(0) == "Model: gpt-6"
    assert "t1" not in widget.model_combo.toolTip()
    window.close()
