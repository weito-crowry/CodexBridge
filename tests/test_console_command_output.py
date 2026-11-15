from __future__ import annotations

import pytest
from PySide6.QtGui import QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from codex_bridge.console.activity_pane import ActivityPane


@pytest.fixture
def pane():
    application = QApplication.instance() or QApplication([])
    widget = ActivityPane()
    widget.process_monitor.shutdown()
    yield widget
    widget.close()
    application.processEvents()


def event(identity, kind="command_output", item="A", text="", status="", **details):
    return dict(
        activity_id=identity,
        type=kind,
        thread_id="thread",
        turn_id="turn",
        item_id=item,
        summary=text,
        status=status,
        details=details,
    )


def test_parallel_output_is_partitioned_and_replay_preserves_panels(pane):
    events = [
        event("a1", text="A1"),
        event("b1", item="B", text="B1"),
        event("a2", text="A2"),
        event("b2", item="B", text="B2"),
    ]
    for activity in events:
        pane.append_activity(activity)
    a, b = pane.command_output.panels.values()
    assert a.output == "A1A2"
    assert b.output == "B1B2"
    assert not a.toggle_button.isChecked()
    a.toggle_button.click()
    b.toggle_button.click()
    pane.set_snapshot({"recent_activities": events})
    assert len(pane.command_output.panels) == 2
    assert a.output == "A1A2"
    assert a.toggle_button.isChecked() and b.toggle_button.isChecked()


def test_late_start_fills_metadata_without_reopening_terminal_command(pane):
    pane.append_activity(
        event("done", "command_completed", text="cmd", status="failed", exit_code=7, duration_ms=42)
    )
    pane.append_activity(
        event(
            "start",
            "command_started",
            text="cmd --full",
            status="in_progress",
            process_id="p42",
            cwd="D:/work",
        )
    )
    (panel,) = pane.command_output.panels.values()
    assert len(pane.command_output.panels) == 1
    assert "Failed" in panel.toggle_button.text()
    assert "exit 7" in panel.toggle_button.text()
    assert "42 ms" in panel.toggle_button.text()
    assert "cmd --full" in panel.toggle_button.text()
    assert "p42" in panel.metadata_label.text()
    assert "D:/work" in panel.metadata_label.text()
    assert "not observed" in panel.note_label.text()


def test_missing_item_id_events_remain_independent_and_turn_ids_are_not_merged(pane):
    pane.append_activity(event("known", text="known"))
    pane.append_activity(event("unknown1", item=None, text="orphan1"))
    pane.append_activity(event("unknown2", item=None, text="orphan2"))
    next_turn = event("next", text="next turn")
    next_turn["turn_id"] = "turn2"
    pane.append_activity(next_turn)
    panels = list(pane.command_output.panels.values())
    assert [p.output for p in panels] == [
        "known",
        "orphan1",
        "orphan2",
        "next turn",
    ]
    assert "Unidentified" in panels[1].toggle_button.text()


def test_recovered_truncated_empty_and_interrupted_outputs_are_distinct(pane):
    pane.append_activity(
        event("recovered", text="tail", output_source="completed_item", output_truncated=True)
    )
    pane.append_activity(event("empty", item="B", text=""))
    pane.append_activity(event("stop", "command_completed", item="B", status="interrupted"))
    pane.append_activity(event("unknown", "command_completed", item="C"))
    a, b, c = pane.command_output.panels.values()
    assert "completed App Server item" in a.note_label.text()
    assert "truncated" in a.note_label.text()
    assert "Empty output" in b.note_label.text()
    assert "Interrupted" in b.toggle_button.text()
    assert "Unknown" in c.toggle_button.text()
    assert "not observed" in c.note_label.text()


def test_output_updates_preserve_selection_scroll_and_other_expansion(pane):
    pane.append_activity(event("long", text="line\n" * 300))
    pane.append_activity(event("other", item="B", text="B"))
    a, b = pane.command_output.panels.values()
    a.toggle_button.click()
    pane.resize(700, 800)
    pane.show()
    QApplication.processEvents()
    cursor = a.output_text.textCursor()
    cursor.setPosition(10)
    cursor.setPosition(20, QTextCursor.MoveMode.KeepAnchor)
    a.output_text.setTextCursor(cursor)
    a.output_text.verticalScrollBar().setValue(3)
    pane.command_output.verticalScrollBar().setValue(0)
    pane.append_activity(event("other2", item="B", text="B2"))
    pane.append_activity(event("same", text="tail"))
    QTest.qWait(100)
    assert a.toggle_button.isChecked() and not b.toggle_button.isChecked()
    assert a.output_text.textCursor().selectedText() == "line\u2029line\u2029"
    assert a.output_text.verticalScrollBar().value() == 3
    assert pane.command_output.verticalScrollBar().value() == 0


