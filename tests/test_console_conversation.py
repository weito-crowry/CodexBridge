from __future__ import annotations

import pytest
from PySide6.QtCore import QUrl
from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QPushButton, QTextBrowser, QWidget

from codex_bridge.console.appearance import stylesheet_for_theme, theme_for_scheme
from codex_bridge.console.widgets import ActivityPane, HistoryPane, TimelineEntry
from tests.test_console_widgets import _process_layout


def test_history_defaults_to_conversation_and_switches_without_changing_timeline() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = HistoryPane()
    entries = (
        TimelineEntry("turn-1", "user-1", "User", "User", "\u8cea\u554f", None, ()),
        TimelineEntry("turn-1", "agent-1", "Agent", "Agent", "\u56de\u7b54", None, ()),
    )
    pane.set_timeline(entries)
    row_keys = tuple(row.key for row in pane._timeline_rows)

    assert pane.mode == "Conversation"
    pane.set_mode("Details")
    assert pane.mode == "Details"
    assert [card.objectName() for card in pane.findChildren(QFrame, "historyCard")] == [
        "historyCard",
        "historyCard",
    ]
    pane.set_mode("Conversation")
    assert pane.mode == "Conversation"
    assert tuple(row.key for row in pane._timeline_rows) == row_keys
    assert [row.value for row in pane._timeline_rows if row.kind == "card"] == list(entries)
    pane.close()


