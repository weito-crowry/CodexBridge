from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .main_window import MainWindow


class BridgeLifecycleController:
    def __init__(
        self,
        window: MainWindow,
        generate_control_token: Callable[[], str],
    ) -> None:
        self._window = window
        self._generate_control_token = generate_control_token

    def can_start_bridge(self) -> bool:
        window = self._window
        return (
            not window._closing
            and window._config.roots_ready
            and window._codex_resolution is not None
            and window._health_observed
            and window._status_observed
            and not window._health_ok
            and not window._bridge_ready
            and not window._app_server_ready
            and not window._bridge_seen_ready
            and not window._launch_in_progress
            and not window._detached_launch_started
            and not window._bridge_transition
            and not window._tunnel_is_transitioning()
            and not window.bridge_start_retry_timer.isActive()
            and not window._bridge_start_retry_exhausted
            and window._runtime_state in {"unavailable", "stopped", "launch_failed"}
        )

    def retry_now(self) -> None:
        window = self._window
        if window._closing or window._launch_in_progress or window._bridge_transition:
            return
        bridge_ready = window._health_ok and window._bridge_ready and window._app_server_ready
        if bridge_ready and window._tunnel_state != "ready":
            retry_now = getattr(window._tunnel, "retry_now", None)
            if callable(retry_now):
                retry_now()
            return
        window._bridge_managed_retry_coordinator.reset_for_retry_now()
        if window._can_start_bridge():
            window._start_bridge(managed=True)

    def start_bridge(self, *, managed: bool = False) -> None:
        window = self._window
        resolution = window._codex_resolution
        if resolution is None or not window._can_start_bridge():
            return
        if managed:
            window._bridge_managed_retry_coordinator.activate()
        window._bridge_transition_coordinator.reset_for_launch()
        window._bridge_transition = True
        window._sync_tunnel_controls()
        window._launch_bridge(resolution.path)

    def launch_bridge(self, codex_executable: str) -> None:
        window = self._window
        window._launch_generation += 1
        control_token = self._generate_control_token()
        window._control_token = control_token
        window._launch_in_progress = True
        window._set_runtime_state("launching")
        try:
            if window._config.config_mode == "managed" and window._config.config_path is not None:
                result = window._launcher.launch(
                    codex_executable=codex_executable,
                    ui_port=window._config.port,
                    control_token=control_token,
                    allowed_roots=window._config.allowed_roots,
                    managed_config_path=window._config.config_path,
                )
            else:
                result = window._launcher.launch(
                    codex_executable=codex_executable,
                    ui_port=window._config.port,
                    control_token=control_token,
                    allowed_roots=window._config.allowed_roots,
                )
        except Exception:
            window._launch_in_progress = False
            window._control_token = None
            window._bridge_transition = False
            window._set_runtime_state("launch_failed")
            window.bottom_status_label.setText("Bridge launch failed")
            window._schedule_managed_bridge_retry()
            return
        if not result.started:
            window._launch_in_progress = False
            window._control_token = None
            window._bridge_transition = False
            window._set_runtime_state("launch_failed")
            window.bottom_status_label.setText("Bridge launch failed")
            window._schedule_managed_bridge_retry()
            return
        window._detached_launch_started = True
        window._detached_pid = result.pid
        window._bridge_readiness.begin()

    def on_launch_readiness_timeout(self) -> None:
        window = self._window
        window._launch_in_progress = False
        window._bridge_transition = False
        if window._managed_start_active:
            window._set_runtime_state("launch_failed")
        else:
            window._set_runtime_state("launch_timed_out")
        window.bottom_status_label.setText("Bridge launch timed out; it may still be starting")
        window._schedule_managed_bridge_retry()

    def finish_launch_if_ready(self) -> None:
        window = self._window
        if not window._bridge_readiness.ready():
            return
        window.readiness_timer.stop()
        window._launch_in_progress = False
        window._health_ok = True
        window._bridge_ready = True
        window._app_server_ready = True
        window._bridge_seen_ready = True
        window._bridge_runtime_observer.reset_loss_count()
        window._bridge_managed_retry_coordinator.reset_after_ready()
        window._bridge_transition = False
        window._set_runtime_state("console_started", label="Runtime: started by Console")
        window.bottom_status_label.setText("Bridge started by Console")
        window._tunnel.set_bridge_ready(True)
        window._begin_usage_sequence()
        window._restore_restarted_tunnel()

    def bridge_control_allowed(self) -> bool:
        window = self._window
        return (
            window._runtime_state == "console_started"
            and window._bridge_ready
            and window._app_server_ready
            and window._control_token is not None
            and not window._bridge_transition
            and not window._tunnel_is_transitioning()
            and not window._closing
        )

    def tunnel_owned_running(self) -> bool:
        window = self._window
        actions = getattr(window._tunnel, "action_state", None)
        return bool(getattr(actions, "stop_enabled", False)) and getattr(
            window._tunnel, "state", "running"
        ) in {"running", "ready", "not_ready"}

    def on_stop_confirmation_timed_out(self) -> None:
        window = self._window
        if window._exit_shutdown_pending:
            window._finish_exit()
            return
        window._bridge_transition = False
        window._stop_outcome_uncertain = True
        window._health_observed = False
        window._status_observed = False
        window._set_runtime_state("stop_timed_out")
        window.bottom_status_label.setText("Bridge stop timed out")

    def confirm_bridge_stopped(self) -> None:
        window = self._window
        action = window._bridge_transition_coordinator.pending_action
        stop_outcome_was_uncertain = window._stop_outcome_uncertain
        window._bridge_stop_confirmation.complete()
        window._stop_outcome_uncertain = False
        window._bridge_ready = False
        window._app_server_ready = False
        window._health_ok = False
        window._control_token = None
        window._detached_pid = None
        window._detached_launch_started = False
        window._bridge_seen_ready = False
        window._bridge_runtime_observer.reset_loss_count()
        window._launch_in_progress = False
        window._tunnel.set_bridge_ready(False)
        window._bridge_transition_coordinator.clear_pending_action()
        if action == "restart" and not stop_outcome_was_uncertain:
            resolution = window._codex_resolution
            if resolution is None:
                window._bridge_transition = False
                window._bridge_transition_coordinator.clear_restart_flag()
                window._set_runtime_state("launch_failed")
                window.bottom_status_label.setText("Bridge relaunch failed")
                return
            window._launch_bridge(resolution.path)
            return
        window._bridge_transition_coordinator.clear_restart_flag()
        window._bridge_transition = False
        window._sync_tunnel_controls()
        window.bridge_status_label.setText("Bridge: disconnected")
        window.app_server_status_label.setText("App Server: failed")
        window._set_runtime_state("stopped")
        window.bottom_status_label.setText("Bridge stopped")
