from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .main_window import MainWindow

_BRIDGE_LOSS_THRESHOLD = 2


class BridgeRuntimeObserver:
    def __init__(self, window: MainWindow) -> None:
        self._window = window
        self._bridge_loss_count = 0

    @property
    def loss_count(self) -> int:
        return self._bridge_loss_count

    def reset_loss_count(self) -> None:
        self._bridge_loss_count = 0

    def apply_runtime_observation(self) -> None:
        window = self._window
        ready = window._health_ok and window._bridge_ready and window._app_server_ready
        if window._bridge_transition or window._bridge_stop_confirmation.active:
            window._tunnel.set_bridge_ready(False)
            return
        if window._stop_outcome_uncertain:
            fresh_observations = window._health_observed and window._status_observed
            if fresh_observations and ready:
                window._stop_outcome_uncertain = False
                window._bridge_transition_coordinator.clear_pending_action()
            elif fresh_observations and (
                not window._health_ok and not window._bridge_ready and not window._app_server_ready
            ):
                window._confirm_bridge_stopped()
                return
            else:
                window._tunnel.set_bridge_ready(False)
                return
        window._tunnel.set_bridge_ready(ready)
        if ready:
            window.bridge_start_retry_timer.stop()
            window._bridge_start_retry_index = 0
            window._bridge_start_retry_exhausted = False
            window._managed_start_active = False
            window._bridge_seen_ready = True
            if window._detached_launch_started:
                window._launch_in_progress = False
                window.readiness_timer.stop()
                window._set_runtime_state("console_started", label="Runtime: started by Console")
                window._restore_restarted_tunnel()
            else:
                window._set_runtime_state("external")
            return
        if window._detached_launch_started:
            if not window._launch_in_progress and window._runtime_state != "launch_timed_out":
                window._set_runtime_state(
                    "console_started_unreachable",
                    label="Runtime: started by Console (not reachable)",
                )
        elif window._bridge_seen_ready:
            window._set_runtime_state(
                "external_unreachable",
                label="Runtime: external (not reachable)",
            )
        else:
            window._set_runtime_state("unavailable")
        window._maybe_auto_start_bridge()

    def record_bridge_status_observation(self, available: bool) -> None:
        if available:
            self.reset_loss_count()
            return
        self._bridge_loss_count += 1
        window = self._window
        if (
            self._bridge_loss_count < _BRIDGE_LOSS_THRESHOLD
            or window._closing
            or window._bridge_transition
            or window._launch_in_progress
        ):
            return
        self.recover_bridge_after_loss()

    def recover_bridge_after_loss(self) -> None:
        window = self._window
        self._bridge_loss_count = _BRIDGE_LOSS_THRESHOLD
        if window._detached_launch_started and window._control_token is not None:
            window._managed_start_active = True
            window._begin_bridge_transition(
                "restart", tunnel_running=window._tunnel_owned_running()
            )
            return
        window._bridge_seen_ready = False
        window._runtime_state = "unavailable"
        window._update_start_button()
        window._maybe_auto_start_bridge()
