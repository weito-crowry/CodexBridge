from __future__ import annotations

from collections import OrderedDict, deque
from collections.abc import Mapping

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QResizeEvent, QTextCursor
from PySide6.QtWidgets import (
    QLabel,
    QPlainTextEdit,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

_MAX_COMMANDS = 100
_MAX_CHARS = 256_000
_MAX_BLOCKS = 2_000
_MAX_ACTIVITY_IDS = 1_000
CommandKey = tuple[str, str, str]


def _text(value: object, limit: int = 2_000) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _number(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class _CommandHeader(QToolButton):
    def __init__(self) -> None:
        super().__init__()
        self._command = ""
        self._result = ""
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

    def set_header(self, command: str, result: str) -> None:
        self._command, self._result = command, result
        if self.isVisible():
            self._fit_text()
        else:
            self.setText(f"{command} · {result}")

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._fit_text()

    def _fit_text(self) -> None:
        metrics = self.fontMetrics()
        suffix = f" · {self._result}"
        available = max(0, self.width() - metrics.horizontalAdvance(suffix) - 36)
        command = metrics.elidedText(self._command, Qt.TextElideMode.ElideRight, available)
        self.setText(command + suffix)


class CommandOutputPanel(QWidget):
    """One command's metadata and received output, independent of sibling panels."""

    def __init__(self, key: CommandKey, *, unidentified: bool) -> None:
        super().__init__()
        self.key = key
        self.unidentified = unidentified
        self.command = ""
        self.status = ""
        self.exit_code: int | None = None
        self.duration_ms: int | None = None
        self.process_id = ""
        self.cwd = ""
        self.completed = False
        self.output = ""
        self._removed_positions = 0
        self._removed_blocks = 0
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(50)
        self._render_timer.timeout.connect(self._render_output)
        self.output_observed = False
        self.recovered = False
        self.truncated = False
        self.omitted = False
        self.toggle_button = _CommandHeader()
        self.toggle_button.setCheckable(True)
        self.toggle_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle_button.setArrowType(Qt.ArrowType.RightArrow)
        self.toggle_button.toggled.connect(self._set_expanded)
        self.metadata_label = QLabel()
        self.metadata_label.setTextFormat(Qt.TextFormat.PlainText)
        self.metadata_label.setWordWrap(True)
        self.note_label = QLabel()
        self.note_label.setTextFormat(Qt.TextFormat.PlainText)
        self.note_label.setWordWrap(True)
        self.output_text = QPlainTextEdit()
        self.output_text.setObjectName("commandOutputText")
        self.output_text.setReadOnly(True)
        self.output_text.document().setLayoutEnabled(False)
        self.output_text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.output_text.setMinimumHeight(96)
        self.output_text.setMaximumHeight(220)
        content_layout = QVBoxLayout()
        content_layout.setContentsMargins(12, 0, 0, 0)
        content_layout.addWidget(self.metadata_label)
        content_layout.addWidget(self.note_label)
        content_layout.addWidget(self.output_text)
        self._content = QWidget()
        self._content.setLayout(content_layout)
        self._content.hide()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self.toggle_button)
        layout.addWidget(self._content)

    def update_activity(self, activity: Mapping[str, object]) -> None:
        kind = activity.get("type")
        details = activity.get("details")
        if not isinstance(details, Mapping):
            details = {}
        if kind in {"command_started", "command_completed"}:
            self.command = _text(activity.get("summary")) or self.command
            self.process_id = _text(details.get("process_id"), 512) or self.process_id
            self.cwd = _text(details.get("cwd"), 4_096) or self.cwd
            if kind == "command_completed":
                self.completed = True
                self.status = _text(activity.get("status"), 128)
                self.exit_code = _number(details.get("exit_code"))
                self.duration_ms = _number(details.get("duration_ms"))
            elif not self.completed:
                self.status = _text(activity.get("status"), 128) or "in_progress"
        elif kind == "command_output":
            if not self.completed and not self.status:
                self.status = _text(activity.get("status"), 128)
            self.output_observed = True
            self.recovered |= details.get("output_source") == "completed_item"
            self.truncated |= details.get("output_truncated") is True
            self._append_text(_text(activity.get("summary"), _MAX_CHARS))
        self.refresh_labels()

    def refresh_labels(self) -> None:
        status = self.status.casefold().replace("_", "")
        if status in {"interrupted", "cancelled", "canceled", "aborted"}:
            label = "Interrupted"
        elif status in {"failed", "error", "declined"} or (
            self.exit_code is not None and self.exit_code != 0
        ):
            label = "Failed"
        elif status in {"completed", "success", "succeeded"}:
            label = "Success"
        elif status in {"started", "inprogress", "running"}:
            label = "Running"
        else:
            label = "Unknown"
        command = self.command or "Command metadata not received"
        if self.unidentified:
            command = "Unidentified command (missing item id)"
        header = [label]
        if self.exit_code is not None:
            header.append(f"exit {self.exit_code}")
        if self.duration_ms is not None:
            header.append(f"{self.duration_ms} ms")
        self.toggle_button.set_header(command[:160], " · ".join(header))
        metadata = [f"Thread / turn / item: {' / '.join(self.key)}", self.command]
        if self.process_id:
            metadata.append(f"App Server process id: {self.process_id}")
        if self.cwd:
            metadata.append(f"cwd: {self.cwd}")
        self.metadata_label.setText("\n".join(part for part in metadata if part))
        self.toggle_button.setToolTip(self.metadata_label.text())
        notes = []
        if not self.output_observed:
            notes.append("Output not observed; it may be unavailable or not yet produced.")
        elif not self.output and not self.omitted:
            notes.append("Empty output received.")
        if self.recovered:
            notes.append("Final output recovered from the completed App Server item.")
        if self.truncated:
            notes.append("Output delta truncated to its most recent 2000 characters.")
        if self.omitted:
            notes.append("Older command output omitted (display retention limit).")
        self.note_label.setText("\n".join(notes))
        self.note_label.setVisible(bool(notes))

    def trim_output(self, *, chars: int = 0, blocks: int = 0) -> None:
        cut = min(chars, len(self.output))
        for _ in range(blocks):
            newline = self.output.find("\n", cut)
            if newline < 0:
                break
            cut = newline + 1
        if cut:
            removed = self.output[:cut]
            self.output = self.output[cut:]
            self._removed_positions += len(removed.encode("utf-16-le")) // 2
            self._removed_blocks += removed.count("\n")
            self.omitted = True
            if not self.toggle_button.isChecked():
                # A hidden view must not retain output already evicted from the buffer.
                self.output_text.clear()
            self._queue_render()
            self.refresh_labels()

    def _append_text(self, text: str) -> None:
        self.output += text
        self._queue_render()

    def _queue_render(self) -> None:
        if self.toggle_button.isChecked() and not self._render_timer.isActive():
            self._render_timer.start()

    def _render_output(self) -> None:
        self._render_timer.stop()
        previous = self.output_text.toPlainText()
        if previous == self.output:
            return
        scroll = self.output_text.verticalScrollBar().value()
        cursor = self.output_text.textCursor()
        position, anchor = cursor.position(), cursor.anchor()
        if not self._removed_positions and self.output.startswith(previous):
            insertion = QTextCursor(self.output_text.document())
            insertion.movePosition(QTextCursor.MoveOperation.End)
            insertion.insertText(self.output[len(previous) :])
        else:
            self.output_text.setPlainText(self.output)
            cursor.setPosition(max(0, anchor - self._removed_positions))
            cursor.setPosition(
                max(0, position - self._removed_positions), QTextCursor.MoveMode.KeepAnchor
            )
            self.output_text.setTextCursor(cursor)
        self.output_text.verticalScrollBar().setValue(max(0, scroll - self._removed_blocks))
        self._removed_positions = self._removed_blocks = 0

    def _set_expanded(self, expanded: bool) -> None:
        self._render_timer.stop()
        if expanded:
            self._render_output()
        self.output_text.document().setLayoutEnabled(expanded)
        self.toggle_button.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )
        self._content.setVisible(expanded)


