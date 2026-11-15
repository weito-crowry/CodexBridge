from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, QTimer

from .i18n import tr
from .workflows_dialog import (
    StartWorkflowDialog,
    WorkflowManagementDialog,
    workflow_error_message,
)

if TYPE_CHECKING:
    from .main_window import MainWindow


@dataclass(frozen=True)
class _WorkflowRequest:
    key: str
    kind: str
    epoch: int
    token: str
    selection_generation: int
    workflow_id: str | None = None


class WorkflowManagementController(QObject):
    """Owns the workflow dialog, polling, and stale-response guards."""

    def __init__(
        self,
        window: MainWindow,
        client: Any,
        allowed_roots: Sequence[str],
    ) -> None:
        super().__init__(window)
        self._window = window
        self._client = client
        self._allowed_roots = tuple(allowed_roots)
        self._dialog = WorkflowManagementDialog(window)
        self._dialog.refresh_requested.connect(self.refresh)
        self._dialog.start_requested.connect(self._start_new_workflow)
        self._dialog.result_requested.connect(self._request_selected_result)
        self._dialog.workflow_selected.connect(self._select_workflow)
        self._dialog.finished.connect(self._on_dialog_closed)
        self._signal_connections: list[tuple[Any, Any]] = []
        self._timer = QTimer(self)
        self._timer.setInterval(3_000)
        self._timer.timeout.connect(self._refresh_if_ready)
        self._epoch = 0
        self._selection_generation = 0
        self._request_number = 0
        self._availability: tuple[bool, str | None] = (False, None)
        self._inflight: _WorkflowRequest | None = None
        self._selected_workflow_id: str | None = None
        self._pending_selected_workflow_id: str | None = None
        self._result_loaded_for: str | None = None
        self._active_start_dialog: StartWorkflowDialog | None = None
        self._auth_blocked_token: str | None = None
        self._shutdown = False

    @property
    def dialog(self) -> WorkflowManagementDialog:
        return self._dialog

    @property
    def timer(self) -> QTimer:
        return self._timer

    @property
    def has_inflight_request(self) -> bool:
        return self._inflight is not None

    def show(self) -> None:
        if self._shutdown:
            return
        self._connect_client_signals()
        self._dialog.show()
        self._dialog.raise_()
        self._dialog.activateWindow()
        self.notify_readiness_changed()
        self.refresh()

    def _connect_client_signals(self) -> None:
        if self._signal_connections:
            return
        for name, handler in (
            ("workflow_succeeded", self._on_workflow_succeeded),
            ("workflow_failed", self._on_workflow_failed),
        ):
            signal = getattr(self._client, name, None)
            connect = getattr(signal, "connect", None)
            if callable(connect):
                connect(handler)
                self._signal_connections.append((signal, handler))

    def _disconnect_client_signals(self) -> None:
        for signal, handler in self._signal_connections:
            disconnect = getattr(signal, "disconnect", None)
            if callable(disconnect):
                try:
                    disconnect(handler)
                except (RuntimeError, TypeError):
                    pass
        self._signal_connections.clear()

    def _sync_timer(self) -> None:
        if (
            self._shutdown
            or not self._dialog.isVisible()
            or not self._availability[0]
            or self._auth_blocked_token == self._availability[1]
        ):
            self._timer.stop()
        elif not self._timer.isActive():
            self._timer.start()

    def _readiness_snapshot(self) -> tuple[bool, str | None]:
        can_use = getattr(self._window, "_can_display_control_token", None)
        token = getattr(self._window, "_control_token", None)
        ready = bool(callable(can_use) and can_use())
        return (ready and isinstance(token, str) and bool(token.strip()), token if ready else None)

    def notify_readiness_changed(self) -> None:
        if self._shutdown:
            return
        snapshot = self._readiness_snapshot()
        if snapshot == self._availability:
            self._update_buttons()
            self._sync_timer()
            return
        old_token = self._availability[1]
        self._availability = snapshot
        self._epoch += 1
        ready = snapshot[0]
        self._cancel_inflight(
            notify_start_outcome=True,
            retry_allowed=ready,
        )
        if self._auth_blocked_token is not None and snapshot[1] != self._auth_blocked_token:
            self._auth_blocked_token = None
        self._dialog.set_bridge_ready(ready)
        self._dialog.set_message(
            tr("Bridge ready for workflows") if ready else tr("Bridge unavailable")
        )
        self._update_buttons()
        self._sync_timer()
        active = self._active_start_dialog
        if active is not None and active.has_unknown_outcome:
            active.set_retry_allowed(ready)
        if ready and self._dialog.isVisible() and old_token != snapshot[1]:
            self._request_list()

    def refresh(self) -> None:
        self.notify_readiness_changed()
        if not self._dialog.isVisible():
            return
        if not self._availability[0]:
            self._dialog.set_message(tr("Bridge unavailable"))
            return
        # This is an explicit user request. Authentication failures suppress polling,
        # while a manual refresh remains available after reviewing the message.
        self._auth_blocked_token = None
        self._sync_timer()
        self._request_list()

    def _refresh_if_ready(self) -> None:
        if self._shutdown or not self._dialog.isVisible():
            self._timer.stop()
            return
        before = self._availability
        self.notify_readiness_changed()
        if self._availability[0] and before == self._availability:
            self._request_list()

    def _request_list(self) -> None:
        if not self._can_request() or self._inflight is not None:
            return
        self._send_request(
            "list", None, lambda key, token: self._client.get_control_workflows(token, key=key)
        )

    def _select_workflow(self, workflow_id: str) -> None:
        self._selection_generation += 1
        self._selected_workflow_id = workflow_id
        self._result_loaded_for = None
        if self._inflight is not None and self._inflight.kind in {"detail", "result"}:
            self._cancel_inflight()
        if self._inflight is None:
            self._request_detail(workflow_id)

    def _request_detail(self, workflow_id: str) -> None:
        if not self._can_request() or self._inflight is not None:
            return
        self._send_request(
            "detail",
            workflow_id,
            lambda key, token: self._client.get_control_workflow_detail(
                workflow_id, token, key=key
            ),
        )

    def _request_selected_result(self) -> None:
        workflow_id = self._selected_workflow_id
        if workflow_id is not None:
            self._request_result(workflow_id)

    def _request_result(self, workflow_id: str) -> None:
        if not self._can_request() or self._inflight is not None:
            return
        self._send_request(
            "result",
            workflow_id,
            lambda key, token: self._client.get_control_workflow_result(
                workflow_id, token, key=key
            ),
        )

    def _send_request(self, kind: str, workflow_id: str | None, send: Any) -> None:
        token = self._availability[1]
        if not isinstance(token, str) or not token.strip() or not self._can_request():
            return
        self._request_number += 1
        key = f"workflows:{kind}:{self._request_number}"
        request = _WorkflowRequest(
            key=key,
            kind=kind,
            epoch=self._epoch,
            token=token,
            selection_generation=self._selection_generation,
            workflow_id=workflow_id,
        )
        self._inflight = request
        self._update_buttons()
        accepted = send(key, token)
        if not accepted and self._inflight == request:
            self._inflight = None
            self._dialog.set_message(workflow_error_message("invalid_request"))
            self._update_buttons()

    def _can_request(self) -> bool:
        ready, token = self._readiness_snapshot()
        return (
            not self._shutdown
            and self._dialog.isVisible()
            and ready
            and isinstance(token, str)
            and token == self._availability[1]
            and self._auth_blocked_token != token
        )

    def _request_is_current(self, request: _WorkflowRequest) -> bool:
        ready, token = self._readiness_snapshot()
        if (
            self._shutdown
            or not self._dialog.isVisible()
            or not ready
            or token != request.token
            or request.epoch != self._epoch
        ):
            return False
        if request.kind in {"detail", "result"}:
            return (
                request.selection_generation == self._selection_generation
                and request.workflow_id == self._selected_workflow_id
            )
        if request.kind == "start":
            return self._active_start_dialog is not None
        return True

    def _on_workflow_succeeded(self, key: str, status_code: int, payload: object) -> None:
        request = self._inflight
        if request is None or request.key != key:
            return
        self._inflight = None
        self._update_buttons()
        if not self._request_is_current(request):
            return
        if request.kind == "list":
            self._apply_workflow_list(payload)
        elif request.kind == "detail":
            self._apply_workflow_detail(request, payload)
        elif request.kind == "result":
            self._apply_workflow_result(request, status_code, payload)
        elif request.kind == "start":
            self._apply_start_response(payload)

    def _apply_workflow_list(self, payload: object) -> None:
        rows_value = payload.get("workflows") if isinstance(payload, Mapping) else None
        if not isinstance(rows_value, list):
            self._dialog.set_message(workflow_error_message("invalid_json"))
            return
        rows = [
            row
            for row in rows_value[:50]
            if isinstance(row, Mapping) and isinstance(row.get("workflow_id"), str)
        ]
        selected = self._pending_selected_workflow_id or self._selected_workflow_id
        self._pending_selected_workflow_id = None
        self._dialog.set_workflows(rows, selected_workflow_id=selected)
        truncated = isinstance(payload, Mapping) and payload.get("truncated") is True
        self._dialog.set_message(
            tr("Showing the newest 50 workflows") if truncated else tr("Workflow list updated")
        )

    def _apply_workflow_detail(self, request: _WorkflowRequest, payload: object) -> None:
        if not isinstance(payload, Mapping) or payload.get("workflow_id") != request.workflow_id:
            self._dialog.set_message(workflow_error_message("invalid_json"))
            return
        self._dialog.set_detail(payload)
        status = payload.get("status")
        if status == "succeeded" and request.workflow_id != self._result_loaded_for:
            self._request_result(request.workflow_id or "")

    def _apply_workflow_result(
        self,
        request: _WorkflowRequest,
        status_code: int,
        payload: object,
    ) -> None:
        if status_code == 202:
            self._dialog.set_message(workflow_error_message("workflow_in_progress"))
            return
        if not isinstance(payload, Mapping):
            self._dialog.set_message(workflow_error_message("invalid_json"))
            return
        self._dialog.set_result(payload)
        self._result_loaded_for = request.workflow_id
        self._dialog.set_message(tr("Workflow result updated"))

    def _on_workflow_failed(self, key: str, status_code: int, code: str) -> None:
        request = self._inflight
        if request is None or request.key != key:
            return
        self._inflight = None
        if not self._request_is_current(request):
            self._update_buttons()
            return
        if code == "forbidden" or status_code == 403:
            self._auth_blocked_token = request.token
        message = workflow_error_message(code)
        if request.kind == "detail" and isinstance(request.workflow_id, str):
            self._dialog.set_error_classification(code)
            self._dialog.set_message(message)
        elif request.kind == "result":
            self._dialog.set_message(message)
            self._dialog.set_error_classification(code)
        elif request.kind == "start" and self._active_start_dialog is not None:
            outcome_unknown = (
                code
                in {"timeout", "network_error", "invalid_response", "invalid_json", "http_error"}
                or status_code >= 500
            )
            retry_allowed = status_code != 403 and code != "forbidden"
            self._active_start_dialog.show_request_failure(
                code,
                outcome_unknown=outcome_unknown,
                retry_allowed=retry_allowed,
            )
            if outcome_unknown:
                self._dialog.set_message(tr("Workflow start result is unknown"))
        else:
            self._dialog.set_message(message)
        self._update_buttons()
        self._sync_timer()

    def _start_new_workflow(self) -> None:
        self.notify_readiness_changed()
        if self._inflight is not None:
            self._dialog.set_message(tr("A workflow request is already in progress"))
            return
        if not self._availability[0] or self._auth_blocked_token == self._availability[1]:
            self._dialog.set_message(tr("Bridge unavailable"))
            return
        active = self._active_start_dialog
        if active is not None and active.isVisible():
            active.raise_()
            active.activateWindow()
            return
        dialog = StartWorkflowDialog(self._allowed_roots, self._dialog)
        self._active_start_dialog = dialog
        dialog.submitted.connect(self._submit_workflow)
        dialog.finished.connect(lambda _result, dialog=dialog: self._forget_start_dialog(dialog))
        dialog.show()

    def _submit_workflow(self, request_id: str, payload: object) -> None:
        dialog = self._active_start_dialog
        if dialog is None or not isinstance(payload, Mapping):
            return
        if self._inflight is not None:
            dialog.show_request_failure("network_error", outcome_unknown=True, retry_allowed=True)
            return
        token = self._availability[1]
        if not self._can_request() or not isinstance(token, str):
            dialog.show_request_failure("network_error", outcome_unknown=False, retry_allowed=False)
            return
        self._request_number += 1
        key = f"workflows:start:{self._request_number}"
        request = _WorkflowRequest(
            key=key,
            kind="start",
            epoch=self._epoch,
            token=token,
            selection_generation=self._selection_generation,
        )
        self._inflight = request
        self._update_buttons()
        accepted = self._client.post_control_workflow(token, payload, key=key)
        if not accepted and self._inflight == request:
            self._inflight = None
            dialog.show_request_failure(
                "invalid_request", outcome_unknown=False, retry_allowed=True
            )
        self._update_buttons()

    def _apply_start_response(self, payload: object) -> None:
        if not isinstance(payload, Mapping) or not isinstance(payload.get("workflow_id"), str):
            if self._active_start_dialog is not None:
                self._active_start_dialog.show_request_failure(
                    "invalid_response", outcome_unknown=True, retry_allowed=True
                )
            return
        workflow_id = payload["workflow_id"]
        if not workflow_id:
            if self._active_start_dialog is not None:
                self._active_start_dialog.show_request_failure(
                    "invalid_response", outcome_unknown=True, retry_allowed=True
                )
            return
        self._pending_selected_workflow_id = workflow_id
        if self._active_start_dialog is not None:
            self._active_start_dialog.show_request_accepted()
        self._dialog.set_message(tr("Workflow request accepted"))
        self._request_list()

    def _forget_start_dialog(self, dialog: StartWorkflowDialog) -> None:
        if self._active_start_dialog is dialog:
            self._active_start_dialog = None
            self._update_buttons()
        dialog.deleteLater()

    def _update_buttons(self) -> None:
        ready = self._availability[0]
        enabled = (
            ready
            and self._inflight is None
            and self._auth_blocked_token != self._availability[1]
            and self._active_start_dialog is None
            and not self._shutdown
        )
        self._dialog.set_start_enabled(enabled)

    def _cancel_inflight(
        self,
        *,
        notify_start_outcome: bool = False,
        retry_allowed: bool = False,
    ) -> None:
        request = self._inflight
        self._inflight = None
        if request is not None:
            abort = getattr(self._client, "abort_workflow_request", None)
            if callable(abort):
                abort(request.key)
            if (
                request.kind == "start"
                and notify_start_outcome
                and self._active_start_dialog is not None
            ):
                self._active_start_dialog.show_request_failure(
                    "network_error",
                    outcome_unknown=True,
                    retry_allowed=retry_allowed,
                )
                self._dialog.set_message(tr("Workflow start result is unknown"))

    def _on_dialog_closed(self, _result: int) -> None:
        self._timer.stop()
        self._disconnect_client_signals()
        self._epoch += 1
        self._cancel_inflight()
        self._selected_workflow_id = None
        self._result_loaded_for = None
        active = self._active_start_dialog
        if active is not None:
            active.force_close()
            self._active_start_dialog = None

    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        self._timer.stop()
        self._epoch += 1
        self._cancel_inflight()
        active = self._active_start_dialog
        if active is not None:
            active.force_close()
            self._active_start_dialog = None
        self._dialog.close()
        self._disconnect_client_signals()
