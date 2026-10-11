from __future__ import annotations

import pytest

from codex_bridge.console.bridge_managed_retry import BridgeManagedRetryCoordinator
from codex_bridge.console.bridge_runtime_observer import BridgeRuntimeObserver


class _Timer:
    def __init__(self, trace: list[tuple[object, ...]], name: str) -> None:
        self._trace = trace
        self._name = name
        self.active = False

    def stop(self) -> None:
        self._trace.append((self._name,))
        self.active = False

    def start(self) -> None:
        self._trace.append((f"{self._name}_start",))
        self.active = True

    def setInterval(self, interval: int) -> None:
        self._trace.append((f"{self._name}_interval", interval))

    def isActive(self) -> bool:
        return self.active


class _Tunnel:
    def __init__(self, trace: list[tuple[object, ...]]) -> None:
        self._trace = trace
        self.owned_running = True

    def set_bridge_ready(self, ready: bool) -> None:
        self._trace.append(("tunnel", ready))


class _TransitionCoordinator:
    def __init__(self, trace: list[tuple[object, ...]]) -> None:
        self._trace = trace

    def clear_pending_action(self) -> None:
        self._trace.append(("clear_pending_action",))


class _StopConfirmation:
    active = False


class _Window:
    def __init__(self) -> None:
        self.trace: list[tuple[object, ...]] = []
        self._health_ok = False
        self._bridge_ready = False
        self._app_server_ready = False
        self._bridge_transition = False
        self._bridge_stop_confirmation = _StopConfirmation()
        self._stop_outcome_uncertain = False
        self._health_observed = False
        self._status_observed = False
        self._closing = False
        self._bridge_transition_coordinator = _TransitionCoordinator(self.trace)
        self._tunnel = _Tunnel(self.trace)
        self.bridge_start_retry_timer = _Timer(self.trace, "retry_timer_stop")
        self._bridge_managed_retry_coordinator = BridgeManagedRetryCoordinator(
            self.bridge_start_retry_timer,
            get_is_closing=lambda: self._closing,
            can_start_bridge=lambda: False,
            start_bridge_managed=lambda: None,
            update_start_button=lambda: None,
        )
        self._bridge_managed_retry_coordinator.activate()
        for _ in range(3):
            self._bridge_managed_retry_coordinator.schedule_retry()
        self._bridge_managed_retry_coordinator.schedule_retry()
        self._bridge_managed_retry_coordinator.activate()
        self.trace.clear()
        self.readiness_timer = _Timer(self.trace, "readiness_timer_stop")
        self._bridge_seen_ready = False
        self._detached_launch_started = False
        self._launch_in_progress = False
        self._runtime_state = "unavailable"
        self._control_token: str | None = None
        self._bridge_loss_count = 0

    @property
    def _managed_start_active(self) -> bool:
        return self._bridge_managed_retry_coordinator.managed_active

    @property
    def _bridge_start_retry_index(self) -> int:
        return self._bridge_managed_retry_coordinator.retry_index

    @property
    def _bridge_start_retry_exhausted(self) -> bool:
        return self._bridge_managed_retry_coordinator.retry_exhausted

    def _set_runtime_state(self, state: str, *, label: str | None = None) -> None:
        self._runtime_state = state
        self.trace.append(("runtime", state, label))

    def _restore_restarted_tunnel(self) -> None:
        self.trace.append(("restore_tunnel",))

    def _confirm_bridge_stopped(self) -> None:
        self.trace.append(("confirm_stopped",))

    def _maybe_auto_start_bridge(self) -> None:
        self.trace.append(("maybe_auto_start",))

    def _tunnel_owned_running(self) -> bool:
        return self._tunnel.owned_running

    def _begin_bridge_transition(self, action: str, *, tunnel_running: bool) -> None:
        self.trace.append(("begin_transition", action, tunnel_running))

    def _update_start_button(self) -> None:
        self.trace.append(("update_start_button",))


def _ready_window(*, detached: bool) -> _Window:
    window = _Window()
    window._health_ok = True
    window._bridge_ready = True
    window._app_server_ready = True
    window._detached_launch_started = detached
    return window


@pytest.mark.parametrize(
    ("detached", "expected_runtime", "expected_tail"),
    [
        (
            True,
            "console_started",
            [
                ("readiness_timer_stop",),
                ("runtime", "console_started", "Runtime: started by Console"),
                ("restore_tunnel",),
            ],
        ),
        (False, "external", [("runtime", "external", None)]),
    ],
)
def test_ready_observation_syncs_tunnel_and_preserves_runtime_ownership(
    detached: bool,
    expected_runtime: str,
    expected_tail: list[tuple[object, ...]],
) -> None:
    window = _ready_window(detached=detached)
    window._launch_in_progress = detached
    observer = BridgeRuntimeObserver(window)

    observer.apply_runtime_observation()

    assert window._runtime_state == expected_runtime
    assert window._bridge_seen_ready
    assert window._bridge_start_retry_index == 0
    assert not window._bridge_start_retry_exhausted
    assert not window._managed_start_active
    assert window.trace == [("tunnel", True), ("retry_timer_stop",), *expected_tail]


