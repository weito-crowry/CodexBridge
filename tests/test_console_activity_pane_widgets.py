from __future__ import annotations

from PySide6.QtWidgets import QApplication

from codex_bridge.console.widgets import ActivityPane, activity_row


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


def test_activity_pane_displays_bounded_read_only_command_output_and_process_metadata() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = ActivityPane()

    pane.append_activity(
        {
            "activity_id": "started",
            "type": "command_started",
            "item_id": "command-1",
            "summary": "python task.py",
            "details": {"process_id": "pty-42", "cwd": "C:/workspace"},
        }
    )
    pane.append_activity(
        {
            "activity_id": "output-1",
            "type": "command_output",
            "item_id": "command-1",
            "summary": "日本語 output",
            "details": {},
        }
    )
    pane.append_activity(
        {
            "activity_id": "output-2",
            "type": "command_output",
            "item_id": "command-1",
            "summary": "tail",
            "details": {"output_truncated": True},
        }
    )
    pane.append_activity(
        {
            "activity_id": "output-3",
            "type": "command_output",
            "item_id": "command-2",
            "summary": "cached output",
            "details": {"output_source": "completed_item"},
        }
    )
    pane.append_activity(
        {
            "activity_id": "output-3",
            "type": "command_output",
            "item_id": "command-2",
            "summary": "duplicate",
            "details": {},
        }
    )

    output = pane.command_output_text.toPlainText()
    assert pane.command_output_text.isReadOnly()
    assert (
        "[Command command-1 · App Server process id: pty-42 · cwd: C:/workspace] python task.py"
        in output
    )
    assert "日本語 output" in output
    assert "tail" in output
    assert "Output delta truncated" in output
    assert "Final output recovered from the completed App Server item" in output
    assert output.count("cached output") == 1
    assert pane.activity_list.count() == 1

    pane.append_activity(
        {
            "activity_id": "started-empty",
            "type": "command_started",
            "item_id": "command-empty",
            "summary": "quiet command",
            "details": {},
        }
    )
    pane.append_activity(
        {
            "activity_id": "completed-empty",
            "type": "command_completed",
            "item_id": "command-empty",
            "status": "failed",
            "summary": "quiet command",
            "details": {"exit_code": 9},
        }
    )
    assert "No command output was observed" in pane.command_output_text.toPlainText()
    assert "[failed · exit 9]" in pane.command_output_text.toPlainText()

    pane.append_activity(
        {
            "activity_id": "output-large",
            "type": "command_output",
            "item_id": "command-3",
            "summary": "x" * 2000,
            "details": {},
        }
    )
    assert len(pane.command_output_text.toPlainText()) <= 256_000


def test_activity_rows_distinguish_commentary_from_final_agent_message() -> None:
    commentary = activity_row(
        {"type": "agent_commentary", "status": "completed", "summary": "progress update"}
    )
    final = activity_row(
        {"type": "agent_message", "status": "completed", "summary": "final answer"}
    )

    assert "Commentary" in commentary
    assert "Agent" in final


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
