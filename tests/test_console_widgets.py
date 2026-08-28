from __future__ import annotations

from PySide6.QtWidgets import QApplication, QLabel

from codex_bridge.console.widgets import (
    ActivityPane,
    HistoryPane,
    TimelineEntry,
    activity_row,
    timeline_entries,
)


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

    assert any("Turn · completed" in label.text() for label in pane.findChildren(QLabel))
