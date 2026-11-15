from __future__ import annotations

from typing import TYPE_CHECKING, cast

from ..models import ApprovalDecision
from .i18n import tr

if TYPE_CHECKING:
    from .main_window import MainWindow


def connect_client(window: MainWindow) -> None:
    window._client.json_succeeded.connect(window.apply_json_result)
    window._client.json_failed.connect(window._apply_json_error)
    window._client.activity_received.connect(window._apply_activity)
    window._client.stream_state_changed.connect(window._apply_stream_state)
    global_activity_signal = getattr(window._client, "global_activity_received", None)
    if global_activity_signal is not None:
        global_activity_signal.connect(window._apply_global_activity)
    global_state_signal = getattr(window._client, "global_stream_state_changed", None)
    if global_state_signal is not None:
        global_state_signal.connect(window._apply_global_stream_state)
    window._client.control_succeeded.connect(window._apply_control_success)
    window._client.control_failed.connect(window._apply_control_failure)
    window.activity_pane.approval_decision_requested.connect(window._resolve_approval)
    window.thread_pane.refresh_requested.connect(window.refresh)
    window.thread_pane.thread_selected.connect(window.select_thread)
    window.thread_pane.thread_rename_requested.connect(window._rename_thread)
    window.thread_pane.thread_open_requested.connect(window._open_thread_in_codex)
    window.thread_pane.thread_copy_requested.connect(window._copy_thread_content)
    window.thread_pane.new_thread_for_folder_requested.connect(window._new_thread_for_folder)
    window.thread_pane.folder_explorer_requested.connect(window._open_project_folder)
    window.thread_pane.folder_terminal_requested.connect(window._open_project_terminal)
    window.history_pane.older_requested.connect(window.load_older)


def connect_runtime(window: MainWindow) -> None:
    window._codex_probe_controller.attach(window._codex_probe)
    window._codex_update_probe.check_succeeded.connect(window._apply_codex_update_check)
    window._codex_update_probe.check_failed.connect(window._apply_codex_update_check_error)
    window._codex_update_probe.update_succeeded.connect(window._apply_codex_update_success)
    window._codex_update_probe.update_failed.connect(window._apply_codex_update_failure)
    window._codex_update_probe.busy_changed.connect(window._apply_codex_update_busy)
    window.start_bridge_button.clicked.connect(window._start_bridge)
    window.stop_bridge_button.clicked.connect(window._stop_bridge)
    window.restart_bridge_button.clicked.connect(window._restart_bridge)
    window._tunnel.state_changed.connect(window._apply_tunnel_state)
    window._tunnel.message_changed.connect(window._apply_tunnel_message)
    window._tunnel.controls_changed.connect(window._apply_tunnel_controls)
    window.start_tunnel_button.clicked.connect(window._start_tunnel)
    window.stop_tunnel_button.clicked.connect(window._stop_tunnel)
    window.restart_tunnel_button.clicked.connect(window._restart_tunnel)
    window.status_button.clicked.connect(window._show_status)
    window.usage_status_label.activated.connect(window._create_or_show_usage_history_window)
    window.usage_history_button.clicked.connect(window._create_or_show_usage_history_window)
    window.status_refresh_button.clicked.connect(window._refresh_status)
    window.status_close_button.clicked.connect(window.status_dialog.close)
    window.codex_update_check_button.clicked.connect(window._check_codex_updates)
    window.codex_update_button.clicked.connect(window._update_codex)
    window.retry_now_button.clicked.connect(window._retry_now)
    window.restart_codexbridge_button.clicked.connect(window._restart_codexbridge)
    window.advanced_toggle_button.toggled.connect(window.advanced_controls.setVisible)


def apply_control_success(window: MainWindow, key: str) -> None:
    if key == "control:approval" and window._approval_control_pending:
        window._approval_control_pending = False
        window.activity_pane.set_approval_busy(False)
        window.activity_pane.set_approval_feedback(tr("Approval sent. Refreshing status…"))
        window._request_selected_status()
        return
    if key != "control:shutdown" or not (
        window._bridge_transition_coordinator.acknowledge_shutdown_success()
    ):
        return
    window._bridge_stop_confirmation.begin()
    window.bottom_status_label.setText(tr("Bridge shutdown requested"))
    window._request_health()


def apply_control_failure(window: MainWindow, key: str, message: str) -> None:
    if key == "control:approval" and window._approval_control_pending:
        window._approval_control_pending = False
        window.activity_pane.set_approval_busy(False)
        if "already resolved" in message.casefold():
            window.activity_pane.mark_approval_resolved()
            window.activity_pane.set_approval_feedback(
                tr("Approval already resolved. Refreshing status…")
            )
            window._request_selected_status()
        else:
            window.activity_pane.set_approval_feedback(tr("Approval request failed"))
        return
    if key != "control:shutdown":
        return
    window._bridge_transition_coordinator.mark_shutdown_failed()
    window._bridge_stop_confirmation.cancel()
    if window._exit_shutdown_pending:
        window._finish_exit()
        return
    window._bridge_transition = False
    window._set_runtime_state("control_failed")
    window.bottom_status_label.setText(tr("Bridge control request failed"))


def resolve_approval(window: MainWindow, request_id: object, decision: str) -> None:
    if (
        window._closing
        or window._approval_control_pending
        or window._runtime_state != "console_started"
        or window._control_token is None
        or request_id != window.activity_pane.approval_request_id
        or isinstance(request_id, bool)
        or not isinstance(request_id, (int, str))
        or decision not in {"accept", "acceptForSession", "decline", "cancel"}
    ):
        return
    window._approval_control_pending = True
    window.activity_pane.set_approval_busy(True)
    accepted = window._client.post_control_approval(
        window._control_token,
        request_id=request_id,
        decision=cast(ApprovalDecision, decision),
        key="control:approval",
    )
    if not accepted and window._approval_control_pending:
        window._apply_control_failure("control:approval", tr("Bridge control request failed"))
