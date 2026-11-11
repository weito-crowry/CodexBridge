from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCloseEvent, QFontDatabase
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .i18n import tr

_MAX_WORKFLOW_YAML_BYTES = 64 * 1024
_KNOWN_WORKFLOW_STATUSES = frozenset(
    {
        "queued",
        "starting",
        "running",
        "succeeded",
        "failed",
        "failed_to_start",
        "needs_reconciliation",
        "unknown",
    }
)
_KNOWN_WORKER_STATES = frozenset({"unknown", "not_started", "running", "completed", "failed"})
_SAFE_STATE = re.compile(r"^[a-zA-Z0-9_.-]{1,48}$")
_SECRET_KEY_PARTS = ("authorization", "token", "api_key", "secret", "password")
_PATH_KEY_PARTS = ("path", "workspace_root", "cwd", "working_directory")
_WINDOWS_PATH = re.compile(r"(?i)\b[A-Z]:\\[^\r\n\"'<>|]+")
_WINDOWS_FORWARD_PATH = re.compile(r"(?i)\b[A-Z]:/[^\r\n\"'<>|]+")
_UNC_PATH = re.compile(r"(?<![A-Za-z0-9])\\\\[^\r\n\"'<>|]+")
_FILE_URI = re.compile(r"(?i)\bfile:[/\\]{2,}[^\r\n\"'<>|]+")
_POSIX_PATH = re.compile(r"(?<![:/A-Za-z0-9])/(?!/)[^\r\n\"'<>|]+")
_BEARER_VALUE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_AUTH_HEADER = re.compile(r"(?i)\bauthorization\s*:\s*[^\r\n,]+")
_SECRET_ASSIGNMENT = re.compile(r"(?i)\b(?:token|api[_-]?key|password|secret)\s*[:=]\s*\S+")


def workflow_status_label(status: object) -> str:
    key = status if isinstance(status, str) and status in _KNOWN_WORKFLOW_STATUSES else "unknown"
    labels = {
        "queued": "Workflow status: queued",
        "starting": "Workflow status: starting",
        "running": "Workflow status: running",
        "succeeded": "Workflow status: succeeded",
        "failed": "Workflow status: failed",
        "failed_to_start": "Workflow status: failed to start",
        "needs_reconciliation": "Workflow status: needs reconciliation",
        "unknown": "Workflow status: unknown",
    }
    return tr(labels[key])


def _display_identifier(value: object) -> str:
    if not isinstance(value, str) or not value:
        return tr("Not available")
    if (
        len(value) > 128
        or any(character in value for character in "\r\n\t/\\")
        or _BEARER_VALUE.search(value)
        or _AUTH_HEADER.search(value)
        or _SECRET_ASSIGNMENT.search(value)
    ):
        return tr("Not available")
    return value


def _safe_state(value: object) -> str:
    if isinstance(value, bool):
        return tr("Yes") if value else tr("No")
    if isinstance(value, str) and value in _KNOWN_WORKER_STATES and _SAFE_STATE.fullmatch(value):
        return value
    return tr("Unknown")