def test_mode_switch_preserves_the_visible_history_anchor() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    entries = tuple(
        TimelineEntry("turn", f"item-{index}", "Agent", "Agent", "answer\n" * 8, None, ())
        for index in range(24)
    )
    pane = HistoryPane()
    pane.resize(600, 460)
    pane.show()
    pane.set_timeline(entries)
    _process_layout(application)
    scrollbar = pane._scroll.verticalScrollBar()
    scrollbar.setValue(scrollbar.maximum() // 2)
    _process_layout(application)
    anchor = pane._visible_scroll_anchor()
    assert anchor is not None
    key, offset = anchor

    pane.set_mode("Details")
    _process_layout(application)

    widget = pane._timeline_row_widgets[key]
    assert abs((widget.geometry().top() - scrollbar.value()) - offset) <= 3
    assert not pane._follow_newest
    pane.close()


@pytest.mark.parametrize("mode", ["Conversation", "Details"])
def test_load_older_prepends_in_order_and_reuses_rows_in_both_modes(mode: str) -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    current = tuple(
        TimelineEntry("turn-new", f"new-{index}", "Agent", "Agent", "answer\n" * 5, None, ())
        for index in range(12)
    )
    older = (
        TimelineEntry("turn-old", "old-user", "User", "User", "older question", None, ()),
        TimelineEntry("turn-old", "old-agent", "Agent", "Agent", "older answer", None, ()),
    )
    pane = HistoryPane()
    if mode == "Details":
        pane.set_mode("Details")
    pane.resize(600, 460)
    pane.show()
    pane.set_timeline(current, has_older=True)
    _process_layout(application)
    pane._scroll.verticalScrollBar().setValue(0)
    _process_layout(application)
    assert pane.load_older_button.isVisible()
    requested: list[bool] = []
    pane.older_requested.connect(lambda: requested.append(True))
    pane.load_older_button.click()
    assert requested == [True]
    original_rows = {
        entry.item_id: pane._timeline_row_widgets[("card", entry.turn_id, entry.item_id)]
        for entry in current
    }

    pane.set_timeline((*older, *current), has_older=False, prepend=True)
    _process_layout(application)

    card_entries = [row.value for row in pane._timeline_rows if row.kind == "card"]
    assert card_entries == [*older, *current]
    for entry in current:
        assert (
            pane._timeline_row_widgets[("card", entry.turn_id, entry.item_id)]
            is original_rows[entry.item_id]
        )
    assert not pane.load_older_button.isVisible()
    pane.close()


def test_follow_newest_stays_at_bottom_when_conversation_appends() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    entries = tuple(
        TimelineEntry("turn", f"item-{index}", "Agent", "Agent", "answer\n" * 6, None, ())
        for index in range(16)
    )
    pane = HistoryPane()
    pane.resize(560, 440)
    pane.show()
    pane.set_timeline(entries)
    _process_layout(application)
    scrollbar = pane._scroll.verticalScrollBar()
    assert pane._follow_newest
    assert scrollbar.value() == scrollbar.maximum()

    new_entry = TimelineEntry("turn", "item-new", "Agent", "Agent", "latest", None, ())
    pane.set_timeline((*entries, new_entry))
    _process_layout(application)

    assert pane._follow_newest
    assert scrollbar.value() == scrollbar.maximum()
    pane.close()


def test_conversation_keeps_all_timeline_kinds_ordered_and_separates_commentary() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    kinds = (
        "User",
        "Agent",
        "Commentary",
        "Plan",
        "Command",
        "Files",
        "MCP",
        "Dynamic tool",
        "Function",
        "Collaboration",
        "Sub-agent",
        "Image",
        "Context",
        "Review",
    )
    entries = tuple(
        TimelineEntry("turn", f"item-{index}", kind, kind, f"body {kind}", None, ())
        for index, kind in enumerate(kinds)
    )
    pane = HistoryPane()
    pane.set_timeline(entries)

    rows = [pane._timeline_row_widgets[("card", "turn", entry.item_id)] for entry in entries]

    assert [row.objectName() for row in rows[:4]] == [
        "conversationUser",
        "conversationAgent",
        "conversationCommentary",
        "conversationPlan",
    ]
    assert all(row.objectName() == "conversationEvent" for row in rows[4:])
    assert [row.findChild(QLabel, "conversationMessageKind").text() for row in rows[:4]] == list(
        kinds[:4]
    )
    assert [row.key for row in pane._timeline_rows if row.kind == "card"] == [
        ("card", "turn", entry.item_id) for entry in entries
    ]
    pane.close()


def test_conversation_styles_follow_light_and_dark_appearance_tokens() -> None:
    for theme in (theme_for_scheme("light"), theme_for_scheme("dark")):
        stylesheet = stylesheet_for_theme(theme)

        assert "QFrame#conversationUser" in stylesheet
        assert "QFrame#conversationAgent" in stylesheet
        assert "QFrame#conversationCommentary" in stylesheet
        assert f"border-left: 3px solid {theme.tokens.link.name()}" in stylesheet
        assert f"border-left: 3px solid {theme.tokens.selection.name()}" in stylesheet


def test_activity_pane_reports_approval_and_input_requests_without_changing_controls() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = ActivityPane()
    states: list[tuple[bool, str]] = []
    pane.pending_state_changed.connect(lambda pending, label: states.append((pending, label)))
    history = HistoryPane()
    pane.pending_state_changed.connect(history.set_pending_request_state)

    pane.set_snapshot(
        {
            "state": "needs_approval",
            "pending_request": {
                "method": "item/commandExecution/requestApproval",
                "request_id": 12,
                "summary": "Run a command",
            },
        }
    )
    assert states[-1] == (True, "Approval required")
    assert pane.approval_request_id == 12
    assert pane.approval_buttons_widget.isHidden() is False

    pane.set_snapshot(
        {
            "state": "waiting_for_input",
            "pending_request": {"method": "item/requestUserInput", "summary": "Choose"},
        }
    )
    assert states[-1] == (True, "Input required")
    assert pane.approval_request_id is None
    assert pane.approval_buttons_widget.isHidden()
    history.set_activity_visible(False)
    assert history.pending_notice_label.text() == "Input required"
    assert history.activity_toggle_button.accessibleName() == "Input required — Show Activity"

    pane.set_snapshot({"state": "ready"})
    assert states[-1] == (False, "")
    assert history.pending_notice_label.text() == ""
    history.close()
    pane.close()


def test_conversation_markdown_is_safe_and_copy_keeps_original_text() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    original = (
        "# \u65e5\u672c\u8a9e\n\n- \u30ea\u30b9\u30c8 **\u592a\u5b57**\n\n"
        "```text\n\u5b89\u5168\u306a\u30b3\u30fc\u30c9\n" + "x" * 500 + "\n```\n\n"
        "<script>alert('x')</script>\n\n"
        "![remote](https://example.invalid/image.png)\n\n"
        "[link](https://example.invalid/never-open)"
    )
    pane = HistoryPane()
    pane.resize(360, 700)
    pane.show()
    pane.set_timeline((TimelineEntry("turn", "agent", "Agent", "Agent", original, None, ()),))
    _process_layout(application)
    row = pane._timeline_row_widgets[("card", "turn", "agent")]
    body = row.findChild(QTextBrowser, "conversationTextBody")
    copy_button = row.findChild(QPushButton, "conversationCopyMessageButton")

    assert body is not None
    assert "<h1 " in body.document().toHtml()
    assert "<ul " in body.document().toHtml()
    assert "x" * 100 in body.toPlainText()
    assert "<script" not in body.document().toHtml().casefold()
    blocks = []
    block = body.document().begin()
    while block.isValid():
        blocks.append((block.text()[:24], block.blockFormat().nonBreakableLines()))
        block = block.next()
    document_width = body.document().documentLayout().documentSize().width()
    assert document_width <= body.width() + 2, (
        f"document_width={document_width}, body_width={body.width()}, blocks={blocks!r}"
    )
    assert body.horizontalScrollBar().maximum() == 0
    assert not body.openLinks()
    assert not body.openExternalLinks()
    assert (
        body.document().resource(
            QTextDocument.ResourceType.ImageResource, QUrl("https://example.invalid/image.png")
        )
        is None
    )
    assert copy_button is not None
    copy_button.click()
    assert application.clipboard().text() == original
    pane.close()


def test_conversation_events_disclose_only_sanitized_body_and_keep_expansion_across_updates() -> (
    None
):
    application = QApplication.instance() or QApplication([])
    assert application is not None
    command = "pwsh -NoProfile " + "x" * 260 + "\nWrite-Output 'safe command'"
    entry = TimelineEntry(
        "turn-1",
        "command-1",
        "Command",
        "Command",
        command,
        "failed",
        ("exit 2", "duration 1.2s"),
        1_791_518_400_000,
        1_791_518_401_200,
    )
    pane = HistoryPane()
    pane.resize(430, 500)
    pane.show()
    pane.set_timeline((entry,))
    _process_layout(application)
    row = pane._timeline_row_widgets[("card", "turn-1", "command-1")]
    summary = row.findChild(QLabel, "conversationEventSummary")
    disclosure = row.findChild(QPushButton, "conversationDisclosure")
    assert summary is not None and len(summary.text()) <= 180
    assert disclosure is not None and disclosure.text() == "Show details"
    metadata = row.findChild(QLabel, "conversationEventMetadata")
    assert metadata is not None
    assert "Failed" in metadata.text()
    assert "exit 2" in metadata.text()
    assert "duration 1.2s" in metadata.text()
    collapsed_body = row.findChild(QTextBrowser, "conversationDetailsBody")
    details_container = row.findChild(QWidget, "conversationDisclosureContent")
    assert collapsed_body is not None and not collapsed_body.isVisible()
    assert details_container is not None and not details_container.isVisible()

    disclosure.click()
    detail_body = row.findChild(QTextBrowser, "conversationDetailsBody")
    assert detail_body is not None and detail_body.toPlainText() == command
    pane.set_timeline((entry, TimelineEntry("turn-1", "agent", "Agent", "Agent", "done", None, ())))
    pane.set_mode("Details")
    pane.set_mode("Conversation")
    row = pane._timeline_row_widgets[("card", "turn-1", "command-1")]
    assert row.findChild(QTextBrowser, "conversationDetailsBody") is not None
    pane.resize(340, 500)
    _process_layout(application)
    assert pane._scroll.horizontalScrollBar().maximum() == 0
    expanded_body = row.findChild(QTextBrowser, "conversationDetailsBody")
    assert expanded_body is not None and expanded_body.horizontalScrollBar().maximum() == 0
    pane.set_timeline(
        (TimelineEntry("turn-2", "new-command", "Command", "Command", "new", None, ()),)
    )
    replacement = pane._timeline_row_widgets[("card", "turn-2", "new-command")]
    assert replacement.findChild(QWidget, "conversationDisclosureContent") is not None
    assert not replacement.findChild(QWidget, "conversationDisclosureContent").isVisible()
    assert pane._expanded_conversation_items == set()
    pane.close()


def test_conversation_running_event_finishes_and_new_items_reuse_existing_rows() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    entries = tuple(
        TimelineEntry("turn", f"item-{index}", "Agent", "Agent", "answer " * 20, None, ())
        for index in range(24)
    )
    running = TimelineEntry(
        "turn", "command", "Command", "Command", "pwsh -NoProfile", "in_progress", (), 1_000
    )
    pane = HistoryPane()
    pane.resize(520, 430)
    pane.show()
    pane.set_timeline((*entries, running))
    _process_layout(application)
    old_row = pane._timeline_row_widgets[("card", "turn", "item-4")]
    scrollbar = pane._scroll.verticalScrollBar()
    scrollbar.setValue(max(0, scrollbar.maximum() // 2))
    _process_layout(application)
    old_scroll = scrollbar.value()
    running_row = pane._timeline_row_widgets[("card", "turn", "command")]
    spinner = running_row.findChild(QWidget, "historyRunningSpinner")
    assert spinner is not None and spinner._timer.isActive()

    completed = TimelineEntry(
        "turn",
        "command",
        "Command",
        "Command",
        "pwsh -NoProfile",
        "completed",
        ("exit 0",),
        1_000,
        2_000,
    )
    pane.set_timeline((*entries, completed))
    _process_layout(application)

    assert pane._timeline_row_widgets[("card", "turn", "item-4")] is old_row
    assert scrollbar.value() > 0
    assert scrollbar.value() < scrollbar.maximum() - pane._BOTTOM_FOLLOW_THRESHOLD
    assert abs(scrollbar.value() - old_scroll) <= 2
    finished_row = pane._timeline_row_widgets[("card", "turn", "command")]
    assert finished_row.findChild(QWidget, "historyRunningSpinner") is None
    assert not spinner.isVisible()
    assert not spinner._timer.isActive()
    metadata = finished_row.findChild(QLabel, "conversationEventMetadata")
    assert metadata is not None and "completed" in metadata.text().lower()
    pane.close()
