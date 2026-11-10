from __future__ import annotations

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton
from shiboken6 import isValid

from codex_bridge.console import widget_helpers, widgets
from codex_bridge.console.widgets import HistoryPane, TimelineEntry
from tests.test_console_widgets import _process_layout


def _copy_button(pane: HistoryPane) -> QPushButton:
    row = pane._timeline_row_widgets[("card", "turn", "message")]
    button = row.findChild(QPushButton)
    assert button is not None
    return button


@pytest.mark.parametrize("mode", ["Conversation", "Messages", "Details"])
def test_copy_success_preserves_source_and_button_geometry_then_resets(mode: str) -> None:
    application = QApplication.instance() or QApplication([])
    assert isinstance(application, QApplication)
    original = "# 日本語 **原文**\r\n\r\n" + "長文 😀\r\n" * 3000 + "末尾"
    pane = HistoryPane()
    pane.set_mode(mode)
    pane.set_timeline((TimelineEntry("turn", "message", "User", "User", original, None, ()),))
    pane.resize(460, 400)
    pane.show()
    _process_layout(application)
    button = _copy_button(pane)
    geometry = button.geometry()
    hint = button.sizeHint()
    assert button.text() == "Copy"

    button.setFocus()
    QTest.keyClick(button, Qt.Key.Key_Space)
    _process_layout(application)

    assert application.clipboard().text() == original
    assert button.text() == "✓"
    assert "Copied" in button.accessibleName()
    assert "Copied" in button.toolTip()
    assert button.geometry() == geometry
    assert button.sizeHint() == hint
    QTest.qWait(2100)
    assert button.text() == "Copy"
    assert "Copy" in button.accessibleName()
    assert "Copied" not in button.toolTip()
    assert button.geometry() == geometry
    pane.close()


def test_repeated_copy_restarts_feedback_time() -> None:
    application = QApplication.instance() or QApplication([])
    assert isinstance(application, QApplication)
    pane = HistoryPane()
    pane.set_timeline((TimelineEntry("turn", "message", "User", "User", "source", None, ()),))
    button = _copy_button(pane)
    button.click()
    QTest.qWait(1100)
    application.clipboard().setText("changed elsewhere")
    button.click()
    assert application.clipboard().text() == "source"
    QTest.qWait(1100)
    assert button.text() == "✓"
    QTest.qWait(1000)
    assert button.text() == "Copy"
    pane.close()


@pytest.mark.parametrize("mode", ["Conversation", "Messages", "Details"])
@pytest.mark.parametrize("result", [False, None, RuntimeError("clipboard failed")])
def test_failed_copy_never_shows_success(
    mode: str, result: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = QApplication.instance() or QApplication([])
    assert isinstance(application, QApplication)

    def fail_copy(_text: str) -> object:
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(widgets, "copy_to_clipboard", fail_copy)
    pane = HistoryPane()
    pane.set_mode(mode)
    pane.set_timeline((TimelineEntry("turn", "message", "User", "User", "source", None, ()),))
    button = _copy_button(pane)
    button.click()
    assert button.text() == "Copy"
    assert "Copied" not in button.toolTip()
    pane.close()


def test_failed_recopy_clears_previous_success(monkeypatch: pytest.MonkeyPatch) -> None:
    application = QApplication.instance() or QApplication([])
    assert isinstance(application, QApplication)
    results = iter([True, False])
    monkeypatch.setattr(widgets, "copy_to_clipboard", lambda _text: next(results))
    pane = HistoryPane()
    pane.set_timeline((TimelineEntry("turn", "message", "User", "User", "source", None, ()),))
    button = _copy_button(pane)
    button.click()
    assert button.text() == "✓"
    button.click()
    assert button.text() == "Copy"
    pane.close()


@pytest.mark.parametrize("change", ["redraw", "switch", "clear", "close"])
def test_pending_feedback_is_disposed_with_card(
    change: str, capsys: pytest.CaptureFixture[str]
) -> None:
    application = QApplication.instance() or QApplication([])
    assert isinstance(application, QApplication)
    pane = HistoryPane()
    entry = TimelineEntry("turn", "message", "User", "User", "source", None, ())
    pane.set_timeline((entry,))
    button = _copy_button(pane)
    button.click()
    assert button.text() == "✓"
    if change == "redraw":
        pane.set_timeline((TimelineEntry("turn", "message", "User", "User", "updated", None, ()),))
    elif change == "switch":
        pane.set_mode("Details")
    elif change == "clear":
        pane.set_empty_state("Other thread")
        pane.set_timeline((entry,))
    else:
        pane.close()
        pane.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not isValid(button)
    QTest.qWait(2100)
    if change != "close":
        assert _copy_button(pane).text() == "Copy"
        pane.close()
    assert "Internal C++ object" not in capsys.readouterr().err


@pytest.mark.parametrize("failure", ["unavailable", "unchanged", "write_error", "read_error"])
def test_clipboard_helper_requires_successful_readback(
    failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = QApplication.instance() or QApplication([])
    assert isinstance(application, QApplication)

    class Clipboard:
        def setText(self, _text: str) -> None:  # noqa: N802
            if failure == "write_error":
                raise RuntimeError("write failed")

        def text(self) -> str:
            if failure == "read_error":
                raise RuntimeError("read failed")
            return "previous text"

    clipboard = None if failure == "unavailable" else Clipboard()
    monkeypatch.setattr(QApplication, "clipboard", staticmethod(lambda: clipboard))
    assert widget_helpers.copy_to_clipboard("source") is False


def test_clipboard_helper_reports_verified_success() -> None:
    application = QApplication.instance() or QApplication([])
    assert isinstance(application, QApplication)
    assert widget_helpers.copy_to_clipboard("原文\r\n\n**Markdown**") is True
    assert application.clipboard().text() == "原文\r\n\n**Markdown**"
