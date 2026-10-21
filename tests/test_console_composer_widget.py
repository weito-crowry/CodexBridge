from __future__ import annotations

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtGui import QInputMethodEvent, QTextCursor, QTextOption
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QComboBox, QStyle, QStyleOptionComboBox, QVBoxLayout

from codex_bridge.console.composer_widget import ComposerInput, ComposerWidget
from codex_bridge.console.config import ConsoleConfig
from codex_bridge.console.main_window import MainWindow
from tests.console_main_window_test_support import FakeClient, _application


def _visible_combo_text_width(combo: QComboBox) -> int:
    option = QStyleOptionComboBox()
    option.initFrom(combo)
    option.rect = combo.rect()
    option.currentText = combo.currentText()
    option.editable = combo.isEditable()
    option.frame = combo.hasFrame()
    edit_field = combo.style().subControlRect(
        QStyle.ComplexControl.CC_ComboBox,
        option,
        QStyle.SubControl.SC_ComboBoxEditField,
        combo,
    )
    return edit_field.width()


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


def test_effort_combo_emits_change_signal() -> None:
    _application()
    widget = ComposerWidget()
    widget.set_draft_mode(True)
    widget.set_model_options([("Model A", "model-a")], "model-a", available=True)
    widget.set_effort_options([("Low", "low")], None)
    changes: list[bool] = []
    widget.effort_changed.connect(lambda: changes.append(True))

    widget.effort_combo.setCurrentIndex(widget.effort_combo.findData("low"))

    assert widget.selected_effort == "low"
    assert changes == [True]


def test_readonly_metadata_tooltips_are_replaced_and_cleared() -> None:
    _application()
    widget = ComposerWidget()

    widget.set_readonly_metadata(
        "Multiple",
        "Multiple",
        None,
        model_tooltip="history model source",
        effort_tooltip="history effort source",
    )

    assert widget.model_combo.itemText(0) == "Model: Multiple"
    assert widget.effort_combo.itemText(0) == "Effort: Multiple"
    assert widget.access_combo.itemText(0) == "Access: Unknown"
    assert widget.model_combo.toolTip() == "history model source"
    assert widget.effort_combo.toolTip() == "history effort source"

    widget.set_readonly_metadata("current-model", "high", "workspace-write")
    assert widget.model_combo.toolTip() == ""
    assert widget.effort_combo.toolTip() == ""


def test_footer_values_remain_fully_visible_at_ordinary_and_narrow_widths() -> None:
    app = _application()
    widget = ComposerWidget()
    widget.set_draft_mode(True)
    widget.set_model_options([("gpt-6.1-sol", "gpt-6.1-sol")], "gpt-6.1-sol", available=True)
    widget.set_effort_options([("extra high", "extra high")], "extra high")
    widget.set_access("danger-full-access")

    try:
        for width in (760, 420, 320, 260):
            widget.resize(width, 260)
            widget.show()
            app.processEvents()
            assert widget.width() == width
            assert widget._compact_controls is (width < 600)
            combos = (widget.access_combo, widget.model_combo, widget.effort_combo)
            for combo in combos:
                text_width = combo.fontMetrics().horizontalAdvance(combo.currentText())
                assert text_width <= _visible_combo_text_width(combo), (
                    f"{combo.objectName()} clips {combo.currentText()!r} at width {width}"
                )
            if width == 760:
                assert widget.model_combo.width() > max(
                    widget.access_combo.width(), widget.effort_combo.width()
                )
            for left, right in zip(combos, combos[1:], strict=False):
                assert not left.geometry().intersects(right.geometry())
            assert widget.controls_container.geometry().right() < widget.width()
            for control in (*combos, widget.send_button):
                assert control.geometry().right() < control.parentWidget().width(), (
                    f"{control.objectName()} extends beyond the composer at width {width}"
                )
    finally:
        widget.close()


def test_readonly_footer_metadata_updates_remain_visible_during_resize() -> None:
    app = _application()
    widget = ComposerWidget()
    emitted: list[str] = []
    widget.access_changed.connect(lambda: emitted.append("access"))
    widget.model_changed.connect(lambda: emitted.append("model"))
    widget.effort_changed.connect(lambda: emitted.append("effort"))
    widget.set_stop_state(visible=True, enabled=True)
    widget.set_send_enabled(True)
    model_hint_widths: list[int] = []

    try:
        for model in ("gpt-6.1-sol", "gpt-6-luna", "gpt-6.1-sol"):
            widget.set_readonly_metadata(
                model,
                "extra high",
                "danger-full-access",
                model_tooltip=f"history model: {model}",
                effort_tooltip=f"history effort: {model}",
            )
            for width in (760, 620, 600, 599, 420, 320, 260):
                widget.resize(width, 400)
                widget.show()
                app.processEvents()

                combos = (widget.access_combo, widget.model_combo, widget.effort_combo)
                assert tuple(combo.currentText() for combo in combos) == (
                    "Access: Full Access",
                    f"Model: {model}",
                    "Effort: extra high",
                )
                for combo in combos:
                    text_width = combo.fontMetrics().horizontalAdvance(combo.currentText())
                    assert text_width <= _visible_combo_text_width(combo), (
                        f"{combo.objectName()} clips {combo.currentText()!r} at width {width}"
                    )
                    assert not combo.isEnabled()
                assert widget.model_combo.toolTip() == f"history model: {model}"
                assert widget.effort_combo.toolTip() == f"history effort: {model}"
                assert widget.stop_button.isVisible() and widget.stop_button.isEnabled()
                assert widget.send_button.isVisible() and widget.send_button.isEnabled()
                if width == 760:
                    model_hint_widths.append(widget.model_combo.sizeHint().width())

                controls = (*combos, widget.stop_button, widget.send_button)
                for control in controls:
                    assert widget.controls_container.rect().contains(control.geometry()), (
                        f"{control.objectName()} extends outside the controls at width {width}"
                    )
                for index, left in enumerate(controls):
                    for right in controls[index + 1 :]:
                        assert not left.geometry().intersects(right.geometry()), (
                            f"{left.objectName()} overlaps {right.objectName()} at width {width}"
                        )
        assert model_hint_widths[0] > model_hint_widths[1]
        assert model_hint_widths[2] == model_hint_widths[0]
        assert emitted == []
    finally:
        widget.close()


def test_model_selection_can_move_from_long_to_short_and_back() -> None:
    app = _application()
    widget = ComposerWidget()
    widget.set_draft_mode(True)
    widget.set_model_options(
        [("gpt-6.1-sol", "gpt-6.1-sol"), ("gpt-6-luna", "gpt-6-luna")],
        "gpt-6.1-sol",
        available=True,
    )

    try:
        widget.resize(760, 260)
        widget.show()
        app.processEvents()
        assert widget.selected_model == "gpt-6.1-sol"
        widget.model_combo.setCurrentIndex(widget.model_combo.findData("gpt-6-luna"))
        assert widget.selected_model == "gpt-6-luna"
        widget.model_combo.setCurrentIndex(widget.model_combo.findData("gpt-6.1-sol"))
        assert widget.selected_model == "gpt-6.1-sol"
    finally:
        widget.close()


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
