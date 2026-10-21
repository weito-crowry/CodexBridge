from __future__ import annotations

import sys
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QHideEvent, QShowEvent
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .process_inspection import (
    CommandDescriptor,
    CommandKey,
    CommandProcessState,
    InspectionResult,
    ProcessEvidence,
    ProcessView,
    inspect_command_processes,
)

_MAX_COMMANDS = 50
_POLL_INTERVAL_MS = 4_000
_TERMINAL_COMMAND_STATES = frozenset({"completed", "failed", "interrupted", "error"})
ProcessInspector = Callable[
    [tuple[CommandDescriptor, ...], ProcessEvidence, threading.Event], InspectionResult
]


class _InspectionSignals(QObject):
    finished = Signal(int, int, object, object)


class _InspectionTask(QRunnable):
    def __init__(
        self,
        inspector: ProcessInspector,
        commands: tuple[CommandDescriptor, ...],
        evidence: ProcessEvidence,
        cancelled: threading.Event,
        generation: int,
        token: int,
    ) -> None:
        super().__init__()
        self.inspector = inspector
        self.commands = commands
        self.evidence = evidence
        self.cancelled = cancelled
        self.generation = generation
        self.token = token
        self.signals = _InspectionSignals()
        self.setAutoDelete(True)

    def run(self) -> None:
        try:
            result = self.inspector(self.commands, self.evidence, self.cancelled)
            error = None
        except Exception:
            result = None
            error = "Process inspection failed"
        self.signals.finished.emit(self.generation, self.token, result, error)


