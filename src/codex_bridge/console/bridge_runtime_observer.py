from __future__ import annotations

from typing import TYPE_CHECKING

from ..tunnel_profile import TunnelProfileError

if TYPE_CHECKING:
    from .main_window import MainWindow

_BRIDGE_LOSS_THRESHOLD = 2

_SAFE_TUNNEL_PROFILE_ERRORS = frozenset(
    {
        "Tunnel MCP extra_headers must be a mapping of safe strings",
        "Tunnel MCP extra_headers must map valid HTTP header names to safe strings",
        "Tunnel MCP credential headers must use env: or file: references",
        "Tunnel profile mapping keys must be strings",
        "Tunnel profile contains an unsupported value type",
        "Tunnel secret fields must use env: or file: references",
        "Tunnel profile YAML is empty or invalid",
        "Tunnel profile must use config_version 1",
        "Tunnel profile must provide an API key reference",
        "Tunnel profile mapping cannot be rendered",
        "Tunnel profile must be a mapping or rendered YAML string",
        "Tunnel profile exceeds the maximum supported size",
        "Tunnel profile name is invalid",
        "Tunnel client executable is required",
        "Tunnel client could not validate the staged profile",
        "Tunnel client rejected the staged profile",
        "Tunnel profile could not be atomically installed",
        "Tunnel profile could not be written",
    }
)


def _safe_tunnel_profile_error(error: TunnelProfileError) -> str:
    message = str(error)
    if message in _SAFE_TUNNEL_PROFILE_ERRORS:
        return message
    prefix = "Tunnel client rejected the staged profile (exit code "
    if message.startswith(prefix) and message.endswith(")"):
        exit_code = message[len(prefix) : -1]
        if exit_code.isdecimal():
            return message
    return "Managed Tunnel profile generation or doctor validation failed"


def advance_bridge_generation(window: MainWindow) -> None:
    window._bridge_generation += 1
    if window._managed_tunnel_profile is None:
        return
    window._managed_tunnel_profile_attempted_generation = None
    window._tunnel_profile_status = "not_run"
    window._tunnel_profile_skip_reason = "Waiting for Bridge and App Server readiness"
    if window._tunnel_resolution_status == "passed":
        window._tunnel_resolution_error = None
    window._update_tunnel_client_status()


def set_tunnel_bridge_ready(window: MainWindow, ready: bool) -> None:
    if not ready:
        window._tunnel.set_bridge_ready(False)
        return

    profile_request = window._managed_tunnel_profile
    if profile_request is None:
        window._tunnel.set_bridge_ready(True)
        return

    if window._managed_tunnel_profile_attempted_generation != window._bridge_generation:
        window._managed_tunnel_profile_attempted_generation = window._bridge_generation
        window._tunnel_profile_status = "running"
        window._tunnel_profile_skip_reason = ""
        if window._tunnel_resolution_status == "passed":
            window._tunnel_resolution_error = None
        window._update_tunnel_client_status()
        profile_settings, profile_dir, profile_name, executable, generate_profile = profile_request
        try:
            generate_profile(
                profile_settings,
                profile_dir=profile_dir,
                profile_name=profile_name,
                client_executable=executable,
            )
        except TunnelProfileError as exc:
            window._tunnel_profile_status = "failed"
            window._tunnel_profile_skip_reason = ""
            window._tunnel_resolution_error = _safe_tunnel_profile_error(exc)
            window._update_tunnel_client_status()
            window._tunnel.set_bridge_ready(False)
            return
        window._tunnel_profile_status = "passed"
        window._tunnel_profile_skip_reason = ""
        window._update_tunnel_client_status()

    window._tunnel.set_bridge_ready(window._tunnel_profile_status == "passed")


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
            set_tunnel_bridge_ready(window, False)
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
                set_tunnel_bridge_ready(window, False)
                return
        set_tunnel_bridge_ready(window, ready)
        if ready:
            window._bridge_managed_retry_coordinator.reset_after_ready()
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
            window._bridge_managed_retry_coordinator.activate()
            window._begin_bridge_transition(
                "restart", tunnel_running=window._tunnel_owned_running()
            )
            return
        if window._bridge_seen_ready:
            advance_bridge_generation(window)
        window._bridge_seen_ready = False
        window._runtime_state = "unavailable"
        window._update_start_button()
        window._maybe_auto_start_bridge()
