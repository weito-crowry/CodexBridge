from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette

from codex_bridge.console import main_window as main_window_module
from codex_bridge.console.main_window import MainWindow
from tests.console_main_window_test_support import (
    FakeClient,
    FakeCodexProbe,
    FakeCodexUpdateProbe,
    Signal,
    StableTunnel,
    _application,
    _config,
)


def _window(client: FakeClient, *, external_thread_monitor=None) -> MainWindow:
    return MainWindow(
        _config(),
        api_client=client,
        codex_probe=FakeCodexProbe(),
        codex_update_probe=FakeCodexUpdateProbe(),
        tunnel_supervisor=StableTunnel(),
        tray_available=False,
        external_thread_monitor=external_thread_monitor,
    )


class _FakeExternalThreadMonitor:
    def __init__(self) -> None:
        self.updated = Signal()
        self.scan_requests = 0
        self.stop_calls = 0

    def request_scan(self) -> None:
        self.scan_requests += 1

    def stop(self) -> None:
        self.stop_calls += 1


def test_main_window_json_methods_delegate_to_dispatcher(monkeypatch) -> None:
    _application()
    client = FakeClient()
    window = _window(client)
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        window._api_result_dispatcher,
        "apply_result",
        lambda key, payload: calls.append(("result", key, payload)),
    )
    monkeypatch.setattr(
        window._api_result_dispatcher,
        "apply_error",
        lambda key, message: calls.append(("error", key, message)),
    )

    window.apply_json_result("threads", {"threads": []})
    window._apply_json_error("health", "offline")

    assert calls == [
        ("result", "threads", {"threads": []}),
        ("error", "health", "offline"),
    ]
    window.close()


def test_dispatcher_uses_main_window_project_name_callback(monkeypatch, tmp_path) -> None:
    _application()
    client = FakeClient()
    window = _window(client)
    calls: list[None] = []
    cwd = str(tmp_path / "workspace")
    key = os.path.normcase(os.path.normpath(cwd))
    monkeypatch.setattr(
        main_window_module,
        "read_local_project_names",
        lambda: calls.append(None) or {key: "Project"},
    )

    client.result("threads", {"threads": [{"id": "thread-a", "cwd": cwd}]})

    assert calls == [None]
    assert window.thread_pane.list_widget.topLevelItem(0).text(0) == "Project — workspace"
    window.close()


def test_dispatcher_updates_main_window_runtime_thread_state() -> None:
    _application()
    client = FakeClient()
    window = _window(client)

    window._api_result_dispatcher.apply_result(
        "threads",
        {
            "threads": [
                {
                    "id": "thread-a",
                    "runtime_status": {"type": "active", "active_flags": ["waitingOnApproval"]},
                }
            ]
        },
    )

    assert window._thread_turn_statuses == {"thread-a": "needs_approval"}
    assert window._active_thread_ids == {"thread-a"}
    window.close()


def test_external_running_thread_survives_http_thread_refresh() -> None:
    _application()
    client = FakeClient()
    window = _window(client)
    cwd = os.getcwd()
    window._apply_external_running_threads(
        {
            "outside-api-limit": {"id": "outside-api-limit", "cwd": cwd},
            "external-not-loaded": {
                "id": "external-not-loaded",
                "cwd": cwd,
                "originator": "codex_desktop",
            },
            "external-interrupted": {
                "id": "external-interrupted",
                "cwd": cwd,
                "originator": "codex_desktop",
            },
        }
    )

    client.result(
        "threads",
        {
            "threads": [
                {
                    "id": "managed",
                    "cwd": cwd,
                    "runtime_status": {"type": "active", "active_flags": []},
                },
                {
                    "id": "external-not-loaded",
                    "cwd": cwd,
                    "runtime_status": {"type": "notLoaded"},
                },
                {
                    "id": "external-interrupted",
                    "cwd": cwd,
                    "runtime_status": {"type": "interrupted"},
                },
            ]
        },
    )

    thread_items = [
        item
        for index in range(window.thread_pane.list_widget.topLevelItemCount())
        for item in _children(window.thread_pane.list_widget.topLevelItem(index))
        if isinstance(item.data(0, Qt.ItemDataRole.UserRole), str)
    ]
    assert any(
        item.data(0, Qt.ItemDataRole.UserRole) == "outside-api-limit" for item in thread_items
    )
    assert any(item.data(0, Qt.ItemDataRole.UserRole) == "managed" for item in thread_items)
    external_item = next(
        item
        for item in thread_items
        if item.data(0, Qt.ItemDataRole.UserRole) == "external-not-loaded"
    )
    managed_item = next(
        item for item in thread_items if item.data(0, Qt.ItemDataRole.UserRole) == "managed"
    )
    assert external_item.toolTip(0) == "external-not-loaded\n推定実行中"
    interrupted_item = next(
        item
        for item in thread_items
        if item.data(0, Qt.ItemDataRole.UserRole) == "external-interrupted"
    )
    assert interrupted_item.toolTip(0) == "external-interrupted\n推定実行中"
    assert managed_item.foreground(0).color() == window.thread_pane.list_widget.palette().color(
        QPalette.ColorRole.Link
    )
    assert "external-not-loaded" in window._external_running_threads
    assert "external-interrupted" in window._external_running_threads
    window.close()


