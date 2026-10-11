from __future__ import annotations

import pytest

from codex_bridge.console.usage import CodexUsage
from codex_bridge.console.usage_requests import UsageRequestCoordinator

USAGE_PAYLOAD = {"rateLimits": {"primary": {"windowDurationMins": 300, "usedPercent": 28}}}


class FakeTimer:
    def __init__(self) -> None:
        self.active = False
        self.current_interval: int | None = None
        self.operations: list[tuple[str, int | None]] = []

    def start(self, interval: int | None = None) -> None:
        self.active = True
        if interval is not None:
            self.current_interval = interval
        self.operations.append(("start", interval))

    def stop(self) -> None:
        self.active = False
        self.operations.append(("stop", None))

    def isActive(self) -> bool:
        return self.active

    def interval(self) -> int | None:
        return self.current_interval


class FakeClient:
    def __init__(self) -> None:
        self.accept = True
        self.requests: list[tuple[str, str]] = []
        self.aborted_groups: list[str] = []

    def get_json(self, path: str, *, key: str) -> bool:
        self.requests.append((path, key))
        return self.accept

    def abort_json_group(self, group: str) -> None:
        self.aborted_groups.append(group)


class FakeWindow:
    def __init__(self, *, ready: bool = True) -> None:
        self._closing = False
        self._usage_ready = ready
        self._client = FakeClient()
        self.usage_initial_timer = FakeTimer()
        self.usage_retry_timer = FakeTimer()
        self.usage_poll_timer = FakeTimer()
        self.turn_usage_snapshot_timer = FakeTimer()
        self._pending_usage_snapshot_turns: set[tuple[str, str]] = set()
        self._usage_snapshot_request_turns: set[tuple[str, str]] = set()
        self.finished_snapshots: list[tuple[set[tuple[str, str]], CodexUsage]] = []
        self.applied_usage: list[object] = []
        self.recorded_history = 0
        self.usage_failures: list[str] = []

    def _finish_turn_usage_snapshot(
        self, candidates: set[tuple[str, str]], usage: CodexUsage
    ) -> None:
        self.finished_snapshots.append((candidates, usage))

    def _apply_usage(self, payload: object) -> None:
        self.applied_usage.append(payload)

    def _record_usage_history(self) -> None:
        self.recorded_history += 1

    def _apply_usage_failure(self, message: str) -> None:
        self.usage_failures.append(message)

    def _start_usage_snapshot_timer(self) -> None:
        self.turn_usage_snapshot_timer.start(1_000)


def make_coordinator(*, ready: bool = True) -> tuple[FakeWindow, UsageRequestCoordinator]:
    window = FakeWindow(ready=ready)
    return window, UsageRequestCoordinator(window)  # type: ignore[arg-type]


def test_begin_sequence_resets_attempts_and_schedules_pending_snapshots() -> None:
    window, coordinator = make_coordinator()
    window._pending_usage_snapshot_turns.add(("thread-a", "turn-a"))
    coordinator.attempts = 2
    coordinator.request_in_flight = True
    coordinator.request_mode = "periodic"

    coordinator.begin_sequence()

    assert coordinator.sequence_active
    assert coordinator.attempts == 0
    assert coordinator.request_mode == "periodic"
    assert window.usage_initial_timer.operations == [("start", None)]
    assert window.usage_retry_timer.operations == [("stop", None)]
    assert window.usage_poll_timer.operations == [("start", None)]
    assert window.turn_usage_snapshot_timer.operations == [("start", 1_000)]


def test_initial_attempt_limit_and_rejected_request_roll_back_attempt() -> None:
    window, coordinator = make_coordinator()
    coordinator.sequence_active = True
    window._client.accept = False

    assert not coordinator.start_request("initial")
    assert coordinator.attempts == 0

    window._client.accept = True
    for expected_attempt in (1, 2, 3):
        assert coordinator.start_request("initial")
        assert coordinator.attempts == expected_attempt
        coordinator.request_in_flight = False
    assert not coordinator.start_request("initial")
    assert coordinator.attempts == 3
    assert len(window._client.requests) == 4


def test_start_request_rejects_closing_in_flight_and_unready_window() -> None:
    window, coordinator = make_coordinator(ready=False)
    coordinator.sequence_active = True

    assert not coordinator.start_request("initial")
    assert coordinator.attempts == 0

    window._usage_ready = True
    window._closing = True
    assert not coordinator.start_request("manual")

    window._closing = False
    coordinator.request_in_flight = True
    assert not coordinator.start_request("periodic")
    assert not window._client.requests


def test_manual_force_bypasses_readiness_but_not_closing_or_in_flight() -> None:
    window, coordinator = make_coordinator(ready=False)
    coordinator.sequence_active = True
    coordinator.attempts = 2
    coordinator.request_in_flight = True
    coordinator.request_mode = "periodic"

    coordinator.request_usage(force=True)

    assert window._client.requests == []
    assert coordinator.sequence_active
    assert coordinator.attempts == 2
    assert coordinator.request_mode == "periodic"

    coordinator.request_in_flight = False
    coordinator.request_usage(force=True)

    assert window._client.requests == [("/ui-api/account/rate-limits", "usage")]
    assert not coordinator.sequence_active
    assert coordinator.attempts == 0
    assert coordinator.request_mode == "manual"
    assert coordinator.request_in_flight

    window._closing = True
    coordinator.request_in_flight = False
    coordinator.request_usage(force=True)
    assert len(window._client.requests) == 1


