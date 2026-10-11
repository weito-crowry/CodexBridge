from __future__ import annotations

from codex_bridge.console.bridge_stop_confirmation import BridgeStopConfirmationCoordinator


class FakeTimer:
    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0
        self.running = False

    def start(self) -> None:
        self.starts += 1
        self.running = True

    def stop(self) -> None:
        self.stops += 1
        self.running = False


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _coordinator() -> tuple[BridgeStopConfirmationCoordinator, FakeTimer, FakeClock, list[str]]:
    timer = FakeTimer()
    clock = FakeClock(20.0)
    events: list[str] = []
    coordinator = BridgeStopConfirmationCoordinator(
        timer,
        on_confirmed=lambda: events.append("confirmed"),
        on_timed_out=lambda: events.append("timed_out"),
        request_health=lambda: events.append("health"),
        clock=clock,
    )
    return coordinator, timer, clock, events


def test_begin_sets_ten_second_deadline_and_resets_observations() -> None:
    coordinator, timer, clock, _events = _coordinator()

    coordinator.begin()
    coordinator.observe_health(False)
    clock.now = 25.0
    coordinator.begin()

    assert coordinator.active
    assert coordinator.deadline == 35.0
    assert not coordinator.health_unavailable
    assert not coordinator.status_unavailable
    assert timer.starts == 2
    assert timer.running


def test_observations_confirm_once_only_after_both_endpoints_are_unavailable() -> None:
    coordinator, timer, _clock, events = _coordinator()
    coordinator.begin()

    coordinator.observe_health(False)
    assert coordinator.active
    assert coordinator.health_unavailable
    assert events == []

    coordinator.observe_status(True)
    assert coordinator.active
    assert not coordinator.status_unavailable
    assert events == []

    coordinator.observe_status(False)
    coordinator.observe_health(False)

    assert not coordinator.active
    assert timer.stops == 1
    assert events == ["confirmed"]


def test_successful_observations_clear_their_unavailable_flags() -> None:
    coordinator, _timer, _clock, events = _coordinator()
    coordinator.begin()
    coordinator.observe_health(False)
    assert coordinator.health_unavailable
    coordinator.observe_health(True)
    assert not coordinator.health_unavailable

    coordinator.observe_status(False)
    assert coordinator.status_unavailable
    coordinator.observe_status(True)
    assert not coordinator.status_unavailable

    assert coordinator.active
    assert not coordinator.health_unavailable
    assert not coordinator.status_unavailable
    assert events == []


def test_tick_repolls_then_times_out_once() -> None:
    coordinator, timer, clock, events = _coordinator()
    coordinator.begin()

    coordinator.tick()
    assert events == ["health"]
    assert coordinator.active

    clock.now = coordinator.deadline
    coordinator.tick()
    coordinator.tick()

    assert not coordinator.active
    assert not timer.running
    assert events == ["health", "timed_out"]


def test_tick_confirms_unavailability_before_checking_expired_deadline() -> None:
    coordinator, timer, clock, events = _coordinator()
    coordinator.begin()
    coordinator._health_unavailable = True
    coordinator._status_unavailable = True
    clock.now = coordinator.deadline

    coordinator.tick()

    assert events == ["confirmed"]
    assert not coordinator.active
    assert not timer.running


def test_cancel_complete_inactive_tick_and_deadline_setter() -> None:
    coordinator, timer, _clock, events = _coordinator()
    coordinator.begin()
    coordinator.deadline = 0.0
    assert coordinator.deadline == 0.0

    coordinator.cancel()
    coordinator.tick()
    assert not coordinator.active
    assert not timer.running
    assert events == []

    coordinator.begin()
    coordinator.complete()
    coordinator.tick()
    assert not coordinator.active
    assert not timer.running
    assert events == []
