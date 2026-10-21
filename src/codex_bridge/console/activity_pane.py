from __future__ import annotations

import json
from collections import deque
from collections.abc import Collection, Mapping

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QPalette, QTextCursor
from PySide6.QtWidgets import (
    QGridLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

_EFFECTIVE_SANDBOX_MODES = {
    "danger-full-access",
    "external-sandbox",
    "read-only",
    "workspace-write",
}
_EFFECTIVE_APPROVAL_POLICIES = {"never", "on-request", "untrusted"}
_EFFECTIVE_APPROVALS_REVIEWERS = {"auto_review", "guardian_subagent", "user"}
_MAX_COMMAND_OUTPUT_CHARS = 256_000


def _safe_text(value: object, limit: int = 16_384) -> str:
    return value[:limit] if isinstance(value, str) else ""


def activity_row(activity: Mapping[str, object]) -> str:
    timestamp = _safe_text(activity.get("timestamp"), 128)
    activity_type = _safe_text(activity.get("type"), 128)
    activity_type = {
        "agent_commentary": "Commentary",
        "agent_message": "Agent",
    }.get(activity_type, activity_type)
    status = _safe_text(activity.get("status"), 128)
    summary = _safe_text(activity.get("summary"), 2_000)
    parts = [part for part in (timestamp, activity_type, status, summary) if part]
    details = activity.get("details")
    if isinstance(details, Mapping):
        exit_code = details.get("exit_code")
        if isinstance(exit_code, int) and not isinstance(exit_code, bool):
            parts.append(f"exit {exit_code}")
        process_id = details.get("process_id")
        if isinstance(process_id, str):
            parts.append(f"App Server process id {process_id[:512]}")
        cwd = details.get("cwd")
        if isinstance(cwd, str):
            parts.append(f"cwd: {cwd[:4_096]}")
        duration_ms = details.get("duration_ms")
        if isinstance(duration_ms, int) and not isinstance(duration_ms, bool):
            parts.append(f"{duration_ms} ms")
        paths = details.get("paths")
        if isinstance(paths, list):
            safe_paths = [path for path in paths[:100] if isinstance(path, str)]
            if safe_paths:
                parts.append("paths: " + ", ".join(safe_paths))
        decision = details.get("decision")
        if isinstance(decision, str):
            parts.append(decision[:256])
    return " · ".join(parts)


class ActivityPane(QWidget):
    approval_decision_requested = Signal(object, str)
    pending_state_changed = Signal(bool, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state_label = QLabel("not_loaded")
        self.state_label.setWordWrap(True)
        state_header = QLabel("Current state")
        self.sandbox_mode_label = QLabel("Sandbox: Unknown")
        self.sandbox_mode_label.setObjectName("effectiveSandboxMode")
        self.approval_policy_label = QLabel("Approval policy: Unknown")
        self.approval_policy_label.setObjectName("effectiveApprovalPolicy")
        self.approvals_reviewer_label = QLabel("Approvals reviewer: Unknown")
        self.approvals_reviewer_label.setObjectName("effectiveApprovalsReviewer")
        self._effective_access_section = QWidget()
        self._effective_access_section.setObjectName("effectiveThreadAccess")
        effective_access_layout = QVBoxLayout(self._effective_access_section)
        effective_access_layout.setContentsMargins(0, 0, 0, 0)
        effective_access_layout.setSpacing(1)
        for access_label in (
            self.sandbox_mode_label,
            self.approval_policy_label,
            self.approvals_reviewer_label,
        ):
            access_label.setTextFormat(Qt.TextFormat.PlainText)
            access_label.setWordWrap(True)
            effective_access_layout.addWidget(access_label)
        self._sandbox_mode_default_font = QFont(self.sandbox_mode_label.font())
        self.pending_label = QLabel("")
        self.pending_label.setWordWrap(True)
        self._pending_header = QLabel("Pending request")
        self.approval_details_label = QLabel("")
        self.approval_details_label.setObjectName("approvalDetails")
        self.approval_details_label.setWordWrap(True)
        self.approval_control_message = QLabel("")
        self.approval_control_message.setObjectName("approvalControlMessage")
        self.approval_control_message.setWordWrap(True)
        self._approval_feedback = QLabel("")
        self._approval_feedback.setObjectName("approvalFeedback")
        self._approval_feedback.setWordWrap(True)
        self.approval_buttons: dict[str, QPushButton] = {}
        button_layout = QGridLayout()
        for label, decision, name in (
            ("Allow once", "accept", "allowOnce"),
            ("Allow for session", "acceptForSession", "allowForSession"),
            ("Decline", "decline", "decline"),
            ("Cancel", "cancel", "cancel"),
        ):
            button = QPushButton(label)
            button.setObjectName(name)
            button.clicked.connect(
                lambda _checked=False, decision=decision: self._request_approval_decision(decision)
            )
            self.approval_buttons[decision] = button
            index = len(self.approval_buttons)
            button_layout.addWidget(button, index // 2, index % 2)
        self.approval_buttons_widget = QWidget()
        self.approval_buttons_widget.setLayout(button_layout)
        self.allow_once_button = self.approval_buttons["accept"]
        self.allow_for_session_button = self.approval_buttons["acceptForSession"]
        self.decline_button = self.approval_buttons["decline"]
        self.cancel_button = self.approval_buttons["cancel"]
        self._pending_approval_id: int | str | None = None
        self._pending_request_label = ""
        self._has_approval = False
        self._control_available = False
        self._approval_busy = False
        self._approval_resolved = False
        self._state_section = QWidget()
        state_layout = QVBoxLayout(self._state_section)
        state_layout.setContentsMargins(0, 0, 0, 0)
        state_layout.addWidget(state_header)
        state_layout.addWidget(self.state_label)
        state_layout.addWidget(self._effective_access_section)
        state_layout.addWidget(self._pending_header)
        state_layout.addWidget(self.pending_label)
        state_layout.addWidget(self.approval_details_label)
        state_layout.addWidget(self.approval_buttons_widget)
        state_layout.addWidget(self.approval_control_message)
        state_layout.addWidget(self._approval_feedback)
        self.approval_details_label.hide()
        self.approval_buttons_widget.hide()
        self.approval_control_message.hide()
        self._approval_feedback.hide()
        self._state_section.hide()
        self.activity_list = QListWidget()
        self.activity_list.setObjectName("activityList")
        self._activity_header = QLabel("Recent activities")
        self._activity_header.setObjectName("recentActivitiesHeader")
        self.command_output_header = QLabel("Read-only command output")
        self.command_output_header.setObjectName("commandOutputHeader")
        self.command_output_text = QPlainTextEdit()
        self.command_output_text.setObjectName("commandOutputText")
        self.command_output_text.setReadOnly(True)
        self.command_output_text.setMaximumBlockCount(2_000)
        self.command_output_text.setPlaceholderText(
            "No command output deltas observed. Output may be unavailable or not yet produced."
        )
        self._command_output_item_id: str | None = None
        self._activity_ids: set[str] = set()
        self._command_output_activity_ids: set[str] = set()
        self._command_output_activity_order: deque[str] = deque()
        self._command_output_items: set[str] = set()
        self._command_output_item_order: deque[str] = deque()
        layout = QVBoxLayout(self)
        layout.addWidget(self._state_section)
        layout.addWidget(self._activity_header)
        layout.addWidget(self.activity_list, 1)
        layout.addWidget(self.command_output_header)
        layout.addWidget(self.command_output_text, 1)

    def set_snapshot(self, snapshot: Mapping[str, object], *, reset: bool = False) -> None:
        state = _safe_text(snapshot.get("state"), 128) or "not_loaded"
        self.state_label.setText(state)
        self._set_effective_access(snapshot, state)
        pending = snapshot.get("pending_request")
        pending_label = ""
        if isinstance(pending, Mapping):
            self._pending_header.show()
            self.pending_label.show()
            pending_label = "Approval required" if state == "needs_approval" else "Input required"
            label = pending_label
            summary = _safe_text(pending.get("summary"), 2_000) or _safe_text(
                pending.get("reason"), 2_000
            )
            self.pending_label.setText(f"{label}\n{summary}".strip())
            method = pending.get("method")
            request_id = pending.get("request_id")
            self._has_approval = (
                state == "needs_approval"
                and method
                in {
                    "item/commandExecution/requestApproval",
                    "item/fileChange/requestApproval",
                    "item/permissions/requestApproval",
                }
                and not isinstance(request_id, bool)
                and isinstance(request_id, (int, str))
            )
            self._pending_approval_id = request_id if self._has_approval else None
            self._approval_busy = False
            self._approval_resolved = False
            self._approval_feedback.clear()
            if self._has_approval:
                self.approval_details_label.setText(self._approval_details(pending, snapshot))
                self.approval_details_label.show()
                self._approval_feedback.hide()
            else:
                self.approval_details_label.clear()
                self.approval_details_label.hide()
            self._sync_approval_controls()
        else:
            self._pending_header.hide()
            self.pending_label.hide()
            self.pending_label.setText("")
            self._has_approval = False
            self._pending_approval_id = None
            self._approval_busy = False
            self._approval_resolved = False
            self.approval_details_label.clear()
            self.approval_details_label.hide()
            self._approval_feedback.clear()
            self._approval_feedback.hide()
            self._sync_approval_controls()
        self._state_section.show()
        if reset:
            self.activity_list.clear()
            self.command_output_text.clear()
            self._command_output_item_id = None
            self._activity_ids.clear()
            self._command_output_activity_ids.clear()
            self._command_output_activity_order.clear()
            self._command_output_items.clear()
            self._command_output_item_order.clear()
        self.merge_activities(snapshot.get("recent_activities"))
        self._set_pending_request_state(pending_label)

    def _set_pending_request_state(self, label: str) -> None:
        if label == self._pending_request_label:
            return
        self._pending_request_label = label
        self.pending_state_changed.emit(bool(label), label)

    @property
    def approval_request_id(self) -> int | str | None:
        return self._pending_approval_id

    def set_control_available(self, available: bool) -> None:
        self._control_available = available
        self._sync_approval_controls()

    def set_approval_busy(self, busy: bool) -> None:
        self._approval_busy = busy
        if busy:
            self._approval_feedback.setText("Resolving approval…")
            self._approval_feedback.show()
        self._sync_approval_controls()

    def set_approval_feedback(self, message: str) -> None:
        self._approval_feedback.setText(message)
        self._approval_feedback.setVisible(bool(message) and self._has_approval)

    def mark_approval_resolved(self) -> None:
        self._approval_resolved = True
        self._approval_busy = False
        self._approval_feedback.setText("Approval resolved. Refreshing status…")
        self._approval_feedback.show()
        self._sync_approval_controls()

    def _sync_approval_controls(self) -> None:
        active = self._has_approval
        self.approval_buttons_widget.setVisible(active)
        for button in self.approval_buttons.values():
            button.setEnabled(
                active
                and self._control_available
                and not self._approval_busy
                and not self._approval_resolved
            )
        self.approval_control_message.setText(
            "Resolve from MCP client; Console control is unavailable for this Bridge."
        )
        self.approval_control_message.setVisible(active and not self._control_available)

    def _request_approval_decision(self, decision: str) -> None:
        if (
            self._pending_approval_id is not None
            and self._control_available
            and not self._approval_busy
            and not self._approval_resolved
        ):
            self.approval_decision_requested.emit(self._pending_approval_id, decision)

    @staticmethod
    def _approval_details(pending: Mapping[str, object], snapshot: Mapping[str, object]) -> str:
        method = pending.get("method")
        titles = {
            "item/commandExecution/requestApproval": "Command approval",
            "item/fileChange/requestApproval": "File change approval",
            "item/permissions/requestApproval": "Permission approval",
        }
        parts = [titles.get(method if isinstance(method, str) else "", "Approval request")]
        for key, label in (("thread_id", "Thread"), ("turn_id", "Turn")):
            value = _safe_text(pending.get(key), 512)
            if value:
                parts.append(f"{label}: {value}")
        summary = _safe_text(pending.get("summary"), 2_000)
        if summary:
            parts.append(f"Summary / command: {summary}")

        if method == "item/fileChange/requestApproval":
            diff = _safe_text(snapshot.get("current_diff"), 16_000)
            if diff:
                parts.append(f"Current diff:\n{diff}")
        elif method == "item/permissions/requestApproval":
            permission = pending.get("permission")
            if isinstance(permission, Mapping):
                reason = _safe_text(permission.get("reason"), 2_000)
                cwd = _safe_text(permission.get("cwd"), 2_000)
                if reason:
                    parts.append(f"Reason: {reason}")
                if cwd:
                    parts.append(f"Working directory: {cwd}")
                requested = permission.get("requested_permissions")
                if isinstance(requested, Mapping):
                    for key, label in (
                        ("fileSystem", "Requested filesystem permissions"),
                        ("network", "Requested network permissions"),
                    ):
                        if key in requested:
                            rendered = json.dumps(
                                requested[key], ensure_ascii=False, sort_keys=True
                            )[:2_000]
                            parts.append(f"{label}: {rendered}")
                scopes = permission.get("allowed_scopes")
                if isinstance(scopes, list):
                    safe_scopes = [scope for scope in scopes if scope in {"turn", "session"}]
                    if safe_scopes:
                        parts.append(f"Allowed scopes: {', '.join(safe_scopes)}")
        return "\n".join(parts)[:8_000]

    def merge_activities(self, recent: object) -> None:
        if not isinstance(recent, list):
            return
        for activity in recent:
            if isinstance(activity, Mapping):
                self.append_activity(activity)

    def append_activity(self, activity: Mapping[str, object]) -> None:
        activity_id = activity.get("activity_id")
        if (
            not isinstance(activity_id, str)
            or activity_id in self._activity_ids
            or activity_id in self._command_output_activity_ids
        ):
            return
        activity_type = activity.get("type")
        if activity_type in {"command_started", "command_output", "command_completed"}:
            self._append_command_activity(activity)
        if activity_type == "command_output":
            self._command_output_activity_ids.add(activity_id)
            self._command_output_activity_order.append(activity_id)
            while len(self._command_output_activity_order) > 500:
                self._command_output_activity_ids.discard(
                    self._command_output_activity_order.popleft()
                )
            return
        self._activity_ids.add(activity_id)
        row = QListWidgetItem(activity_row(activity))
        row.setData(Qt.ItemDataRole.UserRole, activity_id)
        self.activity_list.addItem(row)
        while self.activity_list.count() > 200:
            removed = self.activity_list.takeItem(0)
            if removed is not None:
                removed_id = removed.data(Qt.ItemDataRole.UserRole)
                if isinstance(removed_id, str):
                    self._activity_ids.discard(removed_id)

    def _append_command_activity(self, activity: Mapping[str, object]) -> None:
        activity_type = activity.get("type")
        item_id = _safe_text(activity.get("item_id"), 512) or "unknown"
        summary = _safe_text(activity.get("summary"), 2_000)
        details = activity.get("details")
        if not isinstance(details, Mapping):
            details = {}

        cursor = self.command_output_text.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        current_text = self.command_output_text.toPlainText()
        if activity_type == "command_started":
            self._command_output_item_id = item_id
            process_id = _safe_text(details.get("process_id"), 512)
            cwd = _safe_text(details.get("cwd"), 4_096)
            metadata = "".join(
                f" · {label}: {value}"
                for label, value in (("App Server process id", process_id), ("cwd", cwd))
                if value
            )
            separator = "\n" if current_text and not current_text.endswith("\n") else ""
            cursor = self._insert_bounded_command_output(
                cursor, f"{separator}[Command {item_id}{metadata}] {summary}\n"
            )
        elif activity_type == "command_output":
            if self._command_output_item_id != item_id:
                separator = "\n" if current_text and not current_text.endswith("\n") else ""
                cursor = self._insert_bounded_command_output(
                    cursor, f"{separator}[Output for command {item_id}]\n"
                )
                self._command_output_item_id = item_id
            if item_id not in self._command_output_items:
                self._command_output_items.add(item_id)
                self._command_output_item_order.append(item_id)
                while len(self._command_output_item_order) > 500:
                    self._command_output_items.discard(self._command_output_item_order.popleft())
            if details.get("output_source") == "completed_item":
                cursor = self._insert_bounded_command_output(
                    cursor, "[Final output recovered from the completed App Server item]\n"
                )
            if details.get("output_truncated") is True:
                cursor = self._insert_bounded_command_output(
                    cursor, "[Output delta truncated to its most recent 2000 characters]\n"
                )
            cursor = self._insert_bounded_command_output(cursor, summary)
        elif activity_type == "command_completed":
            if self._command_output_item_id != item_id:
                separator = "\n" if current_text and not current_text.endswith("\n") else ""
                cursor = self._insert_bounded_command_output(
                    cursor, f"{separator}[Command {item_id}] {summary}\n"
                )
            if item_id not in self._command_output_items:
                cursor = self._insert_bounded_command_output(
                    cursor,
                    "[No command output was observed; the command may have produced none "
                    "or output may be unavailable]\n",
                )
            exit_code = details.get("exit_code")
            duration_ms = details.get("duration_ms")
            result = _safe_text(activity.get("status"), 128) or "completed"
            if isinstance(exit_code, int) and not isinstance(exit_code, bool):
                result += f" · exit {exit_code}"
            if isinstance(duration_ms, int) and not isinstance(duration_ms, bool):
                result += f" · {duration_ms} ms"
            cursor = self._insert_bounded_command_output(cursor, f"\n[{result}]\n")
            self._command_output_item_id = None
        self.command_output_text.setTextCursor(cursor)
        self.command_output_text.ensureCursorVisible()

    def _insert_bounded_command_output(self, cursor: QTextCursor, text: str) -> QTextCursor:
        current = self.command_output_text.toPlainText()
        prefix = "[Older command output omitted]\n"
        available = _MAX_COMMAND_OUTPUT_CHARS - len(text) - len(prefix)
        if len(current) + len(text) > _MAX_COMMAND_OUTPUT_CHARS:
            retained = current[-max(0, available) :] if available > 0 else ""
            self.command_output_text.setPlainText(prefix + retained)
            cursor = self.command_output_text.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        return cursor

    def set_empty_state(self, text: str) -> None:
        self.state_label.setText(text)
        self._set_pending_request_state("")
        self._set_effective_access_unavailable("Unknown")
        self.pending_label.clear()
        self._has_approval = False
        self._pending_approval_id = None
        self._approval_busy = False
        self._approval_resolved = False
        self.approval_details_label.clear()
        self.approval_details_label.hide()
        self._approval_feedback.clear()
        self._approval_feedback.hide()
        self._sync_approval_controls()
        self._pending_header.hide()
        self.pending_label.hide()
        self._state_section.hide()
        self.activity_list.clear()
        self.command_output_text.clear()
        self._command_output_item_id = None
        self._activity_ids.clear()
        self._command_output_activity_ids.clear()
        self._command_output_activity_order.clear()
        self._command_output_items.clear()
        self._command_output_item_order.clear()

    def set_loading_state(self, text: str) -> None:
        self.set_empty_state(text)
        self._state_section.show()

    def set_error(self, text: str) -> None:
        self.state_label.setText(text)
        self._set_effective_access_unavailable("Unavailable")
        self.pending_label.clear()
        self._pending_header.hide()
        self.pending_label.hide()
        self._state_section.show()

    def _set_effective_access(self, snapshot: Mapping[str, object], state: str) -> None:
        metadata: object = snapshot.get("thread_metadata")
        if state == "not_loaded":
            metadata = None
        sandbox_mode = self._effective_metadata_value(
            metadata, "sandbox_mode", _EFFECTIVE_SANDBOX_MODES
        )
        approval_policy = self._effective_metadata_value(
            metadata, "approval_policy", _EFFECTIVE_APPROVAL_POLICIES
        )
        approvals_reviewer = self._effective_metadata_value(
            metadata, "approvals_reviewer", _EFFECTIVE_APPROVALS_REVIEWERS
        )

        if sandbox_mode == "danger-full-access":
            self.sandbox_mode_label.setText("Sandbox: WARNING — Full Access (danger-full-access)")
            palette = QPalette(self.sandbox_mode_label.palette())
            palette.setColor(
                QPalette.ColorRole.WindowText,
                palette.color(QPalette.ColorRole.BrightText),
            )
            self.sandbox_mode_label.setPalette(palette)
            font = QFont(self._sandbox_mode_default_font)
            font.setBold(True)
            self.sandbox_mode_label.setFont(font)
        else:
            self.sandbox_mode_label.setText(f"Sandbox: {sandbox_mode}")
            self.sandbox_mode_label.setPalette(QPalette())
            self.sandbox_mode_label.setFont(self._sandbox_mode_default_font)
        self.approval_policy_label.setText(f"Approval policy: {approval_policy}")
        self.approvals_reviewer_label.setText(f"Approvals reviewer: {approvals_reviewer}")

    @staticmethod
    def _effective_metadata_value(
        metadata: object, key: str, allowed_values: Collection[str]
    ) -> str:
        if not isinstance(metadata, Mapping):
            return "Unknown"
        value = metadata.get(key)
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 128
            or value not in allowed_values
        ):
            return "Unknown"
        return value

    def _set_effective_access_unavailable(self, value: str) -> None:
        self.sandbox_mode_label.setText(f"Sandbox: {value}")
        self.sandbox_mode_label.setPalette(QPalette())
        self.sandbox_mode_label.setFont(self._sandbox_mode_default_font)
        self.approval_policy_label.setText(f"Approval policy: {value}")
        self.approvals_reviewer_label.setText(f"Approvals reviewer: {value}")