def test_timeouts_start_initial_retry_and_periodic_requests() -> None:
    window, coordinator = make_coordinator()
    coordinator.sequence_active = True

    coordinator.initial_timeout()
    assert window.usage_initial_timer.operations == [("stop", None)]
    assert coordinator.request_mode == "initial"

    coordinator.request_in_flight = False
    coordinator.request_mode = None
    coordinator.retry_timeout()
    assert window.usage_retry_timer.operations == [("stop", None)]
    assert coordinator.request_mode == "initial"

    coordinator.request_in_flight = False
    coordinator.request_mode = None
    coordinator.poll_timeout()
    assert coordinator.request_mode == "periodic"
    assert len(window._client.requests) == 3


def test_retry_timeout_stops_after_sequence_or_attempt_limit_is_exhausted() -> None:
    window, coordinator = make_coordinator()
    coordinator.retry_timeout()
    assert window._client.requests == []

    coordinator.sequence_active = True
    coordinator.attempts = 3
    coordinator.retry_timeout()
    assert window._client.requests == []


@pytest.mark.parametrize("mode", [None, "initial", "periodic", "manual"])
def test_global_success_records_usage_even_without_in_flight_request(mode: str | None) -> None:
    window, coordinator = make_coordinator()
    coordinator.request_mode = mode
    coordinator.request_in_flight = mode is not None
    coordinator.sequence_active = True

    coordinator.apply_result(USAGE_PAYLOAD)

    assert window.applied_usage == [USAGE_PAYLOAD]
    assert window.recorded_history == 1
    assert not coordinator.request_in_flight
    assert coordinator.request_mode is None
    assert not coordinator.sequence_active
    assert window.usage_initial_timer.operations == [("stop", None)]
    assert window.usage_retry_timer.operations == [("stop", None)]
    assert window.usage_poll_timer.operations == [("start", None)]


def test_snapshot_success_finishes_candidates_without_global_usage_or_history() -> None:
    window, coordinator = make_coordinator()
    candidates = {("thread-a", "turn-a")}
    window._usage_snapshot_request_turns = set(candidates)
    coordinator.request_in_flight = True
    coordinator.request_mode = "turn_snapshot"

    coordinator.apply_result(USAGE_PAYLOAD)

    assert len(window.finished_snapshots) == 1
    assert window.finished_snapshots[0][0] == candidates
    assert window.finished_snapshots[0][1].five_hour is not None
    assert not window.applied_usage
    assert window.recorded_history == 0
    assert not window._usage_snapshot_request_turns
    assert window.usage_poll_timer.operations == [("start", None)]


def test_failure_without_in_flight_request_is_a_noop() -> None:
    window, coordinator = make_coordinator()

    coordinator.apply_error("ignored")

    assert not window.usage_failures
    assert not window.usage_retry_timer.operations


def test_initial_failure_retries_when_allowed_and_stops_when_exhausted() -> None:
    window, coordinator = make_coordinator()
    coordinator.request_in_flight = True
    coordinator.request_mode = "initial"
    coordinator.sequence_active = True
    coordinator.attempts = 1

    coordinator.apply_error("temporary failure")

    assert window.usage_failures == ["temporary failure"]
    assert window.usage_retry_timer.operations == [("start", None)]
    assert coordinator.sequence_active

    coordinator.request_in_flight = True
    coordinator.request_mode = "initial"
    coordinator.attempts = 3
    coordinator.apply_error("final failure")
    assert not coordinator.sequence_active
    assert window.usage_retry_timer.operations == [("start", None)]


def test_snapshot_failure_discards_failed_turns_and_reschedules_remaining() -> None:
    window, coordinator = make_coordinator()
    failed = ("thread-a", "turn-a")
    remaining = ("thread-a", "turn-b")
    window._usage_snapshot_request_turns = {failed}
    window._pending_usage_snapshot_turns = {failed, remaining}
    coordinator.request_in_flight = True
    coordinator.request_mode = "turn_snapshot"

    coordinator.apply_error("snapshot unavailable")

    assert window.usage_failures == ["snapshot unavailable"]
    assert window._pending_usage_snapshot_turns == {remaining}
    assert window.turn_usage_snapshot_timer.operations == [("start", 1_000)]
    assert not window._usage_snapshot_request_turns


def test_invalidation_aborts_usage_and_clears_snapshot_candidates() -> None:
    window, coordinator = make_coordinator()
    window._usage_snapshot_request_turns.add(("thread-a", "turn-a"))
    coordinator.sequence_active = True
    coordinator.attempts = 2
    coordinator.request_in_flight = True
    coordinator.request_mode = "turn_snapshot"

    coordinator.invalidate_sequence()

    assert not coordinator.sequence_active
    assert coordinator.attempts == 0
    assert not coordinator.request_in_flight
    assert coordinator.request_mode is None
    assert window._client.aborted_groups == ["usage"]
    assert not window._usage_snapshot_request_turns
    assert all(
        timer.operations == [("stop", None)]
        for timer in (
            window.usage_initial_timer,
            window.usage_retry_timer,
            window.usage_poll_timer,
            window.turn_usage_snapshot_timer,
        )
    )
