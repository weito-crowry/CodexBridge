from __future__ import annotations

from typing import TYPE_CHECKING

from .i18n import tr

if TYPE_CHECKING:
    from .main_window import MainWindow


_RUNTIME_LABELS = {
    "unavailable": "Runtime: unavailable",
    "external": "Runtime: external",
    "launching": "Runtime: launching",
    "launch_failed": "Runtime: launch failed",
    "launch_timed_out": "Runtime: launch timed out",
    "stopping": "Runtime: stopping",
    "restarting": "Runtime: restarting",
    "stopped": "Runtime: stopped",
    "control_failed": "Runtime: control failed",
    "stop_timed_out": "Runtime: stop timed out",
}


class RuntimeUiPresenter:
    def __init__(self, window: MainWindow) -> None:
        self._window = window

    def _update_tunnel_client_status(self) -> None:
        window = self._window
        version = getattr(window._tunnel, "client_version", None)
        source = getattr(window._tunnel, "resolution_source", "unavailable")
        executable = getattr(window._tunnel, "executable", None)
        if version is None:
            text = tr("Tunnel Client: unavailable")
        else:
            text = tr("Tunnel Client: {p0} · {p1}", p0=version, p1=source)
        window.tunnel_client_status_label.setText(text)
        if executable is not None:
            window.tunnel_client_status_label.setToolTip(executable)
        if window._tunnel_resolution_error is not None:
            window.tunnel_client_status_label.setToolTip(window._tunnel_resolution_error)
        window.tunnel_init_diagnostics_label.setText(self._tunnel_initialization_diagnostics())

    def _tunnel_initialization_diagnostics(self) -> str:
        window = self._window
        resolution_status = getattr(window, "_tunnel_resolution_status", "not_run")
        resolution_detail = (
            window._tunnel_resolution_error if resolution_status == "failed" else None
        )
        profile_status = getattr(window, "_tunnel_profile_status", "not_run")
        profile_detail = (
            window._tunnel_resolution_error
            if profile_status == "failed"
            else getattr(window, "_tunnel_profile_skip_reason", None)
            if profile_status == "not_run"
            else None
        )
        validation_status = getattr(window._tunnel, "validation_status", "not_run")
        validation_detail = getattr(window._tunnel, "validation_detail", None)
        if validation_status == "not_run" and profile_status == "failed":
            validation_detail = tr("Managed profile generation and doctor validation failed")

        return "\n".join(
            (
                self._tunnel_stage_line(
                    tr("Executable resolution"), resolution_status, resolution_detail
                ),
                self._tunnel_stage_line(
                    tr("Managed profile generation and doctor"), profile_status, profile_detail
                ),
                self._tunnel_stage_line(
                    tr("Tunnel Supervisor validation"), validation_status, validation_detail
                ),
            )
        )

    @staticmethod
    def _tunnel_stage_line(name: str, status: object, detail: object) -> str:
        labels = {
            "passed": tr("Passed"),
            "failed": tr("Failed"),
            "running": tr("Running"),
        }
        label = labels.get(status, "Not run") if isinstance(status, str) else tr("Not run")
        line = f"{name}: {label}"
        if isinstance(detail, str) and detail:
            line += f" — {detail}"
        return line

    def _apply_tunnel_controls(self, start: bool, stop: bool, restart: bool) -> None:
        window = self._window
        if window._bridge_transition:
            start = stop = restart = False
        window.start_tunnel_button.setEnabled(start)
        window.stop_tunnel_button.setEnabled(stop)
        window.restart_tunnel_button.setEnabled(restart)
        for name, enabled in (
            ("Start Tunnel", start),
            ("Stop Tunnel", stop),
            ("Restart Tunnel", restart),
        ):
            action = window._tray_actions.get(name)
            if action is not None:
                action.setEnabled(enabled)

    def _update_bridge_controls(self) -> None:
        window = self._window
        enabled = (
            not window._closing
            and window._runtime_state == "console_started"
            and window._bridge_ready
            and window._app_server_ready
            and window._control_token is not None
            and not window._bridge_transition
            and not window._tunnel_is_transitioning()
        )
        window.stop_bridge_button.setEnabled(enabled)
        window.restart_bridge_button.setEnabled(enabled)
        window.restart_codexbridge_button.setEnabled(enabled)
        if (
            window._runtime_state == "external"
            and window._bridge_ready
            and window._app_server_ready
        ):
            window.restart_codexbridge_button.setToolTip(
                tr("Disabled while the Bridge is managed externally.")
            )
        else:
            window.restart_codexbridge_button.setToolTip(tr("Restart the Console-managed Bridge."))
        for name in ("Stop Bridge", "Restart Bridge"):
            action = window._tray_actions.get(name)
            if action is not None:
                action.setEnabled(enabled)
        start_action = window._tray_actions.get("Start Bridge")
        if start_action is not None:
            start_action.setEnabled(window.start_bridge_button.isEnabled())

    def _sync_bridge_controls(self) -> None:
        self._window._update_start_button()
        self._window._update_bridge_controls()

    def _sync_tunnel_controls(self) -> None:
        actions = self._window._tunnel.action_state
        self._window._apply_tunnel_controls(
            actions.start_enabled,
            actions.stop_enabled,
            actions.restart_enabled,
        )

    def _sync_empty_state(self) -> None:
        window = self._window
        if not window._bridge_ready:
            location = f"{window._config.host}:{window._config.port}"
            window.history_pane.set_empty_state(
                tr(
                    "CodexBridge is not available on {p0}\nStart codex-bridge and retry.",
                    p0=location,
                )
            )
            window.activity_pane.set_error(tr("Bridge unavailable"))
            return
        if getattr(window.thread_pane, "thread_count", 0) == 0:
            window.history_pane.set_empty_state(tr("No threads found."))
            window.activity_pane.set_empty_state(tr("No thread selected."))
            return
        if window._selected_thread_id is None:
            window.history_pane.set_empty_state(tr("Select a thread to view history."))
            window.activity_pane.set_empty_state(tr("Select a thread to view activity."))

    def _sync_overall_status(self) -> None:
        window = self._window
        bridge_ready = window._health_ok and window._bridge_ready and window._app_server_ready
        if bridge_ready:
            if window._tunnel_state == "ready":
                state = tr("Ready")
            elif window._tunnel_state in {"checking", "starting", "running", "stopping"}:
                state = tr("Starting")
            else:
                state = tr("Degraded")
        elif (
            window._launch_in_progress
            or window._bridge_transition
            or window._managed_start_active
            or window._runtime_state
            in {
                "launching",
                "restarting",
                "stopping",
                "external_unreachable",
                "console_started_unreachable",
            }
        ):
            state = tr("Starting")
        elif window._bridge_start_retry_exhausted or window._runtime_state in {
            "launch_failed",
            "control_failed",
            "stop_timed_out",
        }:
            state = tr("Error")
        elif not window._health_observed and not window._status_observed:
            state = tr("Starting")
        else:
            state = tr("Error")
        text = f"● {state}"
        window.overall_status_label.setText(text)
        window.overall_detail_label.setText(text)

    def _update_start_button(self) -> None:
        window = self._window
        window.start_bridge_button.setEnabled(window._can_start_bridge())
        start_action = window._tray_actions.get("Start Bridge")
        if start_action is not None:
            start_action.setEnabled(window.start_bridge_button.isEnabled())
        window._update_lifecycle_controls()

    def _update_lifecycle_controls(self) -> None:
        window = self._window
        bridge_ready = window._health_ok and window._bridge_ready and window._app_server_ready
        retry_enabled = (
            not window._closing
            and not window._launch_in_progress
            and not window._bridge_transition
            and (not bridge_ready or window._tunnel_state != "ready")
        )
        window.retry_now_button.setEnabled(retry_enabled)