def _redact_result(value: object, *, key: str = "") -> object:
    normalized_key = key.casefold().replace("-", "_")
    if any(part in normalized_key for part in _SECRET_KEY_PARTS + _PATH_KEY_PARTS):
        return tr("[redacted]")
    if isinstance(value, Mapping):
        return {
            str(child_key): _redact_result(child_value, key=str(child_key))
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return [_redact_result(child) for child in value]
    if isinstance(value, str):
        sanitized = _AUTH_HEADER.sub(tr("[redacted]"), value)
        sanitized = _SECRET_ASSIGNMENT.sub(tr("[redacted]"), sanitized)
        sanitized = _BEARER_VALUE.sub(tr("[redacted]"), sanitized)
        sanitized = _WINDOWS_PATH.sub(tr("[path hidden]"), sanitized)
        sanitized = _WINDOWS_FORWARD_PATH.sub(tr("[path hidden]"), sanitized)
        sanitized = _UNC_PATH.sub(tr("[path hidden]"), sanitized)
        sanitized = _FILE_URI.sub(tr("[path hidden]"), sanitized)
        return _POSIX_PATH.sub(tr("[path hidden]"), sanitized)
    return value


def _extract_final_text(payload: object) -> str | None:
    current = payload
    for _depth in range(8):
        if not isinstance(current, Mapping):
            return None
        final_text = current.get("final_text")
        if isinstance(final_text, str):
            return final_text
        current = current.get("result")
    return None


class WorkflowManagementDialog(QDialog):
    """A nonmodal, read-only view over the existing workflow control API."""

    workflow_selected = Signal(str)
    refresh_requested = Signal()
    start_requested = Signal()
    result_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Workflow management"))
        self.setModal(False)
        self.resize(900, 760)
        self._current_workflow_id: str | None = None

        root = QVBoxLayout(self)
        self.bridge_state_label = QLabel(tr("Bridge unavailable"), self)
        self.status_label = QLabel("", self)
        self.status_label.setWordWrap(True)
        root.addWidget(self.bridge_state_label)
        root.addWidget(self.status_label)

        upper = QHBoxLayout()
        list_group = QGroupBox(tr("Workflow list"), self)
        list_layout = QVBoxLayout(list_group)
        self.workflow_list = QListWidget(list_group)
        self.workflow_list.currentItemChanged.connect(self._selection_changed)
        list_layout.addWidget(self.workflow_list)
        upper.addWidget(list_group, 1)

        detail_group = QGroupBox(tr("Selected workflow details"), self)
        detail_form = QFormLayout(detail_group)
        self.detail_labels = {
            key: QLabel(tr("Not available"), detail_group)
            for key in (
                "workflow_id",
                "status",
                "created_at",
                "updated_at",
                "job_id",
                "thread_id",
                "worker_state",
                "terminal",
                "error",
            )
        }
        for key, label in (
            ("workflow_id", tr("Workflow ID")),
            ("status", tr("Status")),
            ("created_at", tr("Started")),
            ("updated_at", tr("Updated")),
            ("job_id", tr("Job ID")),
            ("thread_id", tr("Codex thread ID")),
            ("worker_state", tr("Worker state")),
            ("terminal", tr("Terminal")),
            ("error", tr("Error classification")),
        ):
            self.detail_labels[key].setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            detail_form.addRow(label, self.detail_labels[key])
        upper.addWidget(detail_group, 2)
        root.addLayout(upper, 2)

        result_group = QGroupBox(tr("Result"), self)
        result_layout = QVBoxLayout(result_group)
        result_layout.addWidget(QLabel(tr("Final text"), result_group))
        self.final_text_view = QPlainTextEdit(result_group)
        self.final_text_view.setReadOnly(True)
        self.final_text_view.setMaximumHeight(110)
        result_layout.addWidget(self.final_text_view)
        result_layout.addWidget(QLabel(tr("Complete JSON result"), result_group))
        self.result_json = QPlainTextEdit(result_group)
        self.result_json.setReadOnly(True)
        self.result_json.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.result_json.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        result_layout.addWidget(self.result_json, 1)
        root.addWidget(result_group, 3)

        buttons = QHBoxLayout()
        self.refresh_button = QPushButton(tr("Update"), self)
        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        self.start_button = QPushButton(tr("New run"), self)
        self.start_button.clicked.connect(self.start_requested.emit)
        self.result_button = QPushButton(tr("Show result"), self)
        self.result_button.setEnabled(False)
        self.result_button.clicked.connect(self.result_requested.emit)
        self.copy_button = QPushButton(tr("Copy full ID"), self)
        self.copy_button.setEnabled(False)
        self.copy_button.clicked.connect(self.copy_selected_id)
        self.close_button = QPushButton(tr("Close"), self)
        self.close_button.clicked.connect(self.close)
        buttons.addWidget(self.refresh_button)
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.result_button)
        buttons.addWidget(self.copy_button)
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)
        root.addLayout(buttons)

    @property
    def selected_workflow_id(self) -> str | None:
        return self._current_workflow_id

    def set_bridge_ready(self, ready: bool) -> None:
        self.bridge_state_label.setText(
            tr("Bridge ready for workflows") if ready else tr("Bridge unavailable")
        )
        if not ready:
            self.start_button.setEnabled(False)

    def set_start_enabled(self, enabled: bool) -> None:
        self.start_button.setEnabled(enabled)

    def set_message(self, message: str) -> None:
        self.status_label.setText(message)

    def set_workflows(
        self,
        rows: Sequence[Mapping[str, object]],
        *,
        selected_workflow_id: str | None = None,
    ) -> None:
        selected = selected_workflow_id or self._current_workflow_id
        self.workflow_list.blockSignals(True)
        self.workflow_list.clear()
        selected_item: QListWidgetItem | None = None
        for row in rows[:50]:
            workflow_id = row.get("workflow_id")
            if not isinstance(workflow_id, str) or not workflow_id:
                continue
            displayed_id = _display_identifier(workflow_id)
            if displayed_id == tr("Not available"):
                continue
            short_id = (
                displayed_id
                if len(displayed_id) <= 20
                else f"{displayed_id[:10]}…{displayed_id[-8:]}"
            )
            created = _display_identifier(row.get("created_at"))
            updated = _display_identifier(row.get("updated_at"))
            status = workflow_status_label(row.get("status"))
            item = QListWidgetItem(
                tr(
                    "{p0} · {p1} · started {p2} · updated {p3}",
                    p0=short_id,
                    p1=status,
                    p2=created,
                    p3=updated,
                )
            )
            item.setData(Qt.ItemDataRole.UserRole, workflow_id)
            self.workflow_list.addItem(item)
            if workflow_id == selected:
                selected_item = item
        self.workflow_list.blockSignals(False)
        if self.workflow_list.count():
            self.workflow_list.setCurrentItem(selected_item or self.workflow_list.item(0))
        else:
            self._current_workflow_id = None
            self.clear_selection()

    def _selection_changed(
        self, current: QListWidgetItem | None, _previous: QListWidgetItem | None
    ) -> None:
        workflow_id = current.data(Qt.ItemDataRole.UserRole) if current is not None else None
        self._current_workflow_id = workflow_id if isinstance(workflow_id, str) else None
        self.copy_button.setEnabled(self._current_workflow_id is not None)
        self.result_button.setEnabled(False)
        self.clear_detail()
        self.clear_result()
        if self._current_workflow_id is not None:
            self.workflow_selected.emit(self._current_workflow_id)

    def clear_selection(self) -> None:
        self.workflow_list.clearSelection()
        self.workflow_list.setCurrentRow(-1)
        self._current_workflow_id = None
        self.copy_button.setEnabled(False)
        self.result_button.setEnabled(False)
        self.clear_detail()
        self.clear_result()

    def clear_detail(self) -> None:
        for label in self.detail_labels.values():
            label.setText(tr("Not available"))

    def clear_result(self) -> None:
        self.final_text_view.clear()
        self.result_json.clear()

    def set_detail(self, detail: Mapping[str, object], *, error_code: str | None = None) -> None:
        status = detail.get("status")
        self.detail_labels["workflow_id"].setText(_display_identifier(detail.get("workflow_id")))
        self.detail_labels["status"].setText(workflow_status_label(status))
        self.detail_labels["created_at"].setText(_display_identifier(detail.get("created_at")))
        self.detail_labels["updated_at"].setText(_display_identifier(detail.get("updated_at")))
        self.detail_labels["job_id"].setText(_display_identifier(detail.get("job_id")))
        self.detail_labels["thread_id"].setText(_display_identifier(detail.get("thread_id")))
        self.detail_labels["worker_state"].setText(_safe_state(detail.get("worker_state")))
        self.detail_labels["terminal"].setText(_safe_state(detail.get("terminal")))
        failure_state = status in {"failed", "failed_to_start", "needs_reconciliation", "unknown"}
        classification = error_code or (str(status) if failure_state else "")
        self.detail_labels["error"].setText(
            workflow_error_message(classification) if classification else tr("None")
        )
        self.result_button.setEnabled(status == "succeeded")

    def set_error_classification(self, code: str) -> None:
        self.detail_labels["error"].setText(workflow_error_message(code))

    def set_result(self, payload: object) -> None:
        safe_payload = _redact_result(payload)
        self.result_json.setPlainText(json.dumps(safe_payload, ensure_ascii=False, indent=2))
        final_text = _extract_final_text(payload)
        self.final_text_view.setPlainText(
            str(_redact_result(final_text)) if final_text is not None else ""
        )

    def copy_selected_id(self) -> None:
        application = QApplication.instance()
        if self._current_workflow_id is None or not isinstance(application, QApplication):
            return
        application.clipboard().setText(self._current_workflow_id)
        self.set_message(tr("Full workflow ID copied"))


