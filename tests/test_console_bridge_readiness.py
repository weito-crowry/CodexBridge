from __future__ import annotations

from collections.abc import Mapping

import pytest

from codex_bridge.console import bridge_readiness as readiness_module
from codex_bridge.console.bridge_readiness import BridgeReadinessCoordinator


class FakeTimer:
    def __init__(self) -> None:
        self.start_count = 0
        self.stop_count = 0
        self.active = False

    def start(self) -> None:
        self.start_count += 1
        self.active = True

    def stop(self) -> None:
        self.stop_count += 1
        self.active = False


class FakeClient:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []

    def get_json(self, path: str, *, key: str, query: Mapping[str, object] | None = None) -> bool:
        self.requests.append((path, key))
        return True


class FakeLabel:
    def __init__(self, text: str = "") -> None:
        self.value = text

    def setText(self, text: str) -> None:
        self.value = text


def make_coordinator(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[
    BridgeReadinessCoordinator,
    FakeTimer,
    FakeClient,
    FakeLabel,
    FakeLabel,
    list[bool],
    list[bool],
    list[float],
    dict[str, bool],
]:
    now = [100.0]
    monkeypatch.setattr(readiness_module, "monotonic", lambda: now[0])
    timer = FakeTimer()
    client = FakeClient()
    bridge_label = FakeLabel("Bridge: disconnected")
    app_server_label = FakeLabel("App Server: failed")
    state = {"closing": False, "launching": True}
    ready_calls: list[bool] = []
    timeout_calls: list[bool] = []

    def on_ready() -> None:
        ready_calls.append(True)
        state["launching"] = False

    coordinator = BridgeReadinessCoordinator(
        timer,
        client,
        bridge_label,
        app_server_label,
        is_closing=lambda: state["closing"],
        is_launch_in_progress=lambda: state["launching"],
        on_ready=on_ready,
        on_timeout=lambda: timeout_calls.append(True),
    )
    return (
        coordinator,
        timer,
        client,
        bridge_label,
        app_server_label,
        ready_calls,
        timeout_calls,
        now,
        state,
    )


def test_begin_sets_deadline_starts_timer_and_requests_in_health_then_status_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        coordinator,
        timer,
        client,
        _bridge,
        _app,
        ready_calls,
        _timeouts,
        _now,
        _state,
    ) = make_coordinator(monkeypatch)

    coordinator.begin()

    assert coordinator.deadline == 110.0
    assert timer.start_count == 1
    assert timer.active
    assert client.requests == [
        ("/healthz", "launch:health"),
        ("/ui-api/status", "launch:status"),
    ]
    assert ready_calls == []


def test_deadline_can_be_read_and_written(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator, *_ = make_coordinator(monkeypatch)

    coordinator.deadline = 123.5

    assert coordinator.deadline == 123.5


def test_status_then_health_ready_fires_once_and_updates_existing_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        coordinator,
        _timer,
        _client,
        bridge_label,
        app_server_label,
        ready_calls,
        _timeouts,
        _now,
        _state,
    ) = make_coordinator(monkeypatch)
    coordinator.begin()

    coordinator.apply_status({"bridge": "connected", "app_server": "ready"})
    assert ready_calls == []
    coordinator.apply_health({"status": "ok"})
    coordinator.apply_health({"status": "ok"})
    coordinator.apply_status({"bridge": "ready", "app_server": "connected"})

    assert bridge_label.value == "Bridge: connected"
    assert app_server_label.value == "App Server: ready"
    assert ready_calls == [True]


def test_non_mapping_status_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    (
        coordinator,
        _timer,
        _client,
        bridge_label,
        app_server_label,
        ready_calls,
        _timeouts,
        _now,
        _state,
    ) = make_coordinator(monkeypatch)
    coordinator.begin()
    coordinator.apply_status({"bridge": "ready", "app_server": "connected"})

    coordinator.apply_status(["malformed"])
    coordinator.apply_health({"status": "ok"})

    assert bridge_label.value == "Bridge: connected"
    assert app_server_label.value == "App Server: ready"
    assert ready_calls == [True]


def test_fail_health_and_status_clear_their_readiness_before_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        coordinator,
        _timer,
        _client,
        _bridge,
        _app,
        ready_calls,
        _timeouts,
        _now,
        _state,
    ) = make_coordinator(monkeypatch)
    coordinator.begin()
    status = {"bridge": "ready", "app_server": "connected"}

    coordinator.apply_status(status)
    coordinator.fail_status()
    coordinator.apply_health({"status": "ok"})
    assert ready_calls == []
    coordinator.apply_status(status)
    assert ready_calls == [True]

    _state["launching"] = True
    coordinator.begin()
    coordinator.apply_health({"status": "ok"})
    coordinator.fail_health()
    coordinator.apply_status(status)
    assert ready_calls == [True]
    coordinator.apply_health({"status": "ok"})

    assert ready_calls == [True, True]


def test_tick_repolls_before_deadline_then_times_out_at_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        coordinator,
        timer,
        client,
        _bridge,
        _app,
        _ready,
        timeout_calls,
        now,
        _state,
    ) = make_coordinator(monkeypatch)
    coordinator.begin()
    now[0] = 109.999

    coordinator.tick()

    assert len(client.requests) == 4
    assert timer.active
    assert timeout_calls == []

    now[0] = 110.0
    coordinator.tick()

    assert len(client.requests) == 4
    assert timer.stop_count == 1
    assert not timer.active
    assert timeout_calls == [True]


@pytest.mark.parametrize(("closing", "launching"), [(True, True), (False, False)])
def test_tick_stops_when_closing_or_launch_has_finished(
    monkeypatch: pytest.MonkeyPatch, closing: bool, launching: bool
) -> None:
    (
        coordinator,
        timer,
        client,
        _bridge,
        _app,
        _ready,
        _timeouts,
        _now,
        state,
    ) = make_coordinator(monkeypatch)
    coordinator.begin()
    timer.stop_count = 0
    client.requests.clear()
    state["closing"] = closing
    state["launching"] = launching

    coordinator.tick()

    assert timer.stop_count == 1
    assert client.requests == []


def test_request_does_nothing_when_closing_or_not_launching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        coordinator,
        _timer,
        client,
        _bridge,
        _app,
        _ready,
        _timeouts,
        _now,
        state,
    ) = make_coordinator(monkeypatch)

    state["closing"] = True
    coordinator.request()
    assert client.requests == []
    state["closing"] = False
    state["launching"] = False
    coordinator.request()
    assert client.requests == []