@pytest.mark.parametrize("active_owner", ["transition", "stop_confirmation"])
def test_active_transition_keeps_tunnel_not_ready(active_owner: str) -> None:
    window = _ready_window(detached=False)
    if active_owner == "transition":
        window._bridge_transition = True
    else:
        window._bridge_stop_confirmation.active = True
    observer = BridgeRuntimeObserver(window)

    observer.apply_runtime_observation()

    assert window.trace == [("tunnel", False)]
    assert window._runtime_state == "unavailable"


def test_uncertain_stop_with_fresh_ready_observations_restores_ownership() -> None:
    window = _ready_window(detached=True)
    window._health_observed = True
    window._status_observed = True
    window._stop_outcome_uncertain = True
    window._launch_in_progress = True
    observer = BridgeRuntimeObserver(window)

    observer.apply_runtime_observation()

    assert not window._stop_outcome_uncertain
    assert not window._launch_in_progress
    assert window.trace == [
        ("clear_pending_action",),
        ("tunnel", True),
        ("retry_timer_stop",),
        ("readiness_timer_stop",),
        ("runtime", "console_started", "Runtime: started by Console"),
        ("restore_tunnel",),
    ]
    assert all(event[0] != "begin_transition" for event in window.trace)


def test_uncertain_stop_with_fresh_unavailable_observations_confirms_stop() -> None:
    window = _Window()
    window._health_observed = True
    window._status_observed = True
    window._stop_outcome_uncertain = True
    observer = BridgeRuntimeObserver(window)

    observer.apply_runtime_observation()

    assert window.trace == [("confirm_stopped",)]


@pytest.mark.parametrize("observed", ["health", "status"])
def test_uncertain_stop_waits_until_both_observations_are_fresh(observed: str) -> None:
    window = _ready_window(detached=False)
    setattr(window, f"_{observed}_observed", True)
    window._stop_outcome_uncertain = True
    observer = BridgeRuntimeObserver(window)

    observer.apply_runtime_observation()

    assert window._stop_outcome_uncertain
    assert window.trace == [("tunnel", False)]


def test_one_loss_waits_and_second_owned_loss_starts_restart_transition() -> None:
    window = _Window()
    window._detached_launch_started = True
    window._control_token = "control-token"
    observer = BridgeRuntimeObserver(window)

    observer.record_bridge_status_observation(False)
    assert observer.loss_count == 1
    assert window.trace == []

    observer.record_bridge_status_observation(False)

    assert observer.loss_count == 2
    assert window._managed_start_active
    assert window.trace == [("begin_transition", "restart", True)]


def test_successful_status_observation_resets_loss_count() -> None:
    window = _Window()
    observer = BridgeRuntimeObserver(window)

    observer.record_bridge_status_observation(False)
    observer.record_bridge_status_observation(True)
    observer.record_bridge_status_observation(False)

    assert observer.loss_count == 1
    assert window.trace == []


def test_two_external_losses_reset_seen_ready_and_keep_auto_start() -> None:
    window = _Window()
    window._bridge_seen_ready = True
    observer = BridgeRuntimeObserver(window)

    observer.record_bridge_status_observation(False)
    observer.record_bridge_status_observation(False)

    assert observer.loss_count == 2
    assert not window._bridge_seen_ready
    assert window._runtime_state == "unavailable"
    assert window.trace == [("update_start_button",), ("maybe_auto_start",)]


@pytest.mark.parametrize("guard", ["_closing", "_bridge_transition", "_launch_in_progress"])
def test_loss_threshold_does_not_recover_while_window_is_busy(guard: str) -> None:
    window = _Window()
    setattr(window, guard, True)
    observer = BridgeRuntimeObserver(window)

    observer.record_bridge_status_observation(False)
    observer.record_bridge_status_observation(False)

    assert observer.loss_count == 2
    assert window.trace == []


@pytest.mark.parametrize(
    ("detached", "seen_ready", "expected_state", "expected_label"),
    [
        (
            True,
            True,
            "console_started_unreachable",
            "Runtime: started by Console (not reachable)",
        ),
        (False, True, "external_unreachable", "Runtime: external (not reachable)"),
        (False, False, "unavailable", None),
    ],
)
def test_unready_observation_keeps_runtime_classification_and_auto_start(
    detached: bool,
    seen_ready: bool,
    expected_state: str,
    expected_label: str | None,
) -> None:
    window = _Window()
    window._detached_launch_started = detached
    window._bridge_seen_ready = seen_ready
    observer = BridgeRuntimeObserver(window)

    observer.apply_runtime_observation()

    assert window._runtime_state == expected_state
    assert window.trace == [
        ("tunnel", False),
        ("runtime", expected_state, expected_label),
        ("maybe_auto_start",),
    ]
