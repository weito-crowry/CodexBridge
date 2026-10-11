from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .main_window import MainWindow


class DiagnosticsPanelController:
    def __init__(self, window: MainWindow) -> None:
        self._window = window

    def toggle_diagnostics(self) -> None:
        self._set_diagnostics_visible(self._window.diagnostics_pane.isHidden())

    def _set_diagnostics_visible(self, visible: bool) -> None:
        window = self._window
        window.diagnostics_pane.setVisible(visible)
        window.diagnostics_toggle_button.setText("Hide Diagnostics" if visible else "Diagnostics")
        if visible:
            window.diagnostics_text.clear()
            window._diagnostics_reader.reset()
            window._on_diagnostics_timeout()
            window.diagnostics_timer.start()
        else:
            window.diagnostics_timer.stop()

    def _on_diagnostics_timeout(self) -> None:
        window = self._window
        try:
            lines = window._diagnostics_reader.poll()
        except Exception:
            return
        if lines:
            window._append_diagnostics_lines(lines)

    def _append_diagnostics_lines(self, lines: list[str]) -> None:
        if not lines:
            return
        window = self._window
        scrollbar = window.diagnostics_text.verticalScrollBar()
        follow = scrollbar.maximum() - scrollbar.value() <= 1
        previous_value = scrollbar.value()
        window.diagnostics_text.appendPlainText("\n".join(lines))
        if follow:
            scrollbar.setValue(scrollbar.maximum())
        else:
            scrollbar.setValue(min(previous_value, scrollbar.maximum()))