def test_http_codexbridge_originator_suppresses_external_overlay_but_keeps_statuses() -> None:
    _application()
    client = FakeClient()
    window = _window(client)
    cwd = os.getcwd()
    managed_ids = ("managed-running", "managed-approval", "managed-input", "managed-waiting")
    window._apply_external_running_threads(
        {thread_id: {"id": thread_id, "cwd": cwd} for thread_id in managed_ids}
    )

    client.result(
        "threads",
        {
            "threads": [
                {
                    "id": "managed-running",
                    "name": "Running",
                    "cwd": cwd,
                    "originator": "codexbridge",
                    "runtime_status": {"type": "active", "active_flags": []},
                },
                {
                    "id": "managed-approval",
                    "name": "Approval",
                    "cwd": cwd,
                    "originator": " CodexBridge ",
                    "runtime_status": {
                        "type": "active",
                        "active_flags": ["waitingOnApproval"],
                    },
                },
                {
                    "id": "managed-input",
                    "name": "Input",
                    "cwd": cwd,
                    "originator": "CODEXBRIDGE",
                    "runtime_status": {
                        "type": "active",
                        "active_flags": ["waitingOnUserInput"],
                    },
                },
                {
                    "id": "managed-waiting",
                    "name": "Waiting",
                    "cwd": cwd,
                    "originator": "codexbridge",
                    "runtime_status": {"type": "active", "active_flags": ["other"]},
                },
            ]
        },
    )

    by_id = {
        item.data(0, Qt.ItemDataRole.UserRole): item
        for index in range(window.thread_pane.list_widget.topLevelItemCount())
        for item in _children(window.thread_pane.list_widget.topLevelItem(index))
        if isinstance(item.data(0, Qt.ItemDataRole.UserRole), str)
    }
    assert by_id["managed-running"].text(0) == "\u25cf Running"
    assert window._thread_turn_statuses == {
        "managed-running": "in_progress",
        "managed-approval": "needs_approval",
        "managed-input": "needs_input",
        "managed-waiting": "waiting",
    }
    assert by_id["managed-running"].foreground(
        0
    ).color() == window.thread_pane.list_widget.palette().color(QPalette.ColorRole.Link)
    assert by_id["managed-approval"].text(0) == "\u23f8 Approval"
    assert by_id["managed-input"].text(0) == "\u270e Input"
    assert by_id["managed-waiting"].text(0) == "\u23f8 Waiting"
    for thread_id in managed_ids:
        assert by_id[thread_id].icon(0).isNull()
        assert by_id[thread_id].toolTip(0) == thread_id
        assert by_id[thread_id].foreground(0).color() != QColor("#d93025")
    running_section = window.thread_pane.list_widget.topLevelItem(0)
    assert running_section.text(0) == "現在処理中"
    assert [
        item.data(0, Qt.ItemDataRole.UserRole)
        for item in _children(running_section)
        if item.data(0, Qt.ItemDataRole.UserRole) is not None
    ] == ["managed-running"]
    assert set(window._external_running_threads) == set(managed_ids)
    window.close()


