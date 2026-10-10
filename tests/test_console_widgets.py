from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from inspect import signature

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QPalette, QTextOption
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QLabel,
    QPushButton,
    QTextBrowser,
    QTreeWidgetItem,
    QWidget,
)

from codex_bridge.console import usage as usage_module
from codex_bridge.console import widgets as widgets_module
from codex_bridge.console.config import ConsoleConfig
from codex_bridge.console.main_window import MainWindow
from codex_bridge.console.usage import parse_codex_usage
from codex_bridge.console.widgets import (
    ActivityPane,
    HistoryPane,
    ThreadListPane,
    TimelineEntry,
    activity_row,
    format_history_timing,
    format_thread_content,
    timeline_entries,
)
from tests.test_console_main_window import (
    FakeClient,
    FakeCodexProbe,
    FakeCodexUpdateProbe,
    FakeLauncher,
    StableTunnel,
)


def _usage_snapshot() -> object:
    snapshot_type = getattr(usage_module, "CodexUsageSnapshot", None)
    assert snapshot_type is not None
    return snapshot_type(
        parse_codex_usage(
            {
                "rateLimits": {
                    "primary": {"windowDurationMins": 300, "usedPercent": 28},
                    "secondary": {"windowDurationMins": 10080, "usedPercent": 39},
                }
            }
        ),
        datetime(2026, 9, 30, 4, 45, 12, tzinfo=UTC),
    )


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


def test_parent_and_empty_placeholder_have_no_thread_context_menu(tmp_path) -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = ThreadListPane()
    pane.set_threads([{"id": "a", "cwd": str(tmp_path)}])

    assert pane._context_menu_for_item(pane.list_widget.topLevelItem(0)) is None
    pane.set_empty_state("No threads found.")
    placeholder = pane.list_widget.topLevelItem(0)
    assert not placeholder.flags() & Qt.ItemFlag.ItemIsSelectable
    assert pane._context_menu_for_item(placeholder) is None


def test_thread_context_menu_uses_right_clicked_thread_for_all_actions(tmp_path) -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = ThreadListPane()
    pane.set_threads(
        [
            {"id": "selected", "name": "Selected", "cwd": str(tmp_path / "selected")},
            {
                "id": "target",
                "name": "Target",
                "cwd": str(tmp_path),
                "preview": "must not copy",
            },
        ],
        active_thread_ids={"target"},
    )
    pane.list_widget.setCurrentItem(_thread_item(pane, "selected"))
    renamed: list[str] = []
    pane.thread_rename_requested.connect(renamed.append)
    opened: list[str] = []
    pane.thread_open_requested.connect(opened.append)
    copied_threads: list[str] = []
    pane.thread_copy_requested.connect(copied_threads.append)

    menu = pane._context_menu_for_item(_thread_item(pane, "target"))
    actions = menu.actions()

    assert [action.text() for action in actions] == [
        "名前を変更...",
        "Open in Codex App",
        "スレッドIDをコピー",
        "スレッド情報をコピー",
        "Copy thread content",
        "",
        "作業フォルダを開く",
    ]
    actions[0].trigger()
    assert renamed == ["target"]

    actions[1].trigger()
    assert opened == ["target"]

    actions[2].trigger()
    assert application.clipboard().text() == "target"
    application.processEvents()

    actions[3].trigger()
    assert application.clipboard().text() == (
        f"Name: Target\nThread ID: target\nCWD: {tmp_path}\nStatus: active"
    )
    assert "must not copy" not in application.clipboard().text()
    assert pane.list_widget.currentItem().data(0, Qt.ItemDataRole.UserRole) == "selected"

    actions[4].trigger()
    assert copied_threads == ["target"]
    assert pane.list_widget.currentItem().data(0, Qt.ItemDataRole.UserRole) == "selected"


def test_thread_context_menu_disables_folder_action_without_valid_cwd(tmp_path) -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = ThreadListPane()
    pane.set_threads([{"id": "target", "name": "Target"}])

    menu = pane._context_menu_for_item(_thread_item(pane, "target"))

    assert menu.actions()[-1].text() == "作業フォルダを開く"
    assert not menu.actions()[-1].isEnabled()


def test_thread_context_menu_opens_existing_absolute_cwd(tmp_path, monkeypatch) -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = ThreadListPane()
    pane.set_threads([{"id": "target", "cwd": str(tmp_path)}])
    opened: list[QUrl] = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url) or True)

    menu = pane._context_menu_for_item(_thread_item(pane, "target"))
    menu.actions()[-1].trigger()

    assert len(opened) == 1
    assert os.path.normcase(os.path.normpath(opened[0].toLocalFile())) == os.path.normcase(
        os.path.normpath(str(tmp_path))
    )


def test_thread_context_menu_copies_cached_nonexistent_cwd_but_disables_open(
    tmp_path, monkeypatch
) -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = ThreadListPane()
    missing_cwd = str(tmp_path / "not-created")
    pane.set_threads([{"id": "target", "name": "Target", "cwd": missing_cwd}])

    menu = pane._context_menu_for_item(_thread_item(pane, "target"))
    copied: list[str] = []
    monkeypatch.setattr(pane, "_copy_text", copied.append)
    menu.actions()[3].trigger()

    assert len(copied) == 1
    assert f"CWD: {missing_cwd}" in copied[0]
    assert not menu.actions()[-1].isEnabled()