class ProcessMonitorWidget(QWidget):
    """Bounded, local-only process observations for selected-thread commands."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        inspector: ProcessInspector | None = None,
        platform_name: str | None = None,
        poll_interval_ms: int = _POLL_INTERVAL_MS,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("processMonitor")
        self._platform_name = platform_name or sys.platform
        self._inspector = inspector or self._default_inspector
        self._records: OrderedDict[CommandKey, CommandDescriptor] = OrderedDict()
        self._thread_id: str | None = None
        self._evidence = ProcessEvidence()
        self._last_result: InspectionResult | None = None
        self._generation = 0
        self._next_token = 0
        self._active_token: int | None = None
        self._active_cancel: threading.Event | None = None
        self._busy = False
        self._refresh_pending = False
        self._closed = False

        self.toggle_button = QToolButton()
        self.toggle_button.setObjectName("processMonitorToggle")
        self.toggle_button.setText("Processes (read-only)")
        self.toggle_button.setCheckable(True)
        self.toggle_button.setChecked(False)
        self.toggle_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle_button.setArrowType(Qt.ArrowType.RightArrow)
        self.toggle_button.toggled.connect(self._set_expanded)
        self.refresh_button = QPushButton("Refresh processes")
        self.refresh_button.setObjectName("refreshProcesses")
        self.refresh_button.clicked.connect(self.refresh_processes)
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(self.toggle_button, 1)
        header.addWidget(self.refresh_button)

        self.process_status_label = QLabel("")
        self.process_status_label.setWordWrap(True)
        self.process_text = QPlainTextEdit()
        self.process_text.setObjectName("processMonitorText")
        self.process_text.setReadOnly(True)
        self.process_text.setMaximumBlockCount(1_500)
        self.process_text.setMinimumHeight(64)
        self.process_text.setMaximumHeight(132)
        self.process_text.setPlaceholderText("No observed command process details.")
        content_layout = QVBoxLayout()
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(2)
        content_layout.addWidget(self.process_status_label)
        content_layout.addWidget(self.process_text)
        self._content = QWidget()
        self._content.setLayout(content_layout)
        self._content.hide()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(2)
        layout.addLayout(header)
        layout.addWidget(self._content)

        self.process_timer = QTimer(self)
        self.process_timer.setInterval(max(250, poll_interval_ms))
        self.process_timer.timeout.connect(self.refresh_processes)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._pool.setExpiryTimeout(0)
        self.destroyed.connect(self._cancel_for_destroy)
        application = QApplication.instance()
        if application is not None:
            application.aboutToQuit.connect(self.shutdown)

        if self._platform_name != "win32":
            self.process_text.setPlainText("Windows only")
            self.refresh_button.setEnabled(False)

    @property
    def record_count(self) -> int:
        return len(self._records)

    def append_activity(self, activity: Mapping[str, object]) -> None:
        if self._closed or activity.get("type") not in {"command_started", "command_completed"}:
            return
        descriptor = _command_from_activity(activity)
        if descriptor is None:
            return
        if self._thread_id is None:
            self._thread_id = descriptor.key.thread_id
        if descriptor.key.thread_id != self._thread_id:
            return
        prior = self._records.get(descriptor.key)
        if prior is None and activity.get("type") != "command_started":
            return
        if (
            prior is not None
            and prior.app_server_process_id
            and descriptor.app_server_process_id
            and prior.app_server_process_id != descriptor.app_server_process_id
        ):
            self._remove_evidence_for(descriptor.key)
        if prior is not None and descriptor.command_status in {"started", "in_progress"}:
            descriptor = CommandDescriptor(
                descriptor.key,
                descriptor.summary or prior.summary,
                descriptor.app_server_process_id or prior.app_server_process_id,
                descriptor.started_at_seconds or prior.started_at_seconds,
                prior.completed_at_seconds,
                prior.command_status,
                prior.exit_code,
            )
        elif prior is not None:
            descriptor = CommandDescriptor(
                descriptor.key,
                descriptor.summary or prior.summary,
                descriptor.app_server_process_id or prior.app_server_process_id,
                descriptor.started_at_seconds or prior.started_at_seconds,
                descriptor.completed_at_seconds or prior.completed_at_seconds,
                descriptor.command_status or prior.command_status,
                descriptor.exit_code if descriptor.exit_code is not None else prior.exit_code,
            )
        changed = prior != descriptor
        self._records[descriptor.key] = descriptor
        while len(self._records) > _MAX_COMMANDS:
            self._records.popitem(last=False)
        self._prune_evidence()
        self._sync_timer()
        if changed:
            self.refresh_processes()

    def set_snapshot(self, snapshot: Mapping[str, object]) -> None:
        recent = snapshot.get("recent_activities")
        if isinstance(recent, list):
            for activity in recent:
                if isinstance(activity, Mapping):
                    self.append_activity(activity)

    def reset(self) -> None:
        self._generation += 1
        if self._active_cancel is not None:
            self._active_cancel.set()
        self._records.clear()
        self._thread_id = None
        self._evidence = ProcessEvidence()
        self._last_result = None
        self._refresh_pending = False
        self.process_timer.stop()
        self.process_status_label.clear()
        self.process_text.clear()

    def refresh_processes(self) -> None:
        if self._closed or self._platform_name != "win32":
            return
        if not self._records:
            self.process_text.setPlainText("No command start events observed for this thread.")
            return
        if not self.isVisible():
            self._refresh_pending = True
            return
        if self._busy:
            self._refresh_pending = True
            return
        self._start_inspection()

    def hideEvent(self, event: QHideEvent) -> None:
        self.process_timer.stop()
        if self._active_cancel is not None:
            self._active_cancel.set()
        super().hideEvent(event)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if self._records:
            self.refresh_processes()

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._generation += 1
        self.process_timer.stop()
        if self._active_cancel is not None:
            self._active_cancel.set()
        self._pool.clear()
        self._records.clear()
        self._thread_id = None
        self._evidence = ProcessEvidence()
        self._last_result = None
        self.process_text.clear()
        self.process_status_label.clear()
        self.refresh_button.setEnabled(False)

    def _default_inspector(
        self,
        commands: tuple[CommandDescriptor, ...],
        evidence: ProcessEvidence,
        cancelled: threading.Event,
    ) -> InspectionResult:
        return inspect_command_processes(
            commands,
            evidence,
            platform_name=self._platform_name,
            cancelled=cancelled,
        )

    def _start_inspection(self) -> None:
        if self._closed or self._busy or not self._records:
            return
        if not self.isVisible():
            self._refresh_pending = True
            return
        self._busy = True
        self._refresh_pending = False
        self._next_token += 1
        token = self._next_token
        cancelled = threading.Event()
        self._active_token = token
        self._active_cancel = cancelled
        self.process_status_label.setText("Refreshing processes…")
        task = _InspectionTask(
            self._inspector,
            tuple(self._records.values()),
            self._evidence,
            cancelled,
            self._generation,
            token,
        )
        task.signals.finished.connect(self._on_inspection_finished)
        if not self._pool.tryStart(task):
            self._busy = False
            self._active_token = None
            self._active_cancel = None
            self.process_status_label.setText("Process inspection is busy")

    def _on_inspection_finished(
        self, generation: int, token: int, result: object, error: object
    ) -> None:
        if token != self._active_token:
            return
        self._busy = False
        self._active_token = None
        self._active_cancel = None
        if not self._closed and generation == self._generation:
            if isinstance(result, InspectionResult):
                self._evidence = result.evidence
                self._last_result = result
                self._render_result(result)
            elif isinstance(error, str):
                self.process_status_label.setText(error)
                self.process_text.setPlainText(error)
            self._sync_timer()
        if not self._closed and self._refresh_pending and self.isVisible():
            QTimer.singleShot(0, self._start_inspection)

    def _render_result(self, result: InspectionResult) -> None:
        self._last_result = result
        if not result.supported:
            message = result.message or "Windows only"
            self.process_status_label.setText(message)
            self.process_text.setPlainText(message)
            return
        if result.message:
            self.process_status_label.setText(result.message)
        else:
            self.process_status_label.setText(
                "Observed locally; process details stay in this Console."
            )
        lines: list[str] = []
        for state in result.commands:
            command = state.command
            lines.extend(
                (
                    f"Command: {command.summary or 'Unknown'}",
                    f"Thread / turn / item: {command.key.thread_id} / {command.key.turn_id} / "
                    f"{command.key.item_id}",
                    f"Command status: {_display(command.command_status)}"
                    + (f" · exit {command.exit_code}" if command.exit_code is not None else ""),
                    f"Started: {_time_text(command.started_at_seconds)}",
                    f"Ended: {_time_text(command.completed_at_seconds)}",
                    f"App Server ID: {_display(command.app_server_process_id)}",
                    f"OS process: {state.root_status}",
                )
            )
            if state.root is not None:
                lines.extend(_process_lines(state.root, prefix="  "))
            for child in state.children:
                lines.append(f"  Related process: {child.state}")
                lines.extend(_process_lines(child, prefix="    "))
            lines.append("")
        if not lines:
            lines.append("No command process records to inspect.")
        self.process_text.setPlainText("\n".join(lines).rstrip())

    def _sync_timer(self) -> None:
        if self._closed or self._platform_name != "win32" or not self.isVisible():
            self.process_timer.stop()
            return
        active_command = any(
            record.command_status.casefold() not in _TERMINAL_COMMAND_STATES
            for record in self._records.values()
        )
        observed_running = any(
            state.root_status == "Running"
            or any(child.state == "Running" for child in state.children)
            for state in self._latest_states()
        )
        if active_command or observed_running:
            if not self.process_timer.isActive():
                self.process_timer.start()
        else:
            self.process_timer.stop()

    def _latest_states(self) -> tuple[CommandProcessState, ...]:
        return self._last_result.commands if self._last_result is not None else ()

    def _prune_evidence(self) -> None:
        keys = set(self._records)
        self._evidence = ProcessEvidence(
            roots=tuple((key, value) for key, value in self._evidence.roots if key in keys),
            children=tuple((key, value) for key, value in self._evidence.children if key in keys),
        )

    def _remove_evidence_for(self, key: CommandKey) -> None:
        self._evidence = ProcessEvidence(
            roots=tuple(
                (record_key, value)
                for record_key, value in self._evidence.roots
                if record_key != key
            ),
            children=tuple(
                (record_key, value)
                for record_key, value in self._evidence.children
                if record_key != key
            ),
        )
        if self._last_result is None:
            return
        self._last_result = InspectionResult(
            tuple(state for state in self._last_result.commands if state.command.key != key),
            self._evidence,
            self._last_result.supported,
            self._last_result.message,
        )
        self._render_result(self._last_result)

    def _set_expanded(self, expanded: bool) -> None:
        self.toggle_button.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )
        self._content.setVisible(expanded)

    def _cancel_for_destroy(self, *_args: object) -> None:
        if self._active_cancel is not None:
            self._active_cancel.set()


def _command_from_activity(activity: Mapping[str, object]) -> CommandDescriptor | None:
    thread_id = _string(activity.get("thread_id"))
    turn_id = _string(activity.get("turn_id"))
    item_id = _string(activity.get("item_id"))
    if not thread_id or not turn_id or not item_id:
        return None
    activity_type = activity.get("type")
    details = activity.get("details")
    if not isinstance(details, Mapping):
        details = {}
    started = _seconds(details.get("started_at_ms"))
    if started is None and activity_type == "command_started":
        started = _event_time(activity.get("timestamp"))
    completed = _seconds(details.get("completed_at_ms"))
    if completed is None and activity_type == "command_completed":
        completed = _event_time(activity.get("timestamp"))
    process_id = details.get("process_id")
    exit_code = details.get("exit_code")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        exit_code = None
    status = _string(activity.get("status")) or (
        "in_progress" if activity_type == "command_started" else "completed"
    )
    return CommandDescriptor(
        CommandKey(thread_id, turn_id, item_id),
        _string(activity.get("summary"))[:2_000],
        process_id[:512] if isinstance(process_id, str) else "",
        started,
        completed,
        status[:128],
        exit_code,
    )


def _seconds(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value) / 1_000.0
    return result if 0 < result < 10_000_000_000 else None


def _event_time(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


def _string(value: object) -> str:
    return value[:4_096] if isinstance(value, str) else ""


def _display(value: str | None) -> str:
    return value if value else "Unknown"


def _time_text(value: float | None) -> str:
    if value is None:
        return "Unknown"
    try:
        return datetime.fromtimestamp(value, tz=UTC).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return "Unknown"


def _process_lines(process: ProcessView, *, prefix: str) -> list[str]:
    return [
        f"{prefix}Windows PID: {process.pid}",
        f"{prefix}Name: {_display(process.name)}",
        f"{prefix}Created: {_time_text(process.create_time)}",
        f"{prefix}Command line: {_display(process.command_line)}",
        f"{prefix}Parent PID: {_display(str(process.ppid) if process.ppid is not None else None)}",
        f"{prefix}Process status: {_display(process.status)}",
    ]
