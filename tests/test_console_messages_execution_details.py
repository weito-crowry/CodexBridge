from __future__ import annotations

from dataclasses import replace

import pytest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QTextBrowser, QWidget

from codex_bridge.console.appearance import stylesheet_for_theme, theme_for_scheme
from codex_bridge.console.widgets import HistoryPane, TimelineEntry
from tests.test_console_widgets import _process_layout


def _row(pane: HistoryPane, item_id: str, turn_id: str = "turn") -> QWidget:
    return pane._timeline_row_widgets[("card", turn_id, item_id)]


def _visible_items(pane: HistoryPane) -> list[str]:
    return [
        row.key[-1]
        for row in pane._timeline_rows
        if row.kind == "card" and not pane._timeline_row_widgets[row.key].isHidden()
    ]


def test_messages_execution_toggle_preserves_order_and_individual_disclosures() -> None:
    application = QApplication.instance() or QApplication([])
    pane = HistoryPane()
    pane.resize(660, 500)
    pane.show()
    entries = tuple(
        TimelineEntry("turn", item_id, kind, kind, body, status, details)
        for item_id, kind, body, status, details in (
            ("user", "User", "question", None, ()),
            ("commentary", "Commentary", "working", None, ()),
            ("command", "Command", "pytest\n<script>plain</script>", "failed", ("exit 2",)),
            ("tool", "MCP", "server / lookup", "completed", ()),
            ("dynamic", "Dynamic tool", "scan", "interrupted", ()),
            ("function", "Function", "result", None, ()),
            ("collab", "Collaboration", "wait", "completed", ()),
            ("subagent", "Sub-agent", "finished", None, ()),
            ("plan", "Plan", "plan", None, ()),
            ("context", "Context", "compaction", None, ()),
            ("agent", "Agent", "answer", None, ()),
        )
    )
    pane.set_timeline(entries)
    assert pane.execution_details_button.isHidden()
    pane.set_mode("Messages")
    _process_layout(application)
    assert not pane.execution_details_button.isHidden()
    assert not pane.execution_details_button.isChecked()
    assert _visible_items(pane) == ["user", "commentary", "agent"]
    message_row = _row(pane, "agent")
    pane.execution_details_button.click()
    _process_layout(application)
    assert _visible_items(pane) == [
        "user",
        "commentary",
        "command",
        "tool",
        "dynamic",
        "function",
        "collab",
        "subagent",
        "agent",
    ]
    command = _row(pane, "command")
    tool = _row(pane, "tool")
    command_content = command.findChild(QWidget, "conversationDisclosureContent")
    tool_content = tool.findChild(QWidget, "conversationDisclosureContent")
    assert command_content is not None and command_content.isHidden()
    assert tool_content is not None and tool_content.isHidden()
    disclosure = command.findChild(QPushButton, "conversationDisclosure")
    assert disclosure is not None
    disclosure.click()
    assert not command_content.isHidden()
    assert tool_content.isHidden()
    body = command.findChild(QTextBrowser, "conversationDetailsBody")
    assert body is not None and body.isReadOnly()
    assert body.toPlainText() == "pytest\n<script>plain</script>"
    metadata = command.findChild(QLabel, "conversationEventMetadata")
    assert metadata is not None and "Failed" in metadata.text() and "exit 2" in metadata.text()
    pane.execution_details_button.click()
    assert _visible_items(pane) == ["user", "commentary", "agent"]
    pane.execution_details_button.click()
    assert _row(pane, "command") is command
    assert _row(pane, "agent") is message_row
    assert not command_content.isHidden()
    assert [row.value for row in pane._timeline_rows if row.kind == "card"] == list(entries)
    pane.set_mode("Details")
    assert pane.execution_details_button.isHidden()
    assert len(_visible_items(pane)) == len(entries)
    pane.set_mode("Conversation")
    assert pane.execution_details_button.isHidden()
    assert len(_visible_items(pane)) == len(entries)
    pane.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_execution_details_live_prepend_copy_and_thread_switch(scheme: str) -> None:
    application = QApplication.instance() or QApplication([])
    pane = HistoryPane()
    pane.setStyleSheet(stylesheet_for_theme(theme_for_scheme(scheme)))
    pane.set_mode("Messages")
    pane.execution_details_button.click()
    pane.resize(500, 430)
    pane.show()
    running = TimelineEntry("turn", "command", "Command", "Command", "pytest", "in_progress", ())
    answer = TimelineEntry("turn", "answer", "Agent", "Agent", "original\n**answer**", None, ())
    pane.set_timeline((running, answer))
    _process_layout(application)
    original_answer = _row(pane, "answer")
    command = _row(pane, "command")
    command.findChild(QPushButton, "conversationDisclosure").click()
    spinner = command.findChild(QWidget, "historyRunningSpinner")
    assert spinner is not None and spinner.isVisible()
    failed = replace(running, status="failed", details=("exit 3",))
    pane.set_timeline((failed, answer))
    _process_layout(application)
    updated_command = _row(pane, "command")
    assert not updated_command.findChild(QWidget, "conversationDisclosureContent").isHidden()
    assert updated_command.findChild(QWidget, "historyRunningSpinner") is None
    assert _row(pane, "answer") is original_answer
    copy_button = original_answer.findChild(QPushButton, "conversationCopyMessageButton")
    assert copy_button is not None
    copy_button.click()
    assert application.clipboard().text() == "original\n**answer**"
    older = TimelineEntry("old-turn", "old-tool", "MCP", "MCP", "old tool", "completed", ())
    pane.set_timeline((older, failed, answer), prepend=True)
    _process_layout(application)
    assert _visible_items(pane) == ["old-tool", "command", "answer"]
    assert _row(pane, "command") is updated_command
    pane.set_empty_state("Loading")
    pane.set_timeline((replace(running, turn_id="other-turn"),))
    _process_layout(application)
    assert _visible_items(pane) == ["command"]
    assert (
        _row(pane, "command", "other-turn")
        .findChild(QWidget, "conversationDisclosureContent")
        .isHidden()
    )
    assert pane._scroll.horizontalScrollBar().maximum() == 0
    pane.close()


