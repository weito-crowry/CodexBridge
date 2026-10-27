from __future__ import annotations

import os

import pytest
from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QTreeWidgetItem

from codex_bridge.console.thread_list_pane import ThreadListPane as DirectThreadListPane
from codex_bridge.console.widgets import ThreadListPane


def _thread_items(pane: ThreadListPane) -> list[QTreeWidgetItem]:
    result: list[QTreeWidgetItem] = []

    def visit(item: QTreeWidgetItem) -> None:
        if isinstance(item.data(0, Qt.ItemDataRole.UserRole), str):
            result.append(item)
        for child_index in range(item.childCount()):
            visit(item.child(child_index))

    for index in range(pane.list_widget.topLevelItemCount()):
        visit(pane.list_widget.topLevelItem(index))
    return result


def _thread_item(pane: ThreadListPane, thread_id: str) -> QTreeWidgetItem:
    return next(
        item for item in _thread_items(pane) if item.data(0, Qt.ItemDataRole.UserRole) == thread_id
    )


def _icon_pixels(item: QTreeWidgetItem) -> tuple[int, ...]:
    image = item.icon(0).pixmap(16, 16).toImage()
    return tuple(
        image.pixelColor(x, y).rgba() for y in range(image.height()) for x in range(image.width())
    )


def _icon_contains_color(item: QTreeWidgetItem, color: QColor) -> bool:
    return color.rgba() in _icon_pixels(item)


def test_thread_list_import_paths_preserve_class_widget_and_signals() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    assert ThreadListPane is DirectThreadListPane

    compatibility_pane = ThreadListPane()
    direct_pane = DirectThreadListPane()
    signal_names = (
        "refresh_requested",
        "bulk_thread_naming_requested",
        "thread_selected",
        "thread_rename_requested",
        "thread_open_requested",
        "thread_copy_requested",
        "new_thread_for_folder_requested",
        "folder_explorer_requested",
        "folder_terminal_requested",
    )

    assert compatibility_pane.list_widget.objectName() == "threadList"
    assert direct_pane.list_widget.objectName() == "threadList"
    assert all(hasattr(compatibility_pane, name) for name in signal_names)
    assert all(hasattr(direct_pane, name) for name in signal_names)


def test_thread_list_uses_names_only_and_preserves_thread_identity() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = ThreadListPane()

    pane.set_threads(
        [
            {"id": "named", "name": "Named thread", "preview": "Preview text"},
            {"id": "missing", "preview": "Preview text"},
            {"id": "empty", "name": "", "preview": "Preview text"},
            {"id": "invalid", "name": 42, "preview": "Preview text"},
        ]
    )

    assert [item.text(0) for item in _thread_items(pane)] == [
        "Named thread",
        "New スレッド",
        "New スレッド",
        "New スレッド",
    ]
    assert all("Preview text" not in item.text(0) for item in _thread_items(pane))
    for index, thread_id in enumerate(("named", "missing", "empty", "invalid")):
        item = _thread_items(pane)[index]
        assert item.data(0, Qt.ItemDataRole.UserRole) == thread_id
        assert item.toolTip(0) == thread_id


def test_thread_list_marks_active_threads_with_palette_color_and_bold_font() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = ThreadListPane()

    pane.set_threads(
        [{"id": "active", "name": "Active"}, {"id": "idle", "name": "Idle"}],
        active_thread_ids={"active"},
    )

    active = _thread_item(pane, "active")
    idle = _thread_item(pane, "idle")
    assert active.font(0).weight() > idle.font(0).weight()
    assert active.foreground(0).color() == pane.list_widget.palette().color(QPalette.ColorRole.Link)
    assert active.foreground(0).color() != idle.foreground(0).color()


def test_thread_list_distinguishes_observed_turn_states_without_promoting_waits_to_running() -> (
    None
):
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = ThreadListPane()

    pane.set_threads(
        [
            {"id": "running", "name": "Running"},
            {"id": "approval", "name": "Approval"},
            {"id": "input", "name": "Input"},
            {"id": "locked", "name": "Locked"},
            {"id": "stopping", "name": "Stopping"},
        ],
        thread_statuses={
            "running": "in_progress",
            "approval": "needs_approval",
            "input": "needs_input",
            "locked": "locked",
            "stopping": "stopping",
        },
    )

    running = _thread_item(pane, "running")
    assert running.text(0) == "Running"
    assert not running.icon(0).isNull()
    assert _icon_contains_color(running, pane.list_widget.palette().color(QPalette.ColorRole.Link))
    assert _thread_item(pane, "approval").text(0) == "\u23f8 Approval"
    assert _thread_item(pane, "input").text(0) == "\u270e Input"
    assert _thread_item(pane, "locked").text(0) == "Locked"
    assert _thread_item(pane, "stopping").text(0) == "Stopping"