def test_timeline_reverses_desc_items_and_skips_unknown_raw_items() -> None:
    payload = {
        "items": [
            {
                "turn_id": "turn-2",
                "item": {
                    "id": "agent-2",
                    "type": "agentMessage",
                    "text": "final answer",
                    "phase": "final_answer",
                },
            },
            {
                "turn_id": "turn-1",
                "item": {
                    "id": "command-1",
                    "type": "commandExecution",
                    "command": "pytest",
                    "status": "completed",
                    "exit_code": 0,
                },
            },
            {
                "turn_id": "turn-1",
                "item": {
                    "id": "commentary-1",
                    "type": "agentMessage",
                    "text": "progress update",
                    "phase": "commentary",
                },
            },
            {"turn_id": "turn-1", "item": {"id": "plan-1", "type": "plan", "text": "safe plan"}},
            {"turn_id": "turn-1", "item": {"id": "secret", "type": "reasoning", "raw": "never"}},
            {
                "turn_id": "turn-1",
                "item": {"id": "user-1", "type": "userMessage", "text": "hello <world>"},
            },
        ]
    }

    entries = timeline_entries(payload)

    assert entries == (
        TimelineEntry("turn-1", "user-1", "User", "User", "hello <world>", None, ()),
        TimelineEntry("turn-1", "plan-1", "Plan", "Plan", "safe plan", None, ()),
        TimelineEntry(
            "turn-1", "commentary-1", "Commentary", "Commentary", "progress update", None, ()
        ),
        TimelineEntry(
            "turn-1", "command-1", "Command", "Command", "pytest", "completed", ("exit 0",)
        ),
        TimelineEntry("turn-2", "agent-2", "Agent", "Agent", "final answer", None, ()),
    )
    assert "never" not in str(entries)


def test_format_thread_content_preserves_order_roles_and_plain_bodies() -> None:
    entries = (
        TimelineEntry("turn-1", "user-1", "User", "User", "質問\n日本語", None, ()),
        TimelineEntry("turn-1", "agent-1", "Agent", "Agent", "回答", "completed", ("exit 0",)),
        TimelineEntry("turn-1", "progress-1", "Commentary", "Commentary", "進行中", None, ()),
        TimelineEntry("turn-2", "user-2", "User", "User", "次の質問", None, ()),
        TimelineEntry("turn-2", "agent-2", "Agent", "Agent", "次の回答", None, ()),
    )

    assert format_thread_content(entries) == (
        "User:\n質問\n日本語\n\nAgent:\n回答\n\nCommentary:\n進行中"
        "\n\nUser:\n次の質問\n\nAgent:\n次の回答"
    )
    assert "completed" not in format_thread_content(entries)
    assert "exit 0" not in format_thread_content(entries)


def test_history_pane_adds_copy_action_only_to_user_and_agent_messages(monkeypatch) -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = HistoryPane()
    pane.set_timeline(
        (
            TimelineEntry("turn", "user", "User", "User", "質問", None, ()),
            TimelineEntry("turn", "commentary", "Commentary", "Commentary", "進行中", None, ()),
            TimelineEntry("turn", "command", "Command", "Command", "pytest", None, ()),
            TimelineEntry("turn", "agent", "Agent", "Agent", "回答", None, ()),
        )
    )

    buttons = [button for button in pane.findChildren(QPushButton) if button.text() == "Copy"]
    copied: list[str] = []
    monkeypatch.setattr("codex_bridge.console.widgets.copy_to_clipboard", copied.append)

    assert len(buttons) == 2
    buttons[0].click()
    application.processEvents()
    assert copied == ["質問"]
    buttons[1].click()
    application.processEvents()
    assert copied == ["質問", "回答"]


def test_timeline_renders_safe_work_fields_without_raw_dicts() -> None:
    entries = timeline_entries(
        {
            "items": [
                {
                    "turn_id": "turn",
                    "item": {
                        "id": "file",
                        "type": "fileChange",
                        "status": "completed",
                        "paths": ["src/a.py"],
                    },
                },
                {
                    "turn_id": "turn",
                    "item": {
                        "id": "mcp",
                        "type": "mcpToolCall",
                        "server": "server",
                        "tool": "tool",
                        "status": "completed",
                        "arguments": {"secret": "x"},
                    },
                },
                {
                    "turn_id": "turn",
                    "item": {
                        "id": "dynamic",
                        "type": "dynamicToolCall",
                        "tool": "scan",
                        "status": "completed",
                        "namespace": "safe",
                    },
                },
            ]
        }
    )

    assert [entry.title for entry in entries] == ["Dynamic tool", "MCP", "Files"]
    assert entries[0].details == ("safe",)
    assert entries[1].body == "server / tool"
    assert entries[2].body == "src/a.py"
    assert "secret" not in str(entries)


def test_activity_row_uses_only_allowlisted_details() -> None:
    row = activity_row(
        {
            "timestamp": "2026-08-28T00:00:00Z",
            "type": "command_completed",
            "status": "completed",
            "summary": "pytest",
            "details": {
                "exit_code": 0,
                "paths": ["src/a.py"],
                "decision": "accept",
                "raw": "secret",
            },
        }
    )

    assert "2026-08-28T00:00:00Z" in row
    assert "command_completed" in row
    assert "exit 0" in row
    assert "src/a.py" in row
    assert "accept" in row
    assert "secret" not in row


def test_activity_rows_distinguish_commentary_from_final_agent_message() -> None:
    commentary = activity_row(
        {"type": "agent_commentary", "status": "completed", "summary": "progress update"}
    )
    final = activity_row(
        {"type": "agent_message", "status": "completed", "summary": "final answer"}
    )

    assert "Commentary" in commentary
    assert "Agent" in final


def test_activity_pane_hides_empty_and_unloaded_state_but_keeps_recent_activities() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = ActivityPane()

    pane.set_empty_state("No thread selected.")
    assert not pane.state_label.isVisibleTo(pane)

    pane.set_snapshot(
        {
            "state": "not_loaded",
            "pending_request": None,
            "recent_activities": [
                {"activity_id": "activity-1", "type": "turn_completed", "summary": "done"}
            ],
        }
    )

    assert not pane.state_label.isVisibleTo(pane)
    recent_header = next(
        label for label in pane.findChildren(QLabel) if label.text() == "Recent activities"
    )
    assert recent_header.isVisibleTo(pane)
    assert pane.activity_list.count() == 1


