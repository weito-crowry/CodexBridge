from __future__ import annotations

import os

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication, QTreeWidgetItem

from codex_bridge.console.widgets import ThreadListPane


def _thread_items(pane: ThreadListPane) -> list[QTreeWidgetItem]:
    return [
        pane.list_widget.topLevelItem(group_index).child(child_index)
        for group_index in range(pane.list_widget.topLevelItemCount())
        for child_index in range(pane.list_widget.topLevelItem(group_index).childCount())
    ]


def _thread_item(pane: ThreadListPane, thread_id: str) -> QTreeWidgetItem:
    return next(
        item for item in _thread_items(pane) if item.data(0, Qt.ItemDataRole.UserRole) == thread_id
    )


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

    assert _thread_item(pane, "running").text(0) == "\u25cf Running"
    assert _thread_item(pane, "approval").text(0) == "\u23f8 Approval"
    assert _thread_item(pane, "input").text(0) == "\u270e Input"
    assert _thread_item(pane, "locked").text(0) == "Locked"
    assert _thread_item(pane, "stopping").text(0) == "Stopping"


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

    assert _thread_item(pane, "active").text(0) == "\u25cf Active"
    assert _thread_item(pane, "idle").text(0) == "Idle"
    pane.list_widget.setCurrentItem(_thread_item(pane, "active"))
    selected = pane.list_widget.currentItem()
    assert selected is not None
    assert selected.text(0) == "\u25cf Active"
    assert selected.data(0, Qt.ItemDataRole.UserRole) == "active"

    pane.set_active_thread_ids(set())

    selected = pane.list_widget.currentItem()
    assert selected is not None
    assert selected.text(0) == "Active"
    assert selected.data(0, Qt.ItemDataRole.UserRole) == "active"


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
    assert _thread_item(pane, "b").text(0).startswith("\u25cf ")
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