def test_running_spinners_share_color_phase_and_update_without_rerender(
    tmp_path, monkeypatch
) -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    cwd = str(tmp_path)
    pane = ThreadListPane()
    pane.set_threads(
        [
            {"id": "managed", "name": "Managed", "cwd": cwd},
            {"id": "external", "name": "External", "cwd": cwd},
        ],
        thread_statuses={"managed": "in_progress"},
    )
    pane.set_external_running_threads({"external": {"id": "external", "cwd": cwd}})
    items_by_id = {
        thread_id: [
            item
            for item in _thread_items(pane)
            if item.data(0, Qt.ItemDataRole.UserRole) == thread_id
        ]
        for thread_id in ("managed", "external")
    }
    managed_color = pane.list_widget.palette().color(QPalette.ColorRole.Link)
    external_color = QColor("#d93025")

    for thread_id, title, color in (
        ("managed", "Managed", managed_color),
        ("external", "External", external_color),
    ):
        assert len(items_by_id[thread_id]) == 2
        for item in items_by_id[thread_id]:
            assert item.text(0) == title
            assert not item.icon(0).isNull()
            assert item.foreground(0).color() == color
            assert _icon_contains_color(item, color)
    assert _icon_pixels(items_by_id["managed"][0]) == _icon_pixels(items_by_id["managed"][1])
    assert _icon_pixels(items_by_id["external"][0]) == _icon_pixels(items_by_id["external"][1])

    pane.show()
    assert pane._spinner_timer.isActive()
    assert pane._spinner_timer.interval() == 90
    assert [child for child in pane.children() if isinstance(child, QTimer)] == [
        pane._spinner_timer
    ]
    selected = items_by_id["external"][1]
    pane.list_widget.setCurrentItem(selected)
    render_calls: list[None] = []
    original_render = pane._render

    def track_render() -> None:
        render_calls.append(None)
        original_render()

    monkeypatch.setattr(pane, "_render", track_render)
    previous_managed = _icon_pixels(items_by_id["managed"][0])
    previous_external = _icon_pixels(items_by_id["external"][0])
    pane._advance_running_spinner()
    after_one_managed = _icon_pixels(items_by_id["managed"][0])
    after_one_external = _icon_pixels(items_by_id["external"][0])
    pane._advance_running_spinner()
    after_two_managed = _icon_pixels(items_by_id["managed"][0])
    after_two_external = _icon_pixels(items_by_id["external"][0])
    assert previous_managed != after_one_managed != after_two_managed
    assert previous_external != after_one_external != after_two_external
    assert _icon_pixels(items_by_id["managed"][1]) == after_two_managed
    assert _icon_pixels(items_by_id["external"][1]) == after_two_external
    assert _icon_contains_color(items_by_id["managed"][0], managed_color)
    assert _icon_contains_color(items_by_id["external"][0], external_color)
    assert pane.list_widget.currentItem() is selected
    assert render_calls == []

    updated_palette = pane.list_widget.palette()
    updated_link_color = QColor("#315f9f")
    updated_palette.setColor(QPalette.ColorRole.Link, updated_link_color)
    pane.list_widget.setPalette(updated_palette)
    pane._advance_running_spinner()
    assert items_by_id["managed"][0].foreground(0).color() == updated_link_color
    assert _icon_contains_color(items_by_id["managed"][0], updated_link_color)
    assert items_by_id["external"][0].foreground(0).color() == external_color
    assert render_calls == []

    pane.hide()
    assert not pane._spinner_timer.isActive()
    hidden_phase = pane._spinner_angle
    hidden_frame = _icon_pixels(items_by_id["managed"][0])
    pane._advance_running_spinner()
    assert pane._spinner_angle == hidden_phase
    assert _icon_pixels(items_by_id["managed"][0]) == hidden_frame
    pane.show()
    assert pane._spinner_timer.isActive()

    pane.set_threads([{"id": "idle", "name": "Idle", "cwd": cwd}], thread_statuses={})
    render_count_before_tick = len(render_calls)
    pane._advance_running_spinner()
    assert len(render_calls) == render_count_before_tick
    pane.set_external_running_threads({})
    assert not pane._spinner_timer.isActive()
    pane.close()
    assert not pane._spinner_timer.isActive()
    destroyed: list[None] = []
    pane.destroyed.connect(lambda *_args: destroyed.append(None))
    pane.deleteLater()
    application.sendPostedEvents(pane, QEvent.Type.DeferredDelete)
    assert destroyed == [None]