def test_activity_pane_shows_active_pending_and_failure_states() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = ActivityPane()

    pane.set_snapshot({"state": "in_progress", "pending_request": None})
    assert pane.state_label.text() == "in_progress"
    assert pane.state_label.isVisibleTo(pane)

    for state in ("needs_approval", "needs_input"):
        pane.set_snapshot(
            {
                "state": state,
                "pending_request": {"summary": "Waiting for a decision"},
            }
        )
        assert pane.state_label.isVisibleTo(pane)
        assert "Waiting for a decision" in pane.pending_label.text()
        assert pane.pending_label.isVisibleTo(pane)

    for state in ("failed", "error"):
        pane.set_snapshot({"state": state, "pending_request": None})
        assert pane.state_label.text() == state
        assert pane.state_label.isVisibleTo(pane)


def test_activity_pane_renders_effective_thread_access_from_metadata_only() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = ActivityPane()

    pane.set_snapshot(
        {
            "state": "in_progress",
            "thread_metadata": {
                "sandbox_mode": "workspace-write",
                "approval_policy": "on-request",
                "approvals_reviewer": "user",
            },
            "sandbox_mode": "danger-full-access",
            "setup": {"sandbox_mode": "danger-full-access"},
        }
    )

    sandbox = pane.findChild(QLabel, "effectiveSandboxMode")
    policy = pane.findChild(QLabel, "effectiveApprovalPolicy")
    reviewer = pane.findChild(QLabel, "effectiveApprovalsReviewer")
    assert sandbox is not None
    assert policy is not None
    assert reviewer is not None
    assert sandbox.text() == "Sandbox: workspace-write"
    assert policy.text() == "Approval policy: on-request"
    assert reviewer.text() == "Approvals reviewer: user"
    assert all(
        label.textFormat() == Qt.TextFormat.PlainText for label in (sandbox, policy, reviewer)
    )


def test_activity_pane_marks_full_access_with_warning_text_and_palette_emphasis() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = ActivityPane()
    pane.set_snapshot(
        {
            "state": "in_progress",
            "thread_metadata": {
                "sandbox_mode": "danger-full-access",
                "approval_policy": "never",
                "approvals_reviewer": "auto_review",
            },
        }
    )

    sandbox = pane.findChild(QLabel, "effectiveSandboxMode")
    assert sandbox is not None
    assert "WARNING" in sandbox.text()
    assert "Full Access" in sandbox.text()
    assert "danger-full-access" in sandbox.text()
    assert sandbox.font().bold()
    assert sandbox.palette().color(QPalette.ColorRole.WindowText) == sandbox.palette().color(
        QPalette.ColorRole.BrightText
    )


@pytest.mark.parametrize(
    ("snapshot", "expected"),
    [
        (
            {"state": "in_progress"},
            ("Sandbox: Unknown", "Approval policy: Unknown", "Approvals reviewer: Unknown"),
        ),
        (
            {"state": "in_progress", "thread_metadata": None},
            ("Sandbox: Unknown", "Approval policy: Unknown", "Approvals reviewer: Unknown"),
        ),
        (
            {"state": "in_progress", "thread_metadata": "sandbox_mode=danger-full-access"},
            ("Sandbox: Unknown", "Approval policy: Unknown", "Approvals reviewer: Unknown"),
        ),
        (
            {"state": "in_progress", "thread_metadata": {"sandbox_mode": "workspace-write"}},
            ("Sandbox: workspace-write", "Approval policy: Unknown", "Approvals reviewer: Unknown"),
        ),
        (
            {
                "state": "in_progress",
                "thread_metadata": {
                    "sandbox_mode": None,
                    "approval_policy": None,
                    "approvals_reviewer": None,
                },
            },
            ("Sandbox: Unknown", "Approval policy: Unknown", "Approvals reviewer: Unknown"),
        ),
        (
            {
                "state": "in_progress",
                "thread_metadata": {
                    "sandbox_mode": "",
                    "approval_policy": " ",
                    "approvals_reviewer": "",
                },
            },
            ("Sandbox: Unknown", "Approval policy: Unknown", "Approvals reviewer: Unknown"),
        ),
        (
            {
                "state": "in_progress",
                "thread_metadata": {
                    "sandbox_mode": 42,
                    "approval_policy": ["on-request"],
                    "approvals_reviewer": {"name": "user"},
                },
            },
            ("Sandbox: Unknown", "Approval policy: Unknown", "Approvals reviewer: Unknown"),
        ),
        (
            {
                "state": "in_progress",
                "thread_metadata": {
                    "sandbox_mode": "<b>danger-full-access</b>",
                    "approval_policy": "unknown-policy",
                    "approvals_reviewer": "unknown-reviewer",
                },
            },
            ("Sandbox: Unknown", "Approval policy: Unknown", "Approvals reviewer: Unknown"),
        ),
        (
            {
                "state": "not_loaded",
                "thread_metadata": {
                    "sandbox_mode": "danger-full-access",
                    "approval_policy": "on-request",
                    "approvals_reviewer": "user",
                },
            },
            ("Sandbox: Unknown", "Approval policy: Unknown", "Approvals reviewer: Unknown"),
        ),
    ],
)
def test_activity_pane_shows_unknown_for_missing_or_invalid_thread_access_metadata(
    snapshot: dict[str, object], expected: tuple[str, str, str]
) -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = ActivityPane()

    pane.set_snapshot(snapshot)

    assert (
        pane.findChild(QLabel, "effectiveSandboxMode").text(),
        pane.findChild(QLabel, "effectiveApprovalPolicy").text(),
        pane.findChild(QLabel, "effectiveApprovalsReviewer").text(),
    ) == expected


