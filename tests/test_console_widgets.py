from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication, QLabel, QTextEdit

from codex_bridge.console.widgets import (
    ActivityPane,
    HistoryPane,
    ThreadListPane,
    TimelineEntry,
    activity_row,
    timeline_entries,
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

    assert [pane.list_widget.item(index).text() for index in range(4)] == [
        "Named thread",
        "New スレッド",
        "New スレッド",
        "New スレッド",
    ]
    assert all("Preview text" not in pane.list_widget.item(index).text() for index in range(4))
    for index, thread_id in enumerate(("named", "missing", "empty", "invalid")):
        item = pane.list_widget.item(index)
        assert item.data(Qt.ItemDataRole.UserRole) == thread_id
        assert item.toolTip() == thread_id


def test_thread_list_marks_active_threads_with_palette_color_and_bold_font() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = ThreadListPane()

    pane.set_threads(
        [{"id": "active", "name": "Active"}, {"id": "idle", "name": "Idle"}],
        active_thread_ids={"active"},
    )

    active = pane.list_widget.item(0)
    idle = pane.list_widget.item(1)
    assert active.font().weight() > idle.font().weight()
    assert active.foreground().color() == pane.list_widget.palette().color(QPalette.ColorRole.Link)
    assert active.foreground().color() != idle.foreground().color()


def test_thread_list_refresh_preserves_selected_thread_id() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = ThreadListPane()
    threads = [{"id": "thread-a", "name": "A"}, {"id": "thread-b", "name": "B"}]

    pane.set_threads(threads)
    pane.list_widget.setCurrentRow(1)
    pane.set_threads(threads, active_thread_ids={"thread-b"})

    current = pane.list_widget.currentItem()
    assert current is not None
    assert current.data(Qt.ItemDataRole.UserRole) == "thread-b"
    assert current.font().weight() > pane.list_widget.item(0).font().weight()


def test_timeline_reverses_desc_items_and_skips_unknown_raw_items() -> None:
    payload = {
        "items": [
            {"turn_id": "turn-2", "item": {"id": "agent-2", "type": "agentMessage", "text": "new"}},
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
        TimelineEntry(
            "turn-1", "command-1", "Command", "Command", "pytest", "completed", ("exit 0",)
        ),
        TimelineEntry("turn-2", "agent-2", "Agent", "Agent", "new", None, ()),
    )
    assert "never" not in str(entries)


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


def test_history_pane_shows_each_message_in_full_without_inner_scroll() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = HistoryPane()
    message = "first line\nsecond line with <literal> text"

    pane.set_timeline((TimelineEntry("turn", "item", "Agent", "Agent", message, None, ()),))

    body_labels = [label for label in pane.findChildren(QLabel) if label.text() == message]
    assert len(body_labels) == 1
    body = body_labels[0]
    assert body.wordWrap() is True
    assert body.textFormat() == Qt.TextFormat.PlainText
    assert body.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse
    assert not pane.findChildren(QTextEdit)


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