def test_running_spinner_falls_back_to_a_colored_dot_when_icon_rendering_fails(
    tmp_path, monkeypatch
) -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    cwd = str(tmp_path)
    pane = ThreadListPane()
    monkeypatch.setattr(pane, "_running_spinner_icon", lambda _color, _angle: None)

    pane.set_threads(
        [
            {"id": "managed", "name": "Managed", "cwd": cwd},
            {"id": "external", "name": "External", "cwd": cwd},
        ],
        thread_statuses={"managed": "in_progress"},
    )
    pane.set_external_running_threads({"external": {"id": "external", "cwd": cwd}})

    for thread_id, title, color in (
        ("managed", "Managed", pane.list_widget.palette().color(QPalette.ColorRole.Link)),
        ("external", "External", QColor("#d93025")),
    ):
        item = _thread_item(pane, thread_id)
        assert item.text(0) == f"\u25cf {title}"
        assert item.icon(0).isNull()
        assert item.foreground(0).color() == color
    assert not pane._spinner_timer.isActive()


def test_managed_runtime_statuses_override_external_estimates_without_originator(tmp_path) -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    cwd = str(tmp_path)
    managed_statuses = {
        "managed-running": "in_progress",
        "managed-approval": "needs_approval",
        "managed-input": "needs_input",
        "managed-user-input": "needs_user_input",
        "managed-waiting": "waiting",
    }
    titles = {
        "managed-running": "Running",
        "managed-approval": "Approval",
        "managed-input": "Input",
        "managed-user-input": "User input",
        "managed-waiting": "Waiting",
        "not-loaded": "Not loaded",
    }
    threads = [{"id": thread_id, "name": title, "cwd": cwd} for thread_id, title in titles.items()]
    threads[1]["originator"] = ""
    threads[3]["originator"] = ""
    threads[5]["runtime_status"] = {"type": "notLoaded"}
    pane = ThreadListPane()

    pane.set_threads(threads, thread_statuses=managed_statuses)
    pane.set_external_running_threads(
        {thread_id: {"id": thread_id, "cwd": cwd} for thread_id in titles}
    )

    assert pane._thread_statuses == managed_statuses
    expected_text = {
        "managed-running": "Running",
        "managed-approval": "\u23f8 Approval",
        "managed-input": "\u270e Input",
        "managed-user-input": "\u270e User input",
        "managed-waiting": "\u23f8 Waiting",
    }
    for thread_id, text in expected_text.items():
        item = _thread_item(pane, thread_id)
        assert item.text(0) == text
        if thread_id == "managed-running":
            assert not item.icon(0).isNull()
            assert _icon_contains_color(
                item, pane.list_widget.palette().color(QPalette.ColorRole.Link)
            )
        else:
            assert item.icon(0).isNull()
        assert item.toolTip(0) == thread_id
        assert item.foreground(0).color() == pane.list_widget.palette().color(
            QPalette.ColorRole.Link
        )

    running_section = pane.list_widget.topLevelItem(0)
    assert running_section.text(0) == "現在処理中"
    running_ids: list[str] = []

    def collect_running_ids(item: QTreeWidgetItem) -> None:
        thread_id = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(thread_id, str):
            running_ids.append(thread_id)
        for child_index in range(item.childCount()):
            collect_running_ids(item.child(child_index))

    collect_running_ids(running_section)
    assert running_ids == ["managed-running", "not-loaded"]

    not_loaded = _thread_item(pane, "not-loaded")
    assert _icon_contains_color(not_loaded, QColor("#d93025"))
    assert not_loaded.toolTip(0) == "not-loaded\n推定実行中"