def workflow_error_message(code: str) -> str:
    key = {
        "forbidden": "Workflow request was not authorized",
        "workflow_not_found": "Workflow was not found",
        "workflow_needs_reconciliation": "Workflow needs reconciliation",
        "workflow_failed_to_start": "Workflow failed to start",
        "workflow_failed": "Workflow failed",
        "workflow_state_unknown": "Workflow state is unknown",
        "workflow_capacity_reached": "Workflow capacity is reached",
        "workflow_worker_not_ready": "Workflow worker is not ready",
        "workflow_service_unavailable": "Workflow service is unavailable",
        "bridge_not_ready": "Bridge is not ready",
        "invalid_workflow_request": "Workflow request is invalid",
        "request_id_conflict": "Request ID conflicts with an existing workflow",
        "timeout": "Workflow request timed out",
        "network_error": "Workflow connection failed",
        "invalid_json": "Workflow response was invalid",
        "invalid_response": "Workflow response was invalid",
        "response_too_large": "Workflow response exceeded the size limit",
        "request_too_large": "Workflow request exceeded the size limit",
        "http_error": "Workflow request failed",
        "invalid_request": "Workflow request is invalid",
        "workflow_in_progress": "Workflow is still running",
        "failed": "Workflow status: failed",
        "failed_to_start": "Workflow status: failed to start",
        "needs_reconciliation": "Workflow status: needs reconciliation",
        "unknown": "Workflow status: unknown",
    }.get(code, "Workflow request failed")
    return tr(key)