def test_unchanged_external_thread_snapshots_do_not_rerender_the_tree(monkeypatch) -> None:
    _application()
    client = FakeClient()
    window = _window(client)
    cwd = os.getcwd()
    first_id = "external-one"
    first = {first_id: {"id": first_id, "cwd": cwd, "originator": "codex_desktop"}}
    window._apply_external_running_threads(first)

    render_calls: list[None] = []
    original_render = window.thread_pane._render

    def track_render() -> None:
        render_calls.append(None)
        original_render()

    monkeypatch.setattr(window.thread_pane, "_render", track_render)
    window._apply_external_running_threads(first)
    assert render_calls == []

    second_id = "external-two"
    window._apply_external_running_threads(
        {
            **first,
            second_id: {"id": second_id, "cwd": cwd, "originator": "codex_desktop"},
        }
    )
    assert len(render_calls) == 1
    assert window.thread_pane.thread_count == 2
    window.close()


def test_empty_http_thread_refresh_keeps_external_running_thread_visible() -> None:
    _application()
    client = FakeClient()
    window = _window(client)
    thread_id = "external-outside-api"
    window._apply_external_running_threads({thread_id: {"id": thread_id, "cwd": os.getcwd()}})

    client.result("threads", {"threads": []})

    assert window.thread_pane.thread_count == 1
    visible_items = [
        item
        for index in range(window.thread_pane.list_widget.topLevelItemCount())
        for item in _children(window.thread_pane.list_widget.topLevelItem(index))
        if item.data(0, Qt.ItemDataRole.UserRole) == thread_id
    ]
    assert len(visible_items) == 2
    assert all(item.toolTip(0) == f"{thread_id}\n推定実行中" for item in visible_items)
    window.close()


def test_main_window_applies_external_monitor_updates_and_stops_on_exit() -> None:
    _application()
    client = FakeClient()
    monitor = _FakeExternalThreadMonitor()
    window = _window(client, external_thread_monitor=monitor)
    thread_id = "external-thread"
    monitor.updated.emit(
        {thread_id: {"id": thread_id, "cwd": os.getcwd(), "originator": "codex_desktop"}}
    )

    assert monitor.scan_requests == 1
    assert window.thread_pane.thread_count == 1
    external_item = next(
        item
        for item in _children(window.thread_pane.list_widget.topLevelItem(0))
        if item.data(0, Qt.ItemDataRole.UserRole) == thread_id
    )
    assert external_item.text(0) == thread_id
    assert not external_item.icon(0).isNull()
    assert external_item.icon(0).pixmap(12, 12).toImage().pixelColor(6, 6) == QColor("#d93025")
    assert external_item.toolTip(0) == f"{thread_id}\n推定実行中"
    window.thread_pane.thread_selected.emit(thread_id)
    assert window._selected_thread_id == thread_id
    assert any(path == f"/ui-api/threads/{thread_id}" for _key, path, _query in client.requests)

    window._begin_exit()

    assert monitor.stop_calls >= 1
    assert not window.external_thread_timer.isActive()
    window.close()


def _children(item):
    result = []
    for index in range(item.childCount()):
        child = item.child(index)
        result.append(child)
        result.extend(_children(child))
    return result


def test_api_json_signals_keep_single_main_window_compatibility_slot() -> None:
    _application()
    client = FakeClient()
    window = _window(client)

    dispatcher = window._api_result_dispatcher
    succeeded_slots = [
        slot for slot in client.json_succeeded._slots if getattr(slot, "__self__", None) is window
    ]
    failed_slots = [
        slot for slot in client.json_failed._slots if getattr(slot, "__self__", None) is window
    ]
    assert len(succeeded_slots) == 1
    assert succeeded_slots[0].__func__ is MainWindow.apply_json_result
    assert len(failed_slots) == 1
    assert failed_slots[0].__func__ is MainWindow._apply_json_error
    assert all(
        getattr(slot, "__self__", None) is not dispatcher for slot in client.json_succeeded._slots
    )
    assert all(
        getattr(slot, "__self__", None) is not dispatcher for slot in client.json_failed._slots
    )
    window.close()