def test_external_running_threads_overlay_without_replacing_managed_statuses(tmp_path) -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    first_cwd = tmp_path / "project-a"
    second_cwd = tmp_path / "project-b"
    first_cwd.mkdir()
    second_cwd.mkdir()
    pane = ThreadListPane()

    pane.set_threads(
        [
            {"id": "managed", "name": "Managed", "cwd": str(first_cwd)},
            {"id": "shared", "name": "Shared", "cwd": str(first_cwd)},
            {
                "id": "waiting",
                "name": "Waiting",
                "cwd": str(first_cwd),
                "originator": " CoDeXBrIdGe ",
            },
            {"id": "idle", "name": "Idle", "cwd": str(first_cwd)},
        ],
        thread_statuses={"managed": "in_progress", "waiting": "needs_approval"},
    )
    pane.set_external_running_threads(
        {
            "managed": {"cwd": str(first_cwd)},
            "shared": {"cwd": str(first_cwd)},
            "waiting": {"cwd": str(first_cwd)},
            "outside-api-limit": {"cwd": str(second_cwd)},
        }
    )

    managed = _thread_item(pane, "managed")
    external = _thread_item(pane, "shared")
    omitted = _thread_item(pane, "outside-api-limit")
    waiting = _thread_item(pane, "waiting")
    idle = _thread_item(pane, "idle")
    running = pane.list_widget.topLevelItem(0)
    assert running.text(0) == "現在処理中"
    assert pane.thread_count == 5
    assert managed.text(0) == "Managed"
    assert not managed.icon(0).isNull()
    assert managed.foreground(0).color() == pane.list_widget.palette().color(
        QPalette.ColorRole.Link
    )
    assert external.text(0) == "Shared"
    assert not external.icon(0).isNull()
    assert _icon_contains_color(external, QColor("#d93025"))
    assert external.foreground(0).color() == QColor("#d93025")
    assert external.toolTip(0) == "shared\n推定実行中"
    for external_copy in [
        item for item in _thread_items(pane) if item.data(0, Qt.ItemDataRole.UserRole) == "shared"
    ]:
        assert external_copy.text(0) == "Shared"
        assert not external_copy.icon(0).isNull()
        assert _icon_contains_color(external_copy, QColor("#d93025"))
        assert external_copy.foreground(0).color() == QColor("#d93025")
        assert external_copy.toolTip(0) == "shared\n推定実行中"
    assert omitted.text(0) == "outside-api-limit"
    assert not omitted.icon(0).isNull()
    assert _icon_contains_color(omitted, QColor("#d93025"))
    assert omitted.foreground(0).color() == QColor("#d93025")
    assert idle.text(0) == "Idle"
    assert idle.icon(0).isNull()
    assert waiting.text(0) == "\u23f8 Waiting"
    assert waiting.icon(0).isNull()
    assert waiting.foreground(0).color() == pane.list_widget.palette().color(
        QPalette.ColorRole.Link
    )
    assert waiting.toolTip(0) == "waiting"
    assert pane._thread_statuses["waiting"] == "needs_approval"
    assert (
        sum(item.data(0, Qt.ItemDataRole.UserRole) == "shared" for item in _thread_items(pane)) == 2
    )
    assert (
        sum(
            item.data(0, Qt.ItemDataRole.UserRole) == "outside-api-limit"
            for item in _thread_items(pane)
        )
        == 2
    )

    pane.set_external_running_threads({})

    assert pane.thread_count == 4
    assert _thread_item(pane, "shared").text(0) == "Shared"
    assert (
        sum(item.data(0, Qt.ItemDataRole.UserRole) == "shared" for item in _thread_items(pane)) == 1
    )
    assert all(
        item.data(0, Qt.ItemDataRole.UserRole) != "outside-api-limit"
        for item in _thread_items(pane)
    )


def test_thread_status_refresh_preserves_selection_groups_expansion_rows_and_scroll(
    tmp_path,
) -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    first_cwd = str(tmp_path / "first-project")
    second_cwd = str(tmp_path / "second-project")
    threads = [
        *({"id": f"first-{index}", "cwd": first_cwd} for index in range(20)),
        *({"id": f"second-{index}", "cwd": second_cwd} for index in range(20)),
    ]
    pane = ThreadListPane()
    pane.resize(320, 240)
    pane.show()
    pane.set_threads(threads)
    application.processEvents()
    pane.list_widget.topLevelItem(0).setExpanded(False)
    pane.list_widget.setCurrentItem(_thread_item(pane, "second-10"))
    scroll = pane.list_widget.verticalScrollBar()
    scroll.setValue(scroll.maximum())
    previous_scroll = scroll.value()
    previous_group_labels = [
        pane.list_widget.topLevelItem(index).text(0)
        for index in range(pane.list_widget.topLevelItemCount())
    ]

    pane.set_thread_statuses({"second-10": "needs_approval"})
    application.processEvents()

    current = pane.list_widget.currentItem()
    assert current is not None
    assert current.data(0, Qt.ItemDataRole.UserRole) == "second-10"
    assert [
        pane.list_widget.topLevelItem(index).text(0)
        for index in range(pane.list_widget.topLevelItemCount())
    ] == previous_group_labels
    assert not pane.list_widget.topLevelItem(0).isExpanded()
    assert pane.list_widget.topLevelItem(1).childCount() == 20
    assert scroll.value() == previous_scroll
    assert _thread_item(pane, "second-10").text(0) == "\u23f8 New スレッド"
    pane.close()


