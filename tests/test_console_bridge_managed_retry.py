from __future__ import annotations

from collections.abc import Callable

from codex_bridge.console.bridge_managed_retry import BridgeManagedRetryCoordinator


class FakeTimer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int | None]] = []
        self.interval: int | None = None
        self.active = False

    def stop(self) -> None:
        self.calls.append(("stop", None))
        self.active = False

    def start(self) -> None:
        self.calls.append(("start", None))
        self.active = True

    def setInterval(self, interval: int) -> None:
        self.calls.append(("set_interval", interval))
        self.interval = interval

    def isActive(self) -> bool:
        return self.active


def _coordinator(
    timer: FakeTimer,
    *,
    is_closing: Callable[[], bool],
    can_start: Callable[[], bool],
    starts: list[str],
    updates: list[str],
) -> BridgeManagedRetryCoordinator:
    def start_bridge_managed() -> None:
        starts.append("start")
        timer.calls.append(("start_bridge", None))

    return BridgeManagedRetryCoordinator(
        timer,
        get_is_closing=is_closing,
        can_start_bridge=can_start,
        start_bridge_managed=start_bridge_managed,
        update_start_button=lambda: updates.append("update"),
    )


def test_retry_schedule_uses_2s_5s_10s_then_exhausts() -> None:
    timer = FakeTimer()
    starts: list[str] = []
    updates: list[str] = []

    def can_start() -> bool:
        timer.calls.append(("can_start", None))
        return True

    coordinator = _coordinator(
        timer,
        is_closing=lambda: False,
        can_start=can_start,
        starts=starts,
        updates=updates,
    )
    coordinator.activate()

    for index, delay in enumerate((2_000, 5_000, 10_000), start=1):
        coordinator.schedule_retry()
        assert timer.interval == delay
        assert timer.isActive()
        assert coordinator.retry_index == index
        coordinator.on_retry_timeout()
        assert timer.calls[-3:] == [
            ("stop", None),
            ("can_start", None),
            ("start_bridge", None),
        ]
        assert starts == ["start"] * index
        assert coordinator.managed_active

    coordinator.schedule_retry()

    assert not coordinator.managed_active
    assert coordinator.retry_index == 3
    assert coordinator.retry_exhausted
    assert updates == ["update"]
    assert not timer.isActive()


def test_schedule_and_timeout_respect_inactive_closing_and_start_guards() -> None:
    timer = FakeTimer()
    starts: list[str] = []
    updates: list[str] = []
    closing = False
    can_start = False

    def on_can_start() -> bool:
        timer.calls.append(("can_start", None))
        return can_start

    coordinator = _coordinator(
        timer,
        is_closing=lambda: closing,
        can_start=on_can_start,
        starts=starts,
        updates=updates,
    )

    coordinator.schedule_retry()
    assert timer.calls == []

    coordinator.activate()
    closing = True
    coordinator.schedule_retry()
    assert timer.calls == []

    closing = False
    coordinator.schedule_retry()
    assert timer.interval == 2_000
    coordinator.on_retry_timeout()
    assert timer.calls[-2:] == [("stop", None), ("can_start", None)]
    assert not coordinator.managed_active
    assert starts == []

    coordinator.activate()
    closing = True
    coordinator.on_retry_timeout()
    assert timer.calls[-1] == ("stop", None)
    assert coordinator.managed_active
    assert starts == []

    closing = False
    for _ in range(3):
        coordinator.activate()
        coordinator.schedule_retry()
    assert coordinator.retry_exhausted
    coordinator.on_retry_timeout()
    assert timer.calls[-1] == ("stop", None)
    assert starts == []
    assert updates == ["update"]


def test_maybe_auto_start_runs_once_and_ignores_waiting_or_exhausted_state() -> None:
    timer = FakeTimer()
    starts: list[str] = []
    updates: list[str] = []
    can_start = False
    coordinator = _coordinator(
        timer,
        is_closing=lambda: False,
        can_start=lambda: can_start,
        starts=starts,
        updates=updates,
    )

    coordinator.maybe_auto_start_bridge()
    assert starts == []

    can_start = True
    coordinator.maybe_auto_start_bridge()
    coordinator.maybe_auto_start_bridge()
    assert starts == ["start"]
    assert coordinator.managed_active

    for _ in range(4):
        coordinator.schedule_retry()
    assert coordinator.retry_exhausted
    coordinator.maybe_auto_start_bridge()
    assert starts == ["start"]


def test_retry_now_and_ready_resets_timer_and_all_state() -> None:
    timer = FakeTimer()
    starts: list[str] = []
    updates: list[str] = []
    coordinator = _coordinator(
        timer,
        is_closing=lambda: False,
        can_start=lambda: True,
        starts=starts,
        updates=updates,
    )
    coordinator.activate()
    for _ in range(4):
        coordinator.schedule_retry()
    assert coordinator.retry_exhausted

    coordinator.reset_for_retry_now()
    assert timer.calls[-1] == ("stop", None)
    assert coordinator.retry_index == 0
    assert not coordinator.retry_exhausted
    assert coordinator.managed_active

    coordinator.schedule_retry()
    coordinator.reset_after_ready()
    assert timer.calls[-1] == ("stop", None)
    assert coordinator.retry_index == 0
    assert not coordinator.retry_exhausted
    assert not coordinator.managed_active