def test_retention_caps_output_characters_blocks_and_command_count(pane):
    for i in range(140):
        pane.append_activity(event(f"large{i}", text="x" * 2000))
    panels = pane.command_output.panels
    assert sum(len(p.output) for p in panels.values()) <= 256000
    assert "omitted" in next(iter(panels.values())).note_label.text()
    for i in range(10):
        pane.append_activity(event(f"lines{i}", text="\n" * 2000))
    assert sum(p.output.count("\n") + 1 for p in panels.values()) <= 2000
    for i in range(110):
        pane.append_activity(event(f"command{i}", item=f"item{i}", text="tail"))
    assert len(panels) <= 100
    assert "omitted" in pane.command_output.retention_label.text()


def test_thread_switch_resets_only_on_explicit_selection_reset(pane):
    first = event("first", text="first")
    pane.set_snapshot({"thread_id": "thread", "recent_activities": [first]}, reset=True)
    pane.set_error("disconnected")
    pane.set_snapshot({"thread_id": "thread", "recent_activities": [first]})
    foreign = event("foreign", text="foreign")
    foreign["thread_id"] = "other"
    pane.append_activity(foreign)
    assert len(pane.command_output.panels) == 1
    pane.set_empty_state("Select thread")
    pane.set_snapshot({"thread_id": "other", "recent_activities": [foreign]}, reset=True)
    (panel,) = pane.command_output.panels.values()
    assert panel.output == "foreign"


def test_foreign_snapshot_cannot_mix_into_selected_thread(pane):
    pane.set_snapshot({"thread_id": "thread", "recent_activities": [event("own", text="own")]})
    foreign = event("foreign", text="foreign")
    foreign["thread_id"] = "other"
    pane.set_snapshot({"thread_id": "other", "recent_activities": [foreign]})
    assert len(pane.command_output.panels) == 1
    assert pane.command_output.panels[("thread", "turn", "A")].output == "own"


def test_output_first_uses_observed_running_state_without_overriding_completion(pane):
    pane.append_activity(event("output-first", text="running", status="in_progress"))
    (panel,) = pane.command_output.panels.values()
    assert "Running" in panel.toggle_button.text()
    pane.append_activity(
        event("done", "command_completed", text="cmd", status="failed", exit_code=2)
    )
    pane.append_activity(event("late-output", text="tail", status="in_progress"))
    assert "Failed" in panel.toggle_button.text()
    assert panel.output == "runningtail"


def test_long_command_header_fits_right_pane_and_keeps_status_visible(pane):
    pane.append_activity(
        event(
            "long-command",
            "command_started",
            text="python " + "argument " * 40,
            status="in_progress",
        )
    )
    (panel,) = pane.command_output.panels.values()
    pane.resize(340, 800)
    pane.show()
    QApplication.processEvents()
    assert panel.toggle_button.width() <= pane.command_output.viewport().width()
    assert "Running" in panel.toggle_button.text()
    assert (
        panel.toggle_button.fontMetrics().horizontalAdvance(panel.toggle_button.text())
        < panel.toggle_button.width()
    )


def test_collapsed_output_defers_layout_until_expanded_and_keeps_received_text(pane):
    pane.append_activity(event("first", text="first\n"))
    (panel,) = pane.command_output.panels.values()
    assert not panel.output_text.document().isLayoutEnabled()
    pane.append_activity(event("second", text="second"))
    panel.toggle_button.click()
    assert panel.output_text.document().isLayoutEnabled()
    assert panel.output_text.toPlainText() == "first\nsecond"
    panel.toggle_button.click()
    assert not panel.output_text.document().isLayoutEnabled()


def test_open_output_coalesces_burst_and_expansion_immediately_renders_latest(pane):
    pane.append_activity(event("first", text="first"))
    (panel,) = pane.command_output.panels.values()
    panel.toggle_button.click()
    assert panel.output_text.toPlainText() == "first"
    for index in range(40):
        pane.append_activity(event(f"burst{index}", text="x" * 2000))
    assert panel.output == "first" + "x" * 80000
    assert panel.output_text.toPlainText() == "first"
    QTest.qWait(100)
    assert panel.output_text.toPlainText() == "first" + "x" * 80000
    panel.toggle_button.click()
    pane.append_activity(event("closed-tail", text="tail"))
    panel.toggle_button.click()
    assert panel.output_text.toPlainText().endswith("tail")
