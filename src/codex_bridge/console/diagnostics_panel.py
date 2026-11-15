from __future__ import annotations

from collections import deque
from itertools import islice
from typing import TYPE_CHECKING

from PySide6.QtGui import QColor, QPalette, QTextCharFormat, QTextCursor

from .i18n import tr

if TYPE_CHECKING:
    from .main_window import MainWindow


class DiagnosticsPanelController:
    def __init__(self, window: MainWindow) -> None:
        self._window = window
        self._history: deque[str] = deque(maxlen=2000)
        self._filter = "All"

    def set_filter(self, selected: str) -> None:
        self._filter = selected
        self._render_history()

    def clear(self) -> None:
        self._history.clear()
        self._window.diagnostics_text.clear()

    def toggle_diagnostics(self) -> None:
        self._set_diagnostics_visible(self._window.diagnostics_pane.isHidden())

    def _set_diagnostics_visible(self, visible: bool) -> None:
        window = self._window
        window.diagnostics_pane.setVisible(visible)
        window.diagnostics_toggle_button.setText(
            tr("Hide Diagnostics") if visible else tr("Diagnostics")
        )
        if visible:
            self.clear()
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
        maxlen = self._history.maxlen or 2000
        evicted_count = max(0, len(self._history) + len(lines) - maxlen)
        relevant_eviction = False
        if evicted_count:
            old_count = len(self._history)
            relevant_eviction = any(
                self._matches_filter(line)
                for line in islice(self._history, min(evicted_count, old_count))
            )
            if not relevant_eviction and evicted_count > old_count:
                relevant_eviction = any(
                    self._matches_filter(line) for line in islice(lines, evicted_count - old_count)
                )
        self._history.extend(lines)
        if relevant_eviction and self._filter != "All":
            self._render_history()
            return
        visible = self._filtered(lines)
        if not visible:
            return
        self._insert_lines(visible)

    def _filtered(self, lines: list[str] | tuple[str, ...]) -> list[str]:
        if self._filter == "stdout":
            return [line for line in lines if line.startswith("[Bridge stdout]")]
        if self._filter == "stderr":
            return [line for line in lines if line.startswith("[Bridge stderr]")]
        return list(lines)

    def _matches_filter(self, line: str) -> bool:
        if self._filter == "stdout":
            return line.startswith("[Bridge stdout]")
        if self._filter == "stderr":
            return line.startswith("[Bridge stderr]")
        return True

    def _render_history(self) -> None:
        self._window.diagnostics_text.clear()
        self._insert_lines(self._filtered(tuple(self._history)))

    def _insert_lines(self, lines: list[str]) -> None:
        if not lines:
            return
        window = self._window
        scrollbar = window.diagnostics_text.verticalScrollBar()
        follow = scrollbar.maximum() - scrollbar.value() <= 1
        previous_value = scrollbar.value()
        normal_format = QTextCharFormat()
        normal_format.setForeground(
            window.diagnostics_text.palette().color(QPalette.ColorRole.Text)
        )
        cursor = QTextCursor(window.diagnostics_text.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        for line in lines:
            if window.diagnostics_text.document().characterCount() > 1:
                cursor.insertBlock()
            prefix = "[Bridge stderr]"
            if line.startswith(prefix):
                color = window.palette().color(window.foregroundRole())
                warning = QColor(190, 135, 45) if color.lightness() > 128 else QColor(230, 185, 105)
                fmt = QTextCharFormat()
                fmt.setForeground(warning)
                cursor.insertText(prefix, fmt)
                cursor.insertText(line[len(prefix) :], normal_format)
            else:
                cursor.insertText(line, normal_format)
        if follow:
            scrollbar.setValue(scrollbar.maximum())
        else:
            scrollbar.setValue(min(previous_value, scrollbar.maximum()))