def test_messages_execution_toggle_preserves_reading_anchor_and_bottom_follow() -> None:
    application = QApplication.instance() or QApplication([])
    pane = HistoryPane()
    pane.set_mode("Messages")
    pane.resize(600, 430)
    pane.show()
    entries = tuple(
        TimelineEntry("turn", f"{kind}-{index}", kind, kind, "body\n" * 6, None, ())
        for index in range(20)
        for kind in ("Agent", "Command")
    )
    pane.set_timeline(entries)
    _process_layout(application)
    scrollbar = pane._scroll.verticalScrollBar()
    pane.execution_details_button.click()
    _process_layout(application)
    assert pane._follow_newest and scrollbar.value() == scrollbar.maximum()
    pane.execution_details_button.click()
    _process_layout(application)
    scrollbar.setValue(scrollbar.maximum() // 2)
    _process_layout(application)
    anchor = pane._visible_scroll_anchor()
    assert anchor is not None
    key, offset = anchor
    pane.execution_details_button.click()
    _process_layout(application)
    assert abs(pane._timeline_row_widgets[key].y() - scrollbar.value() - offset) <= 3
    pane.execution_details_button.click()
    _process_layout(application)
    assert abs(pane._timeline_row_widgets[key].y() - scrollbar.value() - offset) <= 3
    assert not pane._follow_newest
    pane.close()


def test_execution_toggle_does_not_widen_the_history_pane() -> None:
    application = QApplication.instance() or QApplication([])
    pane = HistoryPane()
    pane.resize(420, 430)
    pane.show()
    _process_layout(application)
    original_minimum = pane.minimumSizeHint().width()
    pane.set_mode("Messages")
    _process_layout(application)
    assert pane.minimumSizeHint().width() <= original_minimum
    assert pane.execution_details_button.geometry().right() < pane.width()
    pane.close()