def test_activity_pane_clears_effective_access_on_reset_and_error() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = ActivityPane()
    access = {
        "state": "in_progress",
        "thread_metadata": {
            "sandbox_mode": "workspace-write",
            "approval_policy": "on-request",
            "approvals_reviewer": "user",
        },
    }

    pane.set_snapshot(access)
    pane.set_snapshot(
        {
            "state": "in_progress",
            "thread_metadata": {"sandbox_mode": "read-only"},
        }
    )
    assert pane.findChild(QLabel, "effectiveSandboxMode").text() == "Sandbox: read-only"
    assert pane.findChild(QLabel, "effectiveApprovalPolicy").text() == "Approval policy: Unknown"
    assert (
        pane.findChild(QLabel, "effectiveApprovalsReviewer").text() == "Approvals reviewer: Unknown"
    )

    pane.set_snapshot(access)
    pane.set_empty_state("Loading activity…")
    assert pane.findChild(QLabel, "effectiveSandboxMode").text() == "Sandbox: Unknown"
    assert pane.findChild(QLabel, "effectiveApprovalPolicy").text() == "Approval policy: Unknown"
    assert (
        pane.findChild(QLabel, "effectiveApprovalsReviewer").text() == "Approvals reviewer: Unknown"
    )

    pane.set_snapshot(access)
    pane.set_error("Status unavailable")
    assert pane.findChild(QLabel, "effectiveSandboxMode").text() == "Sandbox: Unavailable"
    assert (
        pane.findChild(QLabel, "effectiveApprovalPolicy").text() == "Approval policy: Unavailable"
    )
    assert (
        pane.findChild(QLabel, "effectiveApprovalsReviewer").text()
        == "Approvals reviewer: Unavailable"
    )


def test_activity_pane_shows_request_error_without_dropping_recent_activities() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = ActivityPane()
    pane.set_snapshot(
        {
            "state": "in_progress",
            "thread_metadata": {
                "sandbox_mode": "workspace-write",
                "approval_policy": "on-request",
                "approvals_reviewer": "user",
            },
            "pending_request": None,
            "recent_activities": [
                {"activity_id": "activity-1", "type": "turn_started", "summary": "started"}
            ],
        }
    )

    pane.set_error("Bridge request failed")

    assert pane.state_label.text() == "Bridge request failed"
    assert pane.state_label.isVisibleTo(pane)
    assert pane.activity_list.count() == 1


def test_activity_pane_reviews_command_file_and_permission_approvals() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = ActivityPane()
    requested: list[tuple[object, str]] = []
    pane.approval_decision_requested.connect(
        lambda request_id, decision: requested.append((request_id, decision))
    )
    pane.set_control_available(True)

    pane.set_snapshot(
        {
            "state": "needs_approval",
            "thread_metadata": {
                "sandbox_mode": "workspace-write",
                "approval_policy": "on-request",
                "approvals_reviewer": "user",
            },
            "current_diff": "diff --git a/a.txt b/a.txt",
            "pending_request": {
                "request_id": "command-1",
                "method": "item/commandExecution/requestApproval",
                "thread_id": "thread-1",
                "turn_id": "turn-1",
                "summary": "pytest tests/test_example.py",
            },
        }
    )
    assert pane.findChild(QLabel, "effectiveSandboxMode").text() == "Sandbox: workspace-write"
    assert pane.findChild(QLabel, "effectiveApprovalPolicy").text() == "Approval policy: on-request"
    assert pane.findChild(QLabel, "effectiveApprovalsReviewer").text() == "Approvals reviewer: user"
    command_details = pane.approval_details_label.text()
    assert "Command approval" in command_details
    assert "pytest tests/test_example.py" in command_details
    assert "Thread: thread-1" in command_details
    assert "Turn: turn-1" in command_details

    pane.allow_once_button.click()
    pane.allow_for_session_button.click()
    pane.decline_button.click()
    pane.cancel_button.click()
    assert requested == [
        ("command-1", "accept"),
        ("command-1", "acceptForSession"),
        ("command-1", "decline"),
        ("command-1", "cancel"),
    ]

    pane.set_snapshot(
        {
            "state": "needs_approval",
            "current_diff": "diff --git a/a.txt b/a.txt",
            "pending_request": {
                "request_id": 2,
                "method": "item/fileChange/requestApproval",
                "thread_id": "thread-1",
                "turn_id": "turn-2",
                "summary": "Update a.txt",
            },
        }
    )
    assert "File change approval" in pane.approval_details_label.text()
    assert "diff --git a/a.txt b/a.txt" in pane.approval_details_label.text()

    pane.set_snapshot(
        {
            "state": "needs_approval",
            "pending_request": {
                "request_id": 3,
                "method": "item/permissions/requestApproval",
                "thread_id": "thread-1",
                "turn_id": "turn-3",
                "summary": "Need local access",
                "permission": {
                    "reason": "Need local access",
                    "cwd": "C:/repo",
                    "requested_permissions": {
                        "fileSystem": {"entries": [{"access": "read", "path": "C:/repo"}]},
                        "network": {"enabled": True},
                    },
                    "allowed_scopes": ["turn", "session"],
                },
            },
        }
    )
    permission_details = pane.approval_details_label.text()
    assert "Permission approval" in permission_details
    assert "Reason: Need local access" in permission_details
    assert "C:/repo" in permission_details
    assert "Requested filesystem permissions" in permission_details
    assert "Requested network permissions" in permission_details
    assert "Allowed scopes: turn, session" in permission_details


def test_activity_pane_disables_console_approval_without_control_and_clears_resolved() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = ActivityPane()
    pane.set_snapshot(
        {
            "state": "needs_approval",
            "pending_request": {
                "request_id": 1,
                "method": "item/fileChange/requestApproval",
                "thread_id": "thread-1",
                "turn_id": "turn-1",
                "summary": "Change files",
            },
        }
    )

    assert pane.approval_control_message.text() == (
        "Resolve from MCP client; Console control is unavailable for this Bridge."
    )
    assert not pane.allow_once_button.isEnabled()
    assert not pane.allow_for_session_button.isEnabled()
    assert not pane.decline_button.isEnabled()
    assert not pane.cancel_button.isEnabled()

    pane.set_snapshot({"state": "in_progress", "pending_request": None})

    assert pane.approval_details_label.isHidden()
    assert pane.approval_buttons_widget.isHidden()


def test_activity_pane_caps_rows_and_deduplicates_current_rows() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = ActivityPane()

    def activity(activity_id: str) -> dict[str, object]:
        return {
            "activity_id": activity_id,
            "timestamp": "now",
            "type": "error",
            "status": "failed",
            "summary": activity_id,
            "details": {},
        }

    for index in range(201):
        pane.append_activity(activity(f"activity-{index}"))
    pane.append_activity(activity("activity-200"))
    pane.append_activity(activity("activity-0"))

    assert pane.activity_list.count() == 200
    assert "activity-2" in pane.activity_list.item(0).text()
    assert "activity-0" in pane.activity_list.item(199).text()