def test_thread_list_keeps_running_indicator_through_selection_and_state_changes() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = ThreadListPane()
    threads = [
        {"id": "active", "name": "Active"},
        {"id": "idle", "name": "Idle"},
    ]

    pane.set_threads(threads, active_thread_ids={"active"})

    active = _thread_item(pane, "active")
    assert active.text(0) == "Active"
    assert not active.icon(0).isNull()
    assert _thread_item(pane, "idle").text(0) == "Idle"
    pane.list_widget.setCurrentItem(_thread_item(pane, "active"))
    selected = pane.list_widget.currentItem()
    assert selected is not None
    assert selected.text(0) == "Active"
    assert not selected.icon(0).isNull()
    assert selected.data(0, Qt.ItemDataRole.UserRole) == "active"
    pane.set_active_thread_ids(set())

    selected = pane.list_widget.currentItem()
    assert selected is not None
    assert selected.text(0) == "Active"
    assert selected.data(0, Qt.ItemDataRole.UserRole) == "active"


def test_running_threads_are_aggregated_by_project_and_remain_in_project_lists(
    tmp_path,
) -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    first_cwd = str(tmp_path / "first-project")
    second_cwd = str(tmp_path / "second-project")
    threads = [
        {"id": "first-a", "name": "First A", "cwd": first_cwd},
        {"id": "first-b", "name": "First B", "cwd": first_cwd},
        {"id": "second", "name": "Second", "cwd": second_cwd},
        {"id": "approval", "name": "Approval", "cwd": first_cwd},
        {"id": "input", "name": "Input", "cwd": second_cwd},
        {"id": "locked", "name": "Locked", "cwd": second_cwd},
        {"id": "stopped", "name": "Stopped", "cwd": second_cwd},
        {"id": "paused", "name": "Paused", "cwd": second_cwd},
    ]
    pane = ThreadListPane()
    pane.set_threads(
        threads,
        thread_statuses={
            "first-a": "in_progress",
            "first-b": "in_progress",
            "second": "in_progress",
            "approval": "needs_approval",
            "input": "needs_input",
            "locked": "locked",
            "stopped": "stopped",
            "paused": "paused",
        },
    )

    running = pane.list_widget.topLevelItem(0)
    assert running.text(0) == "現在処理中"
    assert [running.child(index).text(0) for index in range(running.childCount())] == [
        "first-project",
        "second-project",
    ]
    first_running = running.child(0)
    second_running = running.child(1)
    assert [
        first_running.child(index).data(0, Qt.ItemDataRole.UserRole)
        for index in range(first_running.childCount())
    ] == ["first-a", "first-b"]
    assert second_running.child(0).data(0, Qt.ItemDataRole.UserRole) == "second"
    assert {item.data(0, Qt.ItemDataRole.UserRole) for item in _thread_items(pane)} == {
        "first-a",
        "first-b",
        "second",
        "approval",
        "input",
        "locked",
        "stopped",
        "paused",
    }
    assert (
        sum(item.data(0, Qt.ItemDataRole.UserRole) == "first-a" for item in _thread_items(pane))
        == 2
    )


def test_running_copy_selection_is_preserved_and_terminal_state_removes_only_aggregate_copy(
    tmp_path,
) -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    cwd = str(tmp_path / "project")
    pane = ThreadListPane()
    threads = [{"id": "running", "name": "Running", "cwd": cwd}]
    pane.set_threads(threads, thread_statuses={"running": "in_progress"})
    running_section = pane.list_widget.topLevelItem(0)
    running_item = running_section.child(0).child(0)
    project_item = pane.list_widget.topLevelItem(1).child(0)
    selected: list[str] = []
    pane.thread_selected.connect(selected.append)

    pane.list_widget.setCurrentItem(running_item)
    pane.list_widget.itemClicked.emit(running_item, 0)
    assert selected == ["running"]
    assert pane.list_widget.currentItem().data(0, Qt.ItemDataRole.UserRole) == "running"
    assert pane.list_widget.currentItem().parent().parent().text(0) == "現在処理中"

    pane.list_widget.setCurrentItem(project_item)
    pane.list_widget.itemClicked.emit(project_item, 0)
    assert selected == ["running", "running"]
    pane.set_thread_statuses({"running": "in_progress"})
    current = pane.list_widget.currentItem()
    assert current is not None
    assert current.data(0, Qt.ItemDataRole.UserRole) == "running"
    assert current.parent().text(0) == "project"

    pane.list_widget.setCurrentItem(pane.list_widget.topLevelItem(0).child(0).child(0))
    pane.set_thread_statuses({"running": "failed"})
    assert pane.list_widget.topLevelItemCount() == 1
    remaining = pane.list_widget.topLevelItem(0).child(0)
    assert remaining.data(0, Qt.ItemDataRole.UserRole) == "running"
    assert pane.list_widget.currentItem() is remaining


