from __future__ import annotations

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtGui import QInputMethodEvent, QTextCursor, QTextOption
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QVBoxLayout

from codex_bridge.console.composer_widget import ComposerInput
from codex_bridge.console.config import ConsoleConfig
from codex_bridge.console.main_window import MainWindow
from tests.console_main_window_test_support import FakeClient, _application


def test_enter_sends_once_and_shift_enter_inserts_a_new_line() -> None:
    _application()
    input_box = ComposerInput()
    sent: list[bool] = []
    input_box.send_requested.connect(lambda: sent.append(True))
    input_box.setPlainText("first line")
    input_box.moveCursor(QTextCursor.MoveOperation.End)

    QTest.keyClick(input_box, Qt.Key.Key_Return)
    assert sent == [True]
    assert input_box.toPlainText() == "first line"

    QTest.keyClick(input_box, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    assert sent == [True]
    assert input_box.toPlainText() == "first line\n"


def test_ime_preedit_enter_does_not_send() -> None:
    _application()
    input_box = ComposerInput()
    sent: list[bool] = []
    input_box.send_requested.connect(lambda: sent.append(True))

    QCoreApplication.sendEvent(input_box, QInputMethodEvent("にほん", []))
    QTest.keyClick(input_box, Qt.Key.Key_Return)

    assert sent == []


def test_composer_wraps_long_text_and_remains_outside_history_scroll_area() -> None:
    _application()
    window = MainWindow(ConsoleConfig(), api_client=FakeClient(), tray_available=False)
    try:
        assert window.composer_widget.input.document().defaultTextOption().wrapMode() == (
            QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere
        )
        assert window.composer_widget.input.maximumHeight() <= 180
        window.composer_widget.input.setPlainText("very long path " * 400)
        window.history_pane.set_mode("Details")

        conversation_pane = window.history_pane.parentWidget()
        layout = conversation_pane.layout()
        assert isinstance(layout, QVBoxLayout)
        assert layout.itemAt(0).widget() is window.history_pane
        assert layout.itemAt(1).widget() is window.composer_widget
        assert window.composer_widget.input.toPlainText() == "very long path " * 400
    finally:
        window.close()