def test_history_pane_shows_turn_status_in_separator() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = HistoryPane()
    entries = (
        TimelineEntry("turn-1", "item-1", "User", "User", "old", None, ()),
        TimelineEntry("turn-2", "item-2", "Agent", "Agent", "new", None, ()),
    )

    pane.set_timeline(entries, turn_statuses={"turn-2": "completed"})

    assert any("Turn · Model: unavailable" in label.text() for label in pane.findChildren(QLabel))
    assert any("Turn · completed" in label.text() for label in pane.findChildren(QLabel))


def test_format_history_timing_uses_local_fixed_formats_and_unknown_markers() -> None:
    start = datetime.now().astimezone().replace(hour=17, minute=19, second=8, microsecond=0)
    end = start.replace(second=23)
    start_ms = int(start.timestamp() * 1_000)
    end_ms = int(end.timestamp() * 1_000)

    assert format_history_timing(start_ms, end_ms) == "Start 17:19:08 · End 17:19:23"
    assert format_history_timing(start_ms, None) == "Start 17:19:08 · End —"
    assert format_history_timing(None, None) == "Start — · End —"
    assert format_history_timing(True, 10**100) == "Start — · End —"


def test_format_history_timing_marks_both_dates_when_item_crosses_midnight() -> None:
    start = datetime.now().astimezone().replace(hour=23, minute=59, second=59, microsecond=0)
    end = start + timedelta(seconds=2)
    start_ms = int(start.timestamp() * 1_000)
    end_ms = int(end.timestamp() * 1_000)
    expected = f"Start {start.strftime('%m/%d %H:%M:%S')} · End {end.strftime('%m/%d %H:%M:%S')}"

    assert start.date() != end.date()
    assert format_history_timing(start_ms, end_ms) == expected


def test_timeline_entries_project_optional_item_timestamps() -> None:
    entries = timeline_entries(
        {
            "items": [
                {
                    "turn_id": "turn",
                    "item": {
                        "id": "command",
                        "type": "commandExecution",
                        "command": "pytest",
                        "started_at_ms": 123,
                        "completed_at_ms": True,
                    },
                }
            ]
        }
    )

    assert len(entries) == 1
    assert entries[0].started_at_ms == 123
    assert entries[0].completed_at_ms is None


@pytest.mark.parametrize(
    ("status", "completed_at_ms", "expected"),
    [
        ("in_progress", None, True),
        (None, None, True),
        ("completed", 2_000, False),
        ("failed", None, False),
        ("interrupted", None, False),
        ("error", None, False),
    ],
)
def test_history_card_shows_spinner_only_for_running_items(
    status: str | None, completed_at_ms: int | None, expected: bool
) -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = HistoryPane()
    pane.set_timeline(
        (
            TimelineEntry(
                "turn", "item", "Agent", "Agent", "answer", status, (), 1_000, completed_at_ms
            ),
        )
    )
    pane.show()
    _process_layout(application)

    spinner = pane.findChild(QWidget, "historyRunningSpinner")
    timing = pane.findChild(QLabel, "historyTiming")
    assert spinner is not None
    assert timing is not None
    assert timing.text() == (
        "Start "
        + datetime.fromtimestamp(1).strftime("%H:%M:%S")
        + " · End "
        + (
            datetime.fromtimestamp(completed_at_ms / 1_000).strftime("%H:%M:%S")
            if completed_at_ms is not None
            else "—"
        )
    )
    assert spinner.isVisible() is expected
    timer = spinner._timer
    assert timer.interval() == 90
    assert timer.isActive() is expected
    pane.hide()
    _process_layout(application)
    assert not timer.isActive()
    destroyed: list[bool] = []
    timer.destroyed.connect(lambda *_args: destroyed.append(True))
    spinner.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert destroyed
    pane.deleteLater()
    _process_layout(application)


def test_running_agent_spinner_coexists_with_copy_button() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = HistoryPane()
    pane.set_timeline(
        (TimelineEntry("turn", "item", "Agent", "Agent", "answer", "in_progress", (), 1_000),)
    )
    pane.show()
    _process_layout(application)

    card = pane.findChild(QFrame, "historyCard")
    assert card is not None
    assert card.findChild(QPushButton, "copyMessageButton") is not None
    spinner = card.findChild(QWidget, "historyRunningSpinner")
    assert spinner is not None and spinner.isVisible()
    pane.close()


def test_history_running_spinner_is_created_with_card_parent(monkeypatch) -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    created_with_parents: list[QWidget | None] = []
    spinner_type = widgets_module._HistoryRunningSpinner

    class ParentRecordingSpinner(spinner_type):
        def __init__(self, parent: QWidget | None = None) -> None:
            created_with_parents.append(parent)
            super().__init__(parent)

    monkeypatch.setattr(widgets_module, "_HistoryRunningSpinner", ParentRecordingSpinner)
    pane = HistoryPane()
    pane.set_timeline(
        (TimelineEntry("turn", "item", "Agent", "Agent", "answer", "in_progress", (), 1_000),)
    )

    card = pane.findChild(QFrame, "historyCard")
    spinner = pane.findChild(QWidget, "historyRunningSpinner")
    assert card is not None
    assert spinner is not None
    assert created_with_parents == [card]
    assert spinner.parent() is card
    pane.close()


def test_history_pane_shows_usage_snapshot_on_a_separate_wrapping_line() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = HistoryPane()
    snapshot = _usage_snapshot()
    assert "turn_usage_snapshots" in signature(pane.set_timeline).parameters
    pane.resize(420, 300)
    pane.set_timeline(
        (TimelineEntry("turn", "item", "Agent", "Agent", "answer", None, ()),),
        turn_usage_snapshots={"turn": snapshot},
    )
    pane.show()
    _process_layout(application)

    snapshot_label = pane.findChild(QLabel, "turnUsageSnapshot")
    assert snapshot_label is not None
    assert snapshot_label.wordWrap()
    assert snapshot_label.text().startswith("Usage snapshot: 5h 72% · Week 61% · captured ")
    assert pane._scroll.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert pane._content.width() == pane._scroll.viewport().width()
    assert snapshot_label.width() <= pane._scroll.viewport().width()