def test_running_aggregate_refresh_preserves_project_expansion_selection_and_scroll(
    tmp_path,
) -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    first_cwd = str(tmp_path / "first-project")
    second_cwd = str(tmp_path / "second-project")
    threads = [
        *({"id": f"first-{index}", "cwd": first_cwd} for index in range(20)),
        *({"id": f"second-{index}", "cwd": second_cwd} for index in range(20)),
    ]
    pane = ThreadListPane()
    pane.resize(320, 240)
    pane.show()
    pane.set_threads(threads)
    application.processEvents()
    pane.list_widget.topLevelItem(0).setExpanded(False)
    pane.list_widget.setCurrentItem(_thread_item(pane, "second-10"))
    scroll = pane.list_widget.verticalScrollBar()
    scroll.setValue(scroll.maximum())
    previous_scroll = scroll.value()

    pane.set_external_running_threads({"second-10": {"cwd": second_cwd}})
    application.processEvents()

    current = pane.list_widget.currentItem()
    assert current is not None
    assert current.data(0, Qt.ItemDataRole.UserRole) == "second-10"
    assert current.parent().text(0) == "second-project"
    assert not pane.list_widget.topLevelItem(1).isExpanded()
    assert scroll.value() == previous_scroll
    pane.filter_edit.setText("second-10")
    assert any(
        item.data(0, Qt.ItemDataRole.UserRole) == "second-10" for item in _thread_items(pane)
    )
    pane.filter_edit.clear()
    assert not pane.list_widget.topLevelItem(1).isExpanded()
    pane.close()


def test_running_project_collapse_survives_status_refresh_and_other_project_changes(
    tmp_path,
) -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    first_cwd = str(tmp_path / "first-project")
    second_cwd = str(tmp_path / "second-project")
    pane = ThreadListPane()
    pane.set_threads(
        [
            {"id": "first-a", "cwd": first_cwd},
            {"id": "first-b", "cwd": first_cwd},
            {"id": "second-a", "cwd": second_cwd},
            {"id": "second-b", "cwd": second_cwd},
        ],
        thread_statuses={
            "first-a": "in_progress",
            "first-b": "in_progress",
            "second-a": "in_progress",
            "second-b": "in_progress",
        },
    )
    running_section = pane.list_widget.topLevelItem(0)
    first_running = running_section.child(0)
    second_running = running_section.child(1)
    assert running_section.text(0) == "現在処理中"
    assert first_running.isExpanded()
    assert second_running.isExpanded()

    first_running.setExpanded(False)
    pane.set_thread_statuses(
        {
            "first-a": "in_progress",
            "first-b": "in_progress",
            "second-a": "in_progress",
            "second-b": "needs_input",
        }
    )

    running_section = pane.list_widget.topLevelItem(0)
    assert running_section.isExpanded()
    assert not running_section.child(0).isExpanded()
    assert running_section.child(1).isExpanded()


def test_running_and_regular_project_expansion_states_are_independent(tmp_path) -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    cwd = str(tmp_path / "project")
    pane = ThreadListPane()
    pane.set_threads(
        [
            {"id": "first", "cwd": cwd},
            {"id": "second", "cwd": cwd},
        ],
        thread_statuses={"first": "in_progress", "second": "in_progress"},
    )

    running_section = pane.list_widget.topLevelItem(0)
    running_project = running_section.child(0)
    regular_project = pane.list_widget.topLevelItem(1)
    running_project.setExpanded(False)
    assert regular_project.isExpanded()
    regular_project.setExpanded(False)
    assert not running_project.isExpanded()

    pane.set_thread_statuses({"first": "in_progress", "second": "needs_input"})

    running_section = pane.list_widget.topLevelItem(0)
    running_project = running_section.child(0)
    regular_project = pane.list_widget.topLevelItem(1)
    assert running_section.isExpanded()
    assert not running_project.isExpanded()
    assert not regular_project.isExpanded()

    running_section.setExpanded(False)
    regular_project.setExpanded(True)
    assert not running_project.isExpanded()
    pane.set_thread_statuses({"first": "in_progress", "second": "in_progress"})
    running_section = pane.list_widget.topLevelItem(0)
    assert not running_section.isExpanded()
    running_section.setExpanded(True)
    assert not running_section.child(0).isExpanded()
    assert pane.list_widget.topLevelItem(1).isExpanded()


def test_thread_list_refresh_preserves_selected_thread_id() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = ThreadListPane()
    threads = [{"id": "thread-a", "name": "A"}, {"id": "thread-b", "name": "B"}]

    pane.set_threads(threads)
    pane.list_widget.setCurrentItem(_thread_item(pane, "thread-b"))
    pane.set_threads(threads, active_thread_ids={"thread-b"})

    current = pane.list_widget.currentItem()
    assert current is not None
    assert current.data(0, Qt.ItemDataRole.UserRole) == "thread-b"
    assert current.font(0).weight() > _thread_item(pane, "thread-a").font(0).weight()


