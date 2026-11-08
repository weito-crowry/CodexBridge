from __future__ import annotations

from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QApplication

from codex_bridge.console.main_window import MainWindow
from tests.console_main_window_test_support import (
    FakeClient,
    FakeCodexProbe,
    FakeDiagnosticsReader,
    _application,
    _config,
)


def _window(reader: FakeDiagnosticsReader | None = None) -> MainWindow:
    _application()
    return MainWindow(
        _config(),
        api_client=FakeClient(),
        codex_probe=FakeCodexProbe(),
        diagnostics_reader=reader,
        tray_available=False,
    )


def test_filters_keep_hidden_history_and_clear_it() -> None:
    window = _window()
    rows = [
        "[Bridge] structured",
        "[Console] console",
        "[Bridge stdout] out",
        "[Bridge stderr] err",
    ]
    window._append_diagnostics_lines(rows)
    assert window.diagnostics_text.toPlainText().splitlines() == rows

    window.diagnostics_filter_buttons["stdout"].click()
    assert window.diagnostics_text.toPlainText() == rows[2]
    window._append_diagnostics_lines(["[Bridge stderr] hidden"])
    assert window.diagnostics_text.toPlainText() == rows[2]
    window.diagnostics_filter_buttons["stderr"].click()
    assert window.diagnostics_text.toPlainText().splitlines() == [rows[3], "[Bridge stderr] hidden"]
    window.diagnostics_filter_buttons["All"].click()
    assert window.diagnostics_text.toPlainText().splitlines() == rows + ["[Bridge stderr] hidden"]

    window.diagnostics_filter_buttons["stdout"].click()
    window.diagnostics_clear_button.click()
    window.diagnostics_filter_buttons["All"].click()
    assert window.diagnostics_text.toPlainText() == ""
    window._append_diagnostics_lines(["[Bridge] fresh"])
    assert window.diagnostics_text.toPlainText() == "[Bridge] fresh"
    window.close()


def test_stderr_colors_only_prefix_and_copy_stays_plain_text() -> None:
    window = _window()
    row = "[Bridge stderr] message body"
    window._append_diagnostics_lines([row])
    widget = window.diagnostics_text
    assert widget.toPlainText() == row
    cursor = QTextCursor(widget.document())
    cursor.setPosition(2)
    prefix_color = cursor.charFormat().foreground().color()
    cursor.setPosition(len("[Bridge stderr]") + 2)
    body_color = cursor.charFormat().foreground().color()
    assert prefix_color != body_color
    widget.selectAll()
    widget.copy()
    application = QApplication.instance()
    assert application is not None
    assert application.clipboard().text() == row
    window.close()


def test_reopen_keeps_filter_and_history_is_bounded() -> None:
    reader = FakeDiagnosticsReader([["[Bridge stdout] first"]])
    window = _window(reader)
    window.diagnostics_filter_buttons["stdout"].click()
    window.diagnostics_toggle_button.click()
    window.diagnostics_toggle_button.click()
    assert window.diagnostics_filter_buttons["stdout"].isChecked()
    assert window.diagnostics_text.toPlainText().count("first") == 1

    window._append_diagnostics_lines([f"[Bridge stdout] line-{i}" for i in range(2100)])
    assert len(window._diagnostics_panel._history) == 2000
    assert window.diagnostics_text.blockCount() == 2000
    assert "line-0" not in window.diagnostics_text.toPlainText()
    window.close()