class StartWorkflowDialog(QDialog):
    """Collects a YAML file and workspace path for a single workflow request."""

    submitted = Signal(str, object)

    def __init__(
        self,
        allowed_roots: Sequence[str] = (),
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("New workflow run"))
        self.setModal(True)
        self.resize(680, 300)
        self._allowed_roots = tuple(allowed_roots)
        self._workflow_yaml: str | None = None
        self._request_id: str | None = None
        self._payload: dict[str, object] | None = None
        self._request_in_flight = False
        self._outcome_unknown = False
        self._force_close = False

        layout = QVBoxLayout(self)
        form = QFormLayout()
        yaml_row = QHBoxLayout()
        self.yaml_path_edit = QLineEdit(self)
        self.yaml_path_edit.setPlaceholderText(tr("Choose a .yaml or .yml file"))
        self.yaml_path_edit.textChanged.connect(self._load_yaml_file)
        yaml_row.addWidget(self.yaml_path_edit, 1)
        self.yaml_browse_button = QPushButton(tr("Browse…"), self)
        self.yaml_browse_button.clicked.connect(self._choose_yaml_file)
        yaml_row.addWidget(self.yaml_browse_button)
        form.addRow(tr("Workflow YAML"), yaml_row)

        workspace_row = QHBoxLayout()
        self.workspace_path_edit = QLineEdit(self)
        self.workspace_path_edit.setPlaceholderText(tr("Choose an existing absolute folder"))
        self.workspace_path_edit.textChanged.connect(self._update_preview)
        workspace_row.addWidget(self.workspace_path_edit, 1)
        self.workspace_browse_button = QPushButton(tr("Browse…"), self)
        self.workspace_browse_button.clicked.connect(self._choose_workspace)
        workspace_row.addWidget(self.workspace_browse_button)
        form.addRow(tr("Codex workspace"), workspace_row)
        self.input_preview = QLabel("{}", self)
        form.addRow(tr("JSON input"), self.input_preview)
        layout.addLayout(form)

        self.preview_label = QLabel(tr("Select a YAML file and workspace folder."), self)
        self.preview_label.setWordWrap(True)
        layout.addWidget(self.preview_label)
        self.error_label = QLabel("", self)
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)

        buttons = QHBoxLayout()
        self.submit_button = QPushButton(tr("Run workflow"), self)
        self.submit_button.setEnabled(False)
        self.submit_button.clicked.connect(self.submit)
        self.retry_button = QPushButton(tr("Retry same request"), self)
        self.retry_button.setVisible(False)
        self.retry_button.clicked.connect(self.retry_same_request)
        self.cancel_button = QPushButton(tr("Close"), self)
        self.cancel_button.clicked.connect(self.reject)
        buttons.addStretch(1)
        buttons.addWidget(self.retry_button)
        buttons.addWidget(self.submit_button)
        buttons.addWidget(self.cancel_button)
        layout.addLayout(buttons)

    @property
    def has_unknown_outcome(self) -> bool:
        return self._outcome_unknown and self._payload is not None

    def set_retry_allowed(self, allowed: bool) -> None:
        if self.has_unknown_outcome:
            self.retry_button.setVisible(allowed)

    def _choose_yaml_file(self) -> None:
        filename, _selected_filter = QFileDialog.getOpenFileName(
            self,
            tr("Choose workflow YAML"),
            "",
            tr("YAML files (*.yaml *.yml)"),
        )
        if filename:
            self.yaml_path_edit.setText(filename)

    def _choose_workspace(self) -> None:
        initial_directory = next((root for root in self._allowed_roots if Path(root).is_dir()), "")
        directory = QFileDialog.getExistingDirectory(
            self,
            tr("Choose Codex workspace"),
            initial_directory,
        )
        if directory:
            self.workspace_path_edit.setText(directory)

    def _load_yaml_file(self, filename: str) -> None:
        self._workflow_yaml = None
        self.error_label.clear()
        if not filename:
            self._update_preview()
            return
        path = Path(filename)
        if path.suffix.casefold() not in {".yaml", ".yml"} or not path.is_file():
            self.error_label.setText(tr("Choose an existing .yaml or .yml file"))
            self._update_preview()
            return
        try:
            with path.open("rb") as stream:
                raw = stream.read(_MAX_WORKFLOW_YAML_BYTES + 1)
        except OSError:
            self.error_label.setText(tr("Workflow YAML could not be read"))
            self._update_preview()
            return
        if len(raw) > _MAX_WORKFLOW_YAML_BYTES:
            self.error_label.setText(tr("Workflow YAML exceeds 64 KiB"))
            self._update_preview()
            return
        try:
            self._workflow_yaml = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            self.error_label.setText(tr("Workflow YAML must be UTF-8"))
            self._update_preview()
            return
        self._update_preview()

    def _workspace_root(self) -> str | None:
        raw = self.workspace_path_edit.text().strip()
        if not raw:
            return None
        try:
            path = Path(raw).expanduser()
            if not path.is_absolute() or not path.is_dir():
                return None
            return str(path.resolve())
        except (OSError, RuntimeError, ValueError):
            return None

    def _update_preview(self, *_args: object) -> None:
        if self._payload is not None:
            return
        filename = self.yaml_path_edit.text().strip()
        workspace = self._workspace_root()
        if self._workflow_yaml is None or workspace is None:
            self.preview_label.setText(
                tr("Select a valid YAML file and existing workspace folder.")
            )
            self.submit_button.setEnabled(False)
            return
        self.preview_label.setText(
            tr("Workflow file: {p0}\nCodex workspace: {p1}", p0=Path(filename).name, p1=workspace)
        )
        self.submit_button.setEnabled(True)

    def submit(self) -> None:
        if self._payload is not None:
            self.retry_same_request()
            return
        self._load_yaml_file(self.yaml_path_edit.text().strip())
        workspace = self._workspace_root()
        if self._workflow_yaml is None or workspace is None:
            self._update_preview()
            return
        self._request_id = str(uuid4())
        self._payload = {
            "client_request_id": self._request_id,
            "workflow_yaml": self._workflow_yaml,
            "input": {},
            "workspace_root": workspace,
        }
        self._freeze_input()
        self._send_saved_request()

    def _freeze_input(self) -> None:
        for widget in (
            self.yaml_path_edit,
            self.yaml_browse_button,
            self.workspace_path_edit,
            self.workspace_browse_button,
            self.submit_button,
        ):
            widget.setEnabled(False)

    def _send_saved_request(self) -> None:
        if self._request_id is not None and self._payload is not None:
            self._request_in_flight = True
            self._outcome_unknown = False
            self.cancel_button.setEnabled(False)
            self.error_label.setText(tr("Sending workflow request…"))
            self.retry_button.setVisible(False)
            self.submitted.emit(self._request_id, dict(self._payload))

    def retry_same_request(self) -> None:
        if self._request_id is None or self._payload is None:
            return
        self._send_saved_request()

    def show_request_failure(
        self, code: str, *, outcome_unknown: bool, retry_allowed: bool
    ) -> None:
        self._request_in_flight = False
        self._outcome_unknown = outcome_unknown
        self.error_label.setText(workflow_error_message(code))
        if outcome_unknown:
            if retry_allowed:
                self.error_label.setText(
                    tr(
                        "Send result is unknown. Update the list and do not start a new run. "
                        "You may retry this same request ID and payload."
                    )
                )
            else:
                self.error_label.setText(
                    tr("Send result is unknown. Update the list and do not start a new run.")
                )
        self.retry_button.setVisible(retry_allowed)
        self.cancel_button.setEnabled(not self._request_in_flight)

    def show_request_accepted(self) -> None:
        self._request_in_flight = False
        self._outcome_unknown = False
        self.error_label.setText(tr("Workflow request accepted"))
        self.accept()

    def force_close(self) -> None:
        self._force_close = True
        self.close()

    def reject(self) -> None:
        if not self._force_close:
            if self._request_in_flight:
                return
            if self._outcome_unknown:
                if not self._confirm_unknown_outcome_close():
                    return
                self._force_close = True
        super().reject()

    def _confirm_unknown_outcome_close(self) -> bool:
        answer = QMessageBox.question(
            self,
            tr("Unresolved workflow request"),
            tr(
                "Closing this dialog does not cancel the request sent to the server. "
                "Any job it started may continue. Check the workflow list before starting "
                "another run."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._force_close:
            super().closeEvent(event)
            return
        if self._request_in_flight:
            event.ignore()
            return
        if self._outcome_unknown:
            if not self._confirm_unknown_outcome_close():
                event.ignore()
                return
            self._force_close = True
        super().closeEvent(event)