def test_refresh_does_not_restore_a_thread_after_it_was_removed() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = ThreadListPane()
    pane.set_threads([{"id": "thread-a", "cwd": "/workspace/a"}])
    pane.list_widget.setCurrentItem(_thread_item(pane, "thread-a"))

    pane.set_threads([{"id": "thread-b", "cwd": "/workspace/b"}])
    pane.set_threads([{"id": "thread-a", "cwd": "/workspace/a"}])

    current = pane.list_widget.currentItem()
    assert current is None or current.data(0, Qt.ItemDataRole.UserRole) != "thread-a"


def test_threads_are_grouped_by_normalized_cwd_in_first_seen_order(tmp_path) -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    first = tmp_path / "first"
    second = tmp_path / "second"
    pane = ThreadListPane()

    pane.set_threads(
        [
            {"id": "a", "cwd": str(first)},
            {"id": "b", "cwd": str(second)},
            {"id": "c", "cwd": str(first)},
        ]
    )

    assert pane.list_widget.topLevelItemCount() == 2
    first_group = pane.list_widget.topLevelItem(0)
    second_group = pane.list_widget.topLevelItem(1)
    assert first_group.text(0) == "first"
    assert second_group.text(0) == "second"
    assert first_group.toolTip(0) == str(first)
    assert [first_group.child(index).data(0, Qt.ItemDataRole.UserRole) for index in range(2)] == [
        "a",
        "c",
    ]
    assert second_group.child(0).data(0, Qt.ItemDataRole.UserRole) == "b"
    assert first_group.data(0, Qt.ItemDataRole.UserRole) is None
    assert pane.thread_count == 3


@pytest.mark.skipif(os.name != "nt", reason="Windows normcase is platform-specific")
def test_windows_cwd_case_variants_share_a_parent() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = ThreadListPane()
    pane.set_threads(
        [
            {"id": "upper", "cwd": r"C:\Users\Example\Project"},
            {"id": "lower", "cwd": r"c:\users\example\project"},
        ]
    )

    assert pane.list_widget.topLevelItemCount() == 1
    assert pane.list_widget.topLevelItem(0).childCount() == 2


def test_parent_click_does_not_emit_thread_selection() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = ThreadListPane()
    pane.set_threads([{"id": "a", "cwd": "/workspace/project"}])
    selected: list[str] = []
    pane.thread_selected.connect(selected.append)

    parent = pane.list_widget.topLevelItem(0)
    pane.list_widget.itemClicked.emit(parent, 0)
    pane.list_widget.itemActivated.emit(parent, 0)

    assert selected == []


def test_child_click_emits_thread_id() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = ThreadListPane()
    pane.set_threads([{"id": "thread-a", "cwd": "/workspace/project"}])
    selected: list[str] = []
    pane.thread_selected.connect(selected.append)

    child = _thread_item(pane, "thread-a")
    pane.list_widget.itemClicked.emit(child, 0)

    assert selected == ["thread-a"]


def test_thread_rename_keeps_cwd_group_and_selection(tmp_path) -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    cwd = str(tmp_path / "project")
    pane = ThreadListPane()
    pane.set_threads([{"id": "a", "cwd": cwd}, {"id": "b", "name": "Before", "cwd": cwd}])
    pane.list_widget.topLevelItem(0).setExpanded(False)
    pane.list_widget.setCurrentItem(_thread_item(pane, "b"))

    pane.update_thread_name("b", "After")

    assert pane.list_widget.topLevelItemCount() == 1
    assert not pane.list_widget.topLevelItem(0).isExpanded()
    assert _thread_item(pane, "b").text(0) == "After"
    assert pane.list_widget.currentItem().data(0, Qt.ItemDataRole.UserRole) == "b"


def test_collapsed_groups_stay_collapsed_across_refresh_and_active_rerender(tmp_path) -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    cwd = str(tmp_path / "project")
    threads = [{"id": "a", "cwd": cwd}, {"id": "b", "cwd": cwd}]
    pane = ThreadListPane()
    pane.set_threads(threads)
    group = pane.list_widget.topLevelItem(0)
    group.setExpanded(False)
    assert not group.isExpanded()

    pane.set_threads(threads)
    assert not pane.list_widget.topLevelItem(0).isExpanded()
    pane.set_active_thread_ids({"b"})
    assert not pane.list_widget.topLevelItem(0).isExpanded()
    active = _thread_item(pane, "b")
    assert active.text(0) == "New スレッド"
    assert not active.icon(0).isNull()
    pane.list_widget.topLevelItem(0).setExpanded(True)
    assert pane.list_widget.topLevelItem(0).isExpanded()


