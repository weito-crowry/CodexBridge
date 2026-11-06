from __future__ import annotations

import pytest
from PySide6.QtGui import QTextFormat
from PySide6.QtWidgets import QApplication, QPushButton, QTextBrowser

from codex_bridge.console.conversation_widgets import ConversationTextBody
from codex_bridge.console.widgets import HistoryPane, TimelineEntry, timeline_entries
from tests.test_console_widgets import _process_layout


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_message_soft_breaks_are_lines_within_a_paragraph(newline: str) -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    body = ConversationTextBody(
        newline.join(["一行目 **強調** 😀", "二行目", "", "次の段落"]), markdown=True
    )

    # A soft break is a line separator, while a blank line starts a paragraph.
    assert body.document().toRawText() == "一行目 強調 😀\u2028二行目\u2029次の段落"
    assert body.document().blockCount() == 2
    assert body.document().begin().next().blockFormat().topMargin() > 0
    assert body.heightForWidth(60) > body.heightForWidth(800)
    assert body._measurement_document.toRawText() == body.document().toRawText()
    body.close()


def test_message_breaks_keep_markdown_lists_code_tables_and_links() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    original = (
        "# 見出し\n\n"
        "- 項目 **強調**\n  続き\n- 二項目\n\n"
        "1. 番号\n   続き\n2. 次\n\n"
        "[リンク](https://example.invalid/path) と `inline`\n二行目\n\n"
        "```python\nvalue = 1\n\nprint(value)\n```\n\n"
        "| 列A | 列B |\n| --- | --- |\n| 値A | 値B |"
    )
    body = ConversationTextBody(original, markdown=True)
    html = body.document().toHtml()

    assert "<h1 " in html
    assert "<ul " in html and "<ol " in html
    assert html.count("<li ") == 4  # Continuation lines must not become extra bullets.
    assert "font-weight:700" in html
    assert '<a href="https://example.invalid/path">' in html
    assert "<table " in html
    assert "項目 強調\u2028続き" in body.document().toRawText()
    assert "リンク と inline\n二行目" in body.toPlainText()
    assert "value = 1\n\nprint(value)" in body.toPlainText()
    code_block = body.document().find("value = 1").block()
    assert code_block.blockFormat().property(QTextFormat.Property.BlockCodeLanguage) == "python"
    body.close()


@pytest.mark.parametrize(
    ("item_type", "phase"),
    [("userMessage", None), ("agentMessage", None), ("agentMessage", "commentary"), ("plan", None)],
)
def test_history_projection_keeps_complete_message_source(
    item_type: str, phase: str | None
) -> None:
    original = "# 指示\r\n\r\n" + "長い日本語 ABC **原文**\r\n" * 1000 + "末尾"
    entries = timeline_entries(
        {
            "items": [
                {
                    "turn_id": "turn",
                    "item": {"id": "message", "type": item_type, "phase": phase, "text": original},
                }
            ]
        }
    )

    assert len(original) > 16_384
    assert entries[0].body == original


@pytest.mark.parametrize("mode", ["Conversation", "Messages", "Details"])
def test_history_modes_copy_complete_source_and_update_without_scroll_jump(mode: str) -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    original = "# 指示\r\n\r\n" + "日本語 ABC **強調** " * 1100 + "\r\n次の行\r\n\r\n末尾"

    def entries(text: str) -> tuple[TimelineEntry, ...]:
        return timeline_entries(
            {
                "items": [
                    {"turn_id": "turn", "item": {"id": "user", "type": "userMessage", "text": text}}
                ]
            }
        )

    pane = HistoryPane()
    pane.resize(360, 460)
    pane.set_mode(mode)
    pane.show()
    pane.set_timeline(entries(original))
    _process_layout(application)
    scrollbar = pane._scroll.verticalScrollBar()
    assert scrollbar.maximum() > 0
    scrollbar.setValue(scrollbar.maximum() // 2)
    _process_layout(application)
    before = scrollbar.value()
    pane.set_timeline(entries(original + "\r\n追記"))
    _process_layout(application)

    row = pane._timeline_row_widgets[("card", "turn", "user")]
    body = row.findChild(QTextBrowser)
    button = row.findChild(
        QPushButton, "copyMessageButton" if mode == "Details" else "conversationCopyMessageButton"
    )
    assert body is not None and button is not None
    assert "末尾\n追記" in body.toPlainText()
    if mode == "Details":
        assert body.toPlainText() == (original + "\r\n追記").replace("\r\n", "\n")
    else:
        assert "次の行" in body.toPlainText() and "**強調**" not in body.toPlainText()
    button.click()
    assert application.clipboard().text() == original + "\r\n追記"
    assert abs(scrollbar.value() - before) <= 3
    assert pane._scroll.horizontalScrollBar().maximum() == 0
    assert body.horizontalScrollBar().maximum() == 0
    pane.close()