def test_history_pane_omits_usage_snapshot_when_turn_has_none() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = HistoryPane()

    pane.set_timeline((TimelineEntry("turn", "item", "Agent", "Agent", "answer", None, ()),))

    assert pane.findChild(QLabel, "turnUsageSnapshot") is None


def test_history_pane_preserves_manual_scroll_position_when_snapshot_is_added() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = HistoryPane()
    entries = tuple(
        TimelineEntry(f"turn-{index}", f"item-{index}", "Agent", "Agent", "answer", None, ())
        for index in range(30)
    )
    snapshot = _usage_snapshot()
    assert "turn_usage_snapshots" in signature(pane.set_timeline).parameters
    pane.resize(600, 300)
    pane.set_timeline(entries)
    pane.show()
    _process_layout(application)
    scrollbar = pane._scroll.verticalScrollBar()
    scrollbar.setValue(scrollbar.maximum() // 2)
    _process_layout(application)
    old_value = scrollbar.value()

    pane.set_timeline(entries, turn_usage_snapshots={"turn-0": snapshot})
    _process_layout(application)

    assert scrollbar.value() == old_value
    assert scrollbar.value() < scrollbar.maximum()


def test_history_pane_shows_each_message_in_full_without_inner_scroll() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = HistoryPane()
    message = "first line\nsecond line with <literal> text"

    pane.set_timeline((TimelineEntry("turn", "item", "Agent", "Agent", message, None, ()),))

    body_widgets = [
        widget for widget in pane.findChildren(QTextBrowser) if widget.toPlainText() == message
    ]
    assert len(body_widgets) == 1
    body = body_widgets[0]
    assert body.wordWrapMode() == QTextOption.WrapMode.WrapAnywhere
    assert body.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse
    assert body.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert body.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff


def test_history_pane_disables_horizontal_scrolling() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = HistoryPane()

    assert pane._scroll.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff


def test_history_pane_reflows_long_content_to_viewport_and_preserves_copy(monkeypatch) -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = HistoryPane()
    body_text = "https://" + "u" * 6_000
    command = "powershell -NoProfile -Command " + "x" * 3_000
    path = "C:\\" + ("nested\\" * 400) + "result.txt"
    pane.resize(900, 500)
    pane.set_timeline(
        (
            TimelineEntry("turn", "body", "Agent", "Agent", body_text, None, ()),
            TimelineEntry("turn", "command", "Command", "Command", command, None, ()),
            TimelineEntry("turn", "file", "Files", "Files", "Changed file", None, (path,)),
        )
    )
    pane.show()
    application.processEvents()

    body = next(
        widget for widget in pane.findChildren(QTextBrowser) if widget.toPlainText() == body_text
    )
    wide_body_height = body.height()
    pane.resize(560, 500)
    application.processEvents()

    viewport = pane._scroll.viewport()
    assert pane._scroll.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert pane._content.width() == viewport.width()
    assert pane._content.minimumSizeHint().width() <= viewport.width()
    assert body.toPlainText() == body_text
    assert body.height() > wide_body_height
    text_widgets = pane._content.findChildren(QTextBrowser)
    assert {widget.toPlainText() for widget in text_widgets} == {
        body_text,
        command,
        "Changed file",
        path,
    }
    assert all(
        widget.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        and widget.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        and widget.verticalScrollBar().maximum() == 0
        for widget in text_widgets
    )
    cards = pane._content.findChildren(QFrame, "historyCard")
    assert len(cards) == 3
    assert all(card.geometry().width() <= viewport.width() for card in cards)

    copied: list[str] = []
    monkeypatch.setattr("codex_bridge.console.widgets.copy_to_clipboard", copied.append)
    next(
        button
        for button in pane.findChildren(QPushButton)
        if button.objectName() == "copyMessageButton"
    ).click()
    application.processEvents()
    assert copied == [body_text]


def test_history_pane_adds_model_metadata_to_each_turn_header_only() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = HistoryPane()
    entries = (
        TimelineEntry("turn", "agent", "Agent", "Agent", "answer", None, ()),
        TimelineEntry("turn", "user", "User", "User", "question", None, ()),
    )

    pane.set_timeline(
        entries,
        turn_statuses={"turn": "completed"},
        turn_model_metadata={
            "turn": {
                "model_candidates": [
                    {"model": "gpt-5", "reasoning_effort": "high"},
                ],
                "model_resolution_status": "resolved",
            }
        },
    )

    headers = {label.text() for label in pane._content.findChildren(QLabel)}
    assert "Turn · completed · Model: gpt-5 (high)" in headers
    assert "Agent · Model: gpt-5 · Reasoning: high" not in headers
    assert "User" in headers


def test_history_pane_formats_missing_effort_and_missing_status() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = HistoryPane()

    pane.set_timeline(
        (TimelineEntry("turn", "item", "Agent", "Agent", "answer", None, ()),),
        turn_model_metadata={
            "turn": {
                "model_candidates": [{"model": "gpt-5", "reasoning_effort": None}],
                "model_resolution_status": "resolved",
            }
        },
    )

    assert "Turn · Model: gpt-5" in {label.text() for label in pane._content.findChildren(QLabel)}


def test_history_pane_formats_multiple_candidates_in_source_order() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = HistoryPane()

    pane.set_timeline(
        (TimelineEntry("turn", "item", "Agent", "Agent", "answer", None, ()),),
        turn_model_metadata={
            "turn": {
                "model_candidates": [
                    {"model": "gpt-5.6-luna", "reasoning_effort": "xhigh"},
                    {"model": "gpt-5.6-sol", "reasoning_effort": "high"},
                ],
                "model_resolution_status": "multiple",
            }
        },
    )

    assert "Turn · Models: gpt-5.6-luna (xhigh) / gpt-5.6-sol (high)" in {
        label.text() for label in pane._content.findChildren(QLabel)
    }


def test_history_pane_formats_unavailable_model() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = HistoryPane()

    pane.set_timeline(
        (TimelineEntry("turn", "item", "Agent", "Agent", "answer", None, ()),),
        turn_model_metadata={
            "turn": {
                "model_candidates": [],
                "model_resolution_status": "unavailable",
            }
        },
    )

    assert "Turn · Model: unavailable" in {
        label.text() for label in pane._content.findChildren(QLabel)
    }


def test_history_pane_shrinks_scroll_content_after_long_timeline_is_replaced() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = HistoryPane()
    pane.resize(900, 600)
    long_body = "line " * 2000
    long_entries = tuple(
        TimelineEntry("long", "item", "Agent", "Agent", long_body, None, ()) for _ in range(8)
    )
    pane.set_timeline(long_entries)
    pane.show()
    application.processEvents()
    assert pane._content.height() > pane._scroll.viewport().height()
    pane._scroll.verticalScrollBar().setValue(pane._scroll.verticalScrollBar().maximum())
    application.processEvents()

    pane.set_timeline((TimelineEntry("short", "item", "Agent", "Agent", "short", None, ()),))
    application.processEvents()

    last_widget = pane._content_layout.itemAt(pane._content_layout.count() - 1).widget()
    assert last_widget is not None
    scrollbar = pane._scroll.verticalScrollBar()
    diagnostics = (
        f"content_height={pane._content.height()} "
        f"content_size_hint={pane._content.sizeHint().height()} "
        f"last_bottom={last_widget.geometry().bottom()} "
        f"maximum={scrollbar.maximum()} page_step={scrollbar.pageStep()}"
    )
    assert pane._content.height() <= pane._scroll.viewport().height(), diagnostics
    assert scrollbar.maximum() == 0, diagnostics


def test_history_pane_has_no_scrollable_blank_space_after_initial_long_timeline() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    items: list[dict[str, object]] = [
        {
            "turn_id": "turn-1",
            "item": {
                "id": "agent-long",
                "type": "agentMessage",
                "text": "word " * 150,
            },
        }
    ]
    for index in range(1, 45):
        item_type = ("userMessage", "agentMessage", "commandExecution")[index % 3]
        item: dict[str, object] = {
            "id": f"item-{index}",
            "type": item_type,
            "text": "word " * (8 + index % 8),
        }
        if item_type == "commandExecution":
            item["command"] = item.pop("text")
            item["status"] = "completed"
            item["exit_code"] = 0
        items.append({"turn_id": "turn-1" if index < 23 else "turn-2", "item": item})

    window = MainWindow(ConsoleConfig(), api_client=FakeClient(), tray_available=False)
    window.resize(1400, 850)
    window.history_pane.set_timeline(timeline_entries({"items": items}))
    window.show()
    application.processEvents()
    scrollbar = window.history_pane._scroll.verticalScrollBar()
    scrollbar.setValue(scrollbar.maximum())
    application.processEvents()

    last_item = window.history_pane._content_layout.itemAt(
        window.history_pane._content_layout.count() - 1
    )
    last_widget = last_item.widget() if last_item is not None else None
    assert last_widget is not None
    viewport = window.history_pane._scroll.viewport()
    blank_space = scrollbar.value() + viewport.height() - last_widget.geometry().bottom() - 1
    diagnostics = (
        f"content={window.history_pane._content.geometry().getRect()} "
        f"content_minimum={window.history_pane._content.minimumSizeHint().height()} "
        f"last={last_widget.geometry().getRect()} "
        f"maximum={scrollbar.maximum()} page_step={scrollbar.pageStep()} "
        f"blank_space={blank_space}"
    )
    window.close()

    assert blank_space <= 32, diagnostics


def _history_entries(start: int, count: int) -> tuple[TimelineEntry, ...]:
    return tuple(
        TimelineEntry(
            "turn-1",
            f"item-{index}",
            "Agent",
            "Agent",
            f"entry-{index}\n" + "line\n" * 5,
            None,
            (),
        )
        for index in range(start, start + count)
    )


def _process_layout(application: QApplication) -> None:
    application.processEvents()
    application.processEvents()
    application.processEvents()


def _history_payload(thread_id: str, count: int) -> dict[str, object]:
    return {
        "items": [
            {
                "turn_id": f"{thread_id}-turn",
                "item": {
                    "id": f"{thread_id}-{index}",
                    "type": "agentMessage",
                    "text": f"{thread_id} entry {index}\n" + "line\n" * 5,
                },
            }
            for index in range(count)
        ]
    }


def test_history_pane_starts_at_bottom_after_initial_long_timeline() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = HistoryPane()
    pane.resize(720, 420)
    pane.show()

    pane.set_timeline(_history_entries(0, 18))
    _process_layout(application)

    scrollbar = pane._scroll.verticalScrollBar()
    assert scrollbar.maximum() > 0
    assert scrollbar.value() == scrollbar.maximum()


def test_history_pane_follows_new_content_when_already_at_bottom() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = HistoryPane()
    pane.resize(720, 420)
    pane.show()
    pane.set_timeline(_history_entries(0, 18))
    _process_layout(application)

    pane.set_timeline(_history_entries(0, 24))
    _process_layout(application)

    scrollbar = pane._scroll.verticalScrollBar()
    assert scrollbar.value() == scrollbar.maximum()


def test_history_pane_does_not_jump_to_bottom_after_user_scrolls_up() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = HistoryPane()
    pane.resize(720, 420)
    pane.show()
    pane.set_timeline(_history_entries(0, 24))
    _process_layout(application)
    scrollbar = pane._scroll.verticalScrollBar()
    scrollbar.setValue(scrollbar.maximum() // 2)
    _process_layout(application)
    old_value = scrollbar.value()

    pane.set_timeline(_history_entries(0, 30))
    _process_layout(application)

    assert scrollbar.value() == old_value
    assert scrollbar.value() < scrollbar.maximum()


def test_history_pane_keeps_the_same_visible_card_after_normal_update() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = HistoryPane()
    pane.resize(720, 420)
    pane.show()
    pane.set_timeline(_history_entries(0, 24))
    _process_layout(application)
    scrollbar = pane._scroll.verticalScrollBar()
    scrollbar.setValue(scrollbar.maximum() // 2)
    _process_layout(application)
    anchor = pane._content_layout.itemAt(9).widget()
    assert anchor is not None
    old_viewport_y = anchor.geometry().top() - scrollbar.value()

    pane.set_timeline(_history_entries(0, 30))
    _process_layout(application)

    anchor = pane._content_layout.itemAt(9).widget()
    assert anchor is not None
    assert anchor.geometry().top() - scrollbar.value() == old_viewport_y


def test_history_pane_resumes_following_after_user_returns_to_bottom() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = HistoryPane()
    pane.resize(720, 420)
    pane.show()
    pane.set_timeline(_history_entries(0, 24))
    _process_layout(application)
    scrollbar = pane._scroll.verticalScrollBar()
    scrollbar.setValue(scrollbar.maximum() // 2)
    _process_layout(application)
    scrollbar.setValue(scrollbar.maximum())
    _process_layout(application)

    pane.set_timeline(_history_entries(0, 30))
    _process_layout(application)

    assert scrollbar.value() == scrollbar.maximum()


def test_history_pane_preserves_scroll_position_while_scrollbar_is_dragged() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = HistoryPane()
    pane.resize(720, 420)
    pane.show()
    pane.set_timeline(_history_entries(0, 24))
    _process_layout(application)
    scrollbar = pane._scroll.verticalScrollBar()
    old_maximum = scrollbar.maximum()
    scrollbar.setValue(old_maximum)
    scrollbar.sliderPressed.emit()

    pane.set_timeline(_history_entries(0, 30))
    _process_layout(application)

    assert scrollbar.value() == old_maximum
    assert scrollbar.value() < scrollbar.maximum()
    assert pane._user_scrolling is True

    scrollbar.sliderReleased.emit()
    _process_layout(application)
    assert pane._user_scrolling is False
    assert pane._follow_newest is False


def test_history_pane_shows_load_older_only_at_top() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = HistoryPane()
    pane.resize(720, 420)
    pane.show()
    pane.set_timeline(_history_entries(0, 24), has_older=True)
    _process_layout(application)
    scrollbar = pane._scroll.verticalScrollBar()

    scrollbar.setValue(0)
    _process_layout(application)
    assert pane.load_older_button.isVisible()

    scrollbar.setValue(scrollbar.maximum() // 2)
    _process_layout(application)
    assert not pane.load_older_button.isVisible()

    scrollbar.setValue(0)
    _process_layout(application)
    assert pane.load_older_button.isVisible()


def test_history_pane_keeps_viewport_anchor_when_prepending_older_entries() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = HistoryPane()
    pane.resize(720, 420)
    pane.show()
    current = _history_entries(0, 18)
    pane.set_timeline(current, has_older=True)
    _process_layout(application)
    scrollbar = pane._scroll.verticalScrollBar()
    scrollbar.setValue(0)
    _process_layout(application)
    old_maximum = scrollbar.maximum()
    old_value = scrollbar.value()
    anchor = pane._content_layout.itemAt(1).widget()
    assert anchor is not None
    old_viewport_y = anchor.geometry().top() - old_value

    pane.set_timeline(_history_entries(-5, 5) + current, has_older=True, prepend=True)
    _process_layout(application)

    anchor = pane._content_layout.itemAt(6).widget()
    assert anchor is not None
    new_viewport_y = anchor.geometry().top() - scrollbar.value()
    assert abs(new_viewport_y - old_viewport_y) <= 2
    assert scrollbar.maximum() > old_maximum
    assert scrollbar.value() < scrollbar.maximum()
    assert not pane.load_older_button.isVisible()


def test_history_pane_does_not_jump_to_bottom_when_short_timeline_is_prepended() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = HistoryPane()
    pane.resize(720, 420)
    pane.show()
    current = _history_entries(0, 1)
    pane.set_timeline(current, has_older=True)
    _process_layout(application)
    scrollbar = pane._scroll.verticalScrollBar()
    assert scrollbar.maximum() == 0
    assert pane.load_older_button.isVisible()

    pane.set_timeline(_history_entries(-8, 8) + current, has_older=True, prepend=True)
    _process_layout(application)

    assert scrollbar.maximum() > 0
    assert scrollbar.value() == 0
    assert scrollbar.value() < scrollbar.maximum()


def test_history_pane_hides_load_older_when_no_older_page_exists() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = HistoryPane()
    pane.resize(720, 420)
    pane.show()
    pane.set_timeline(_history_entries(0, 24), has_older=True)
    _process_layout(application)
    pane._scroll.verticalScrollBar().setValue(0)
    _process_layout(application)
    assert pane.load_older_button.isVisible()

    pane.set_timeline(_history_entries(0, 24), has_older=False)
    _process_layout(application)

    assert not pane.load_older_button.isVisible()


def test_history_pane_resets_follow_state_when_thread_selection_changes() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    client = FakeClient()
    window = MainWindow(
        ConsoleConfig(),
        api_client=client,
        codex_probe=FakeCodexProbe(),
        codex_update_probe=FakeCodexUpdateProbe(),
        runtime_launcher=FakeLauncher(),
        tunnel_supervisor=StableTunnel(),
        tray_available=False,
        quit_application=lambda: None,
    )
    window.resize(900, 620)
    window.show()

    window.select_thread("thread-a")
    client.result("selection:1:items", _history_payload("thread-a", 24))
    _process_layout(application)
    scrollbar = window.history_pane._scroll.verticalScrollBar()
    scrollbar.setValue(scrollbar.maximum() // 2)
    _process_layout(application)
    assert scrollbar.value() < scrollbar.maximum()

    window.select_thread("thread-b")
    _process_layout(application)
    client.result("selection:2:items", _history_payload("thread-b", 24))
    _process_layout(application)

    assert scrollbar.maximum() > 0
    assert scrollbar.value() == scrollbar.maximum()
    window.close()