def test_filter_matches_children_and_cwd_and_temporarily_expands_groups(tmp_path) -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    cwd = str(tmp_path / "special-project")
    pane = ThreadListPane()
    pane.set_threads(
        [
            {"id": "match-id", "name": "One", "cwd": cwd},
            {"id": "other", "name": "Two", "cwd": cwd},
            {"id": "third", "name": "Three", "cwd": str(tmp_path / "elsewhere")},
        ]
    )
    first_group = pane.list_widget.topLevelItem(0)
    first_group.setExpanded(False)

    pane.filter_edit.setText("match-id")
    assert pane.list_widget.topLevelItemCount() == 1
    assert pane.list_widget.topLevelItem(0).childCount() == 1
    assert pane.list_widget.topLevelItem(0).isExpanded()

    pane.filter_edit.setText("special-project")
    assert pane.list_widget.topLevelItemCount() == 1
    assert pane.list_widget.topLevelItem(0).childCount() == 2
    pane.filter_edit.clear()
    assert pane.list_widget.topLevelItem(0).childCount() == 2
    assert not pane.list_widget.topLevelItem(0).isExpanded()


def test_friendly_project_label_requires_exact_normalized_root_match(tmp_path) -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    root = str(tmp_path / "workspace" / "codexbridge")
    pane = ThreadListPane()
    pane.set_threads(
        [{"id": "exact", "cwd": root}, {"id": "nested", "cwd": root + "/child"}],
        project_names={os.path.normcase(os.path.normpath(root)): "My Project"},
    )

    assert pane.list_widget.topLevelItem(0).text(0) == "My Project — codexbridge"
    assert pane.list_widget.topLevelItem(1).text(0) == "child"
    original_key = pane.list_widget.topLevelItem(0).data(0, Qt.ItemDataRole.UserRole + 1)
    pane.list_widget.setCurrentItem(_thread_item(pane, "exact"))
    pane.set_threads(
        [{"id": "exact", "cwd": root}],
        project_names={original_key: "Renamed Project"},
    )
    assert pane.list_widget.topLevelItem(0).text(0) == "Renamed Project — codexbridge"
    assert pane.list_widget.topLevelItem(0).data(0, Qt.ItemDataRole.UserRole + 1) == original_key
    assert pane.list_widget.topLevelItem(0).data(0, Qt.ItemDataRole.UserRole) is None
    assert _thread_item(pane, "exact").data(0, Qt.ItemDataRole.UserRole) == "exact"
    assert pane.list_widget.currentItem().data(0, Qt.ItemDataRole.UserRole) == "exact"


@pytest.mark.parametrize(
    "threads, expected_child_ids",
    [
        pytest.param(
            [
                {"id": "relative", "cwd": "relative/path"},
                {"id": "missing"},
            ],
            ["relative", "missing"],
            id="relative-first",
        ),
        pytest.param(
            [
                {"id": "missing"},
                {"id": "relative", "cwd": "relative/path"},
            ],
            ["missing", "relative"],
            id="missing-first",
        ),
    ],
)
def test_invalid_cwd_threads_are_kept_in_other_group(
    threads: list[dict[str, str]], expected_child_ids: list[str]
) -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = ThreadListPane()
    pane.set_threads(threads)

    assert pane.list_widget.topLevelItemCount() == 1
    assert pane.list_widget.topLevelItem(0).text(0) == "Other"
    assert pane.list_widget.topLevelItem(0).data(0, Qt.ItemDataRole.UserRole) is None
    assert [item.data(0, Qt.ItemDataRole.UserRole) for item in _thread_items(pane)] == (
        expected_child_ids
    )

    pane.list_widget.topLevelItem(0).setExpanded(False)
    pane.set_threads(threads)
    assert not pane.list_widget.topLevelItem(0).isExpanded()


def test_single_relative_cwd_displays_other_without_friendly_name() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = ThreadListPane()
    pane.set_threads(
        [{"id": "relative", "cwd": "relative/path"}],
        project_names={"relative/path": "Friendly Project"},
    )

    parent = pane.list_widget.topLevelItem(0)
    assert pane.list_widget.topLevelItemCount() == 1
    assert parent.text(0) == "Other"
    assert parent.data(0, Qt.ItemDataRole.UserRole) is None
    assert parent.childCount() == 1
    assert parent.child(0).data(0, Qt.ItemDataRole.UserRole) == "relative"