class CommandOutputWidget(QScrollArea):
    """Bounded, session-only view of received command activity events."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("commandOutput")
        self.setWidgetResizable(True)
        self.panels: OrderedDict[CommandKey, CommandOutputPanel] = OrderedDict()
        self._activity_ids: set[str] = set()
        self._activity_order: deque[str] = deque()
        self.retention_label = QLabel()
        self.retention_label.setTextFormat(Qt.TextFormat.PlainText)
        self.retention_label.setWordWrap(True)
        self.retention_label.hide()
        self.empty_label = QLabel("No command output deltas observed.")
        self.empty_label.setWordWrap(True)
        content = QWidget()
        self._layout = QVBoxLayout(content)
        self._layout.setContentsMargins(2, 2, 2, 2)
        self._layout.addWidget(self.retention_label)
        self._layout.addWidget(self.empty_label)
        self._layout.addStretch()
        self.setWidget(content)

    def append_activity(self, activity: Mapping[str, object]) -> None:
        identity = _text(activity.get("activity_id"), 512)
        if not identity or identity in self._activity_ids:
            return
        self._activity_ids.add(identity)
        self._activity_order.append(identity)
        while len(self._activity_order) > _MAX_ACTIVITY_IDS:
            self._activity_ids.discard(self._activity_order.popleft())
        item_id = _text(activity.get("item_id"), 512)
        unidentified = not item_id.strip()
        key = (
            _text(activity.get("thread_id"), 512),
            _text(activity.get("turn_id"), 512),
            item_id if not unidentified else "\x00unidentified-" + identity,
        )
        panel = self.panels.get(key)
        if panel is None:
            panel = CommandOutputPanel(key, unidentified=unidentified)
            self.panels[key] = panel
            self._layout.insertWidget(self._layout.count() - 1, panel)
            self.empty_label.hide()
        panel.update_activity(activity)
        while len(self.panels) > _MAX_COMMANDS:
            _, old = self.panels.popitem(last=False)
            self._layout.removeWidget(old)
            old._render_timer.stop()
            old.hide()
            old.deleteLater()
            self.retention_label.setText(
                "Older command panels omitted (100 command display limit)."
            )
            self.retention_label.show()
        self._limit_output()

    def clear(self) -> None:
        for panel in self.panels.values():
            self._layout.removeWidget(panel)
            panel._render_timer.stop()
            panel.hide()
            panel.deleteLater()
        self.panels.clear()
        self._activity_ids.clear()
        self._activity_order.clear()
        self.retention_label.clear()
        self.retention_label.hide()
        self.empty_label.show()

    def _limit_output(self) -> None:
        chars = sum(len(p.output) for p in self.panels.values())
        blocks = sum(p.output.count("\n") + 1 for p in self.panels.values())
        for panel in self.panels.values():
            if chars <= _MAX_CHARS and blocks <= _MAX_BLOCKS:
                break
            old_chars = len(panel.output)
            old_blocks = panel.output.count("\n") + 1
            panel.trim_output(chars=max(0, chars - _MAX_CHARS), blocks=max(0, blocks - _MAX_BLOCKS))
            chars -= old_chars - len(panel.output)
            blocks -= old_blocks - (panel.output.count("\n") + 1)
