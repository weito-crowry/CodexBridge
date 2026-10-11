from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QLabel,
)

from codex_bridge.console import main_window as main_window_module
from codex_bridge.console import usage_history_window as usage_history_window_module
from codex_bridge.console.main_window import MainWindow
from codex_bridge.console.usage_history import (
    get_recent_usage_events,
    get_usage_samples,
)
from codex_bridge.console.widgets import TimelineEntry
from tests.console_main_window_test_support import (
    FakeClient,
    _application,
    _config,
    _set_usage_ready,
    _usage_window,
)


def test_generic_refresh_does_not_reload_usage() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)

    client.requests.clear()
    window.refresh()

    assert not any(key == "usage" for key, _, _ in client.requests)
    window.close()


def test_force_usage_does_not_change_mode_of_in_flight_initial_request() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)
    window._bridge_ready = True
    window._app_server_ready = True
    window._begin_usage_sequence()
    window._on_usage_initial_timeout()

    window._request_usage(force=True)

    assert window._usage_request_mode == "initial"
    assert sum(key == "usage" for key, _, _ in client.requests) == 1
    window.close()


def test_manual_usage_mode_survives_bridge_readiness_transition() -> None:
    _application()
    client = FakeClient()
    window = _usage_window(client)
    window._request_usage(force=True)
    assert window._usage_request_mode == "manual"

    client.result("bridge-status", {"bridge": "ready", "app_server": "ready"})

    assert window._usage_request_mode == "manual"
    assert sum(key == "usage" for key, _, _ in client.requests) == 1
    window.close()


def test_ready_usage_waits_before_first_request() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)

    client.result("health", {"status": "ok"})
    client.result("bridge-status", {"bridge": "ready", "app_server": "ready"})

    assert not any(key == "usage" for key, _, _ in client.requests)
    assert window.usage_initial_timer.isActive()
    assert window.usage_initial_timer.interval() == 1_500

    window._on_usage_initial_timeout()
    assert sum(key == "usage" for key, _, _ in client.requests) == 1
    window.close()


def test_initial_usage_failure_retries_only_after_delay_and_stops_at_three_attempts() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)

    client.result("health", {"status": "ok"})
    client.result("bridge-status", {"bridge": "ready", "app_server": "ready"})
    assert window.usage_poll_timer.interval() == 60 * 1_000
    window._on_usage_initial_timeout()
    client.failure("usage", "Bridge unavailable")

    assert "refresh failed" in window.usage_status_label.text()
    assert sum(key == "usage" for key, _, _ in client.requests) == 1
    assert window.usage_retry_timer.isActive()
    assert window.usage_retry_timer.interval() == 3_000

    window._on_usage_retry_timeout()
    client.failure("usage", "Bridge unavailable")
    window._on_usage_retry_timeout()
    client.failure("usage", "Bridge unavailable")

    assert sum(key == "usage" for key, _, _ in client.requests) == 3
    assert not window.usage_retry_timer.isActive()
    window.close()


def test_ready_loss_invalidates_pending_usage_retry() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)

    client.result("health", {"status": "ok"})
    client.result("bridge-status", {"bridge": "ready", "app_server": "ready"})
    window._on_usage_initial_timeout()
    client.failure("usage", "Bridge unavailable")
    client.result("bridge-status", {"bridge": "ready", "app_server": "failed"})

    request_count = sum(key == "usage" for key, _, _ in client.requests)
    window._on_usage_retry_timeout()

    assert sum(key == "usage" for key, _, _ in client.requests) == request_count
    assert not window.usage_retry_timer.isActive()
    window.close()


def test_periodic_usage_is_ready_only_and_generic_refresh_does_not_request_it() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)

    client.result("health", {"status": "ok"})
    client.result("bridge-status", {"bridge": "ready", "app_server": "ready"})
    window._on_usage_initial_timeout()
    client.result(
        "usage",
        {"rateLimits": {"primary": {"windowDurationMins": 300, "usedPercent": 10}}},
    )
    client.requests.clear()

    window.refresh()
    assert not any(key == "usage" for key, _, _ in client.requests)

    window._on_usage_poll_timeout()
    assert sum(key == "usage" for key, _, _ in client.requests) == 1
    client.result("bridge-status", {"bridge": "ready", "app_server": "failed"})
    client.requests.clear()
    window._on_usage_poll_timeout()
    assert not any(key == "usage" for key, _, _ in client.requests)
    window.close()


def test_periodic_usage_failure_keeps_last_known_display() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)

    client.result(
        "usage",
        {"rateLimits": {"primary": {"windowDurationMins": 300, "usedPercent": 28}}},
    )
    known = window.usage_status_label.text()
    known_font_weight = window.usage_status_label.font().weight()
    known_color = window.usage_status_label.palette().color(QPalette.ColorRole.WindowText)
    window._bridge_ready = True
    window._app_server_ready = True
    window._on_usage_poll_timeout()
    client.failure("usage", "Bridge unavailable")

    assert window.usage_status_label.text() == f"{known} · refresh failed"
    assert window.usage_status_label.text().startswith("Codex Usage  5h 72% · Week —")
    assert "Bridge unavailable" in window.usage_status_label.toolTip()
    assert "Bridge unavailable" in window.usage_detail_label.text()
    assert window.usage_status_label.font().weight() == known_font_weight
    assert window.usage_status_label.palette().color(QPalette.ColorRole.WindowText) == known_color
    assert window._usage.five_hour is not None
    assert window._usage.five_hour.remaining_percent == 72
    window.close()


def test_periodic_usage_success_updates_values_and_clears_failure_state() -> None:
    _application()
    client = FakeClient()
    window = _usage_window(client)
    window._apply_usage({"rateLimits": {"primary": {"windowDurationMins": 300, "usedPercent": 28}}})
    window._bridge_ready = True
    window._app_server_ready = True
    window._on_usage_poll_timeout()
    client.failure("usage", "temporary failure")

    window._on_usage_poll_timeout()
    client.result(
        "usage",
        {"rateLimits": {"primary": {"windowDurationMins": 300, "usedPercent": 11}}},
    )

    assert window.usage_status_label.text() == "Codex Usage  5h 89% · Week —"
    assert "refresh failed" not in window.usage_status_label.text()
    assert "temporary failure" not in window.usage_detail_label.text()
    window.close()


@pytest.mark.parametrize("mode", ["initial", "periodic", "manual"])
def test_canonical_usage_modes_update_status_and_save_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    _application()
    client = FakeClient()
    database_path = tmp_path / "usage-history.sqlite3"
    window = MainWindow(
        _config(),
        api_client=client,
        tray_available=False,
        usage_history_path=database_path,
    )
    _set_usage_ready(window)
    monkeypatch.setattr(main_window_module, "time", lambda: 120.0)
    window._usage_request_in_flight = True
    window._usage_request_mode = mode

    client.result(
        "usage",
        {
            "rateLimitsByLimitId": {
                "codex": {
                    "primary": {"windowDurationMins": 300, "usedPercent": 28},
                    "secondary": {"windowDurationMins": 10080, "usedPercent": 82},
                }
            }
        },
    )

    assert window.usage_status_label.text() == "Codex Usage  5h 72% · Week 18%"
    assert [
        sample.weekly_remaining
        for sample in get_usage_samples(0.0, 180.0, database_path=database_path)
    ] == [18]
    assert get_recent_usage_events(database_path=database_path) == []
    window.close()


def test_usage_failure_does_not_save_history(tmp_path: Path) -> None:
    _application()
    client = FakeClient()
    database_path = tmp_path / "usage-history.sqlite3"
    window = MainWindow(
        _config(),
        api_client=client,
        tray_available=False,
        usage_history_path=database_path,
    )
    window._usage_request_in_flight = True
    window._usage_request_mode = "periodic"

    client.failure("usage", "temporary failure")

    assert not database_path.exists()
    window.close()


def test_success_with_unavailable_usage_does_not_save_history(tmp_path: Path) -> None:
    _application()
    client = FakeClient()
    database_path = tmp_path / "usage-history.sqlite3"
    window = MainWindow(
        _config(),
        api_client=client,
        tray_available=False,
        usage_history_path=database_path,
    )

    client.result("usage", {"rateLimits": {}})

    assert not database_path.exists()
    window.close()


def test_usage_sample_refreshes_only_a_visible_history_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _application()
    client = FakeClient()
    database_path = tmp_path / "usage-history.sqlite3"
    window = MainWindow(
        _config(),
        api_client=client,
        tray_available=False,
        usage_history_path=database_path,
    )
    current_epoch = [1_790_000_000.0]
    monkeypatch.setattr(main_window_module, "time", lambda: current_epoch[0])
    monkeypatch.setattr(usage_history_window_module, "time", lambda: current_epoch[0])
    _set_usage_ready(window)
    window.usage_history_button.click()
    history_window = window._usage_history_window
    assert history_window is not None
    refresh_calls: list[dict[str, bool]] = []
    monkeypatch.setattr(
        history_window,
        "refresh",
        lambda *, rolling=False: refresh_calls.append({"rolling": rolling}),
    )

    def deliver_sample(weekly_used: int) -> None:
        window._usage_request_in_flight = True
        window._usage_request_mode = "periodic"
        client.result(
            "usage",
            {
                "rateLimits": {
                    "primary": {"windowDurationMins": 300, "usedPercent": 28},
                    "secondary": {
                        "windowDurationMins": 10080,
                        "usedPercent": weekly_used,
                    },
                }
            },
        )

    deliver_sample(70)
    assert refresh_calls == [{"rolling": True}]
    assert (
        len(get_usage_samples(current_epoch[0] - 1, current_epoch[0], database_path=database_path))
        == 1
    )

    history_window.hide()
    current_epoch[0] += 60
    deliver_sample(65)
    assert refresh_calls == [{"rolling": True}]
    assert (
        len(get_usage_samples(current_epoch[0] - 1, current_epoch[0], database_path=database_path))
        == 1
    )

    window._show_status()
    current_epoch[0] += 60
    deliver_sample(60)
    assert refresh_calls == [{"rolling": True}]
    assert (
        len(get_usage_samples(current_epoch[0] - 1, current_epoch[0], database_path=database_path))
        == 1
    )

    history_window.show()
    assert refresh_calls == [{"rolling": True}, {"rolling": True}]
    current_epoch[0] += 60
    deliver_sample(55)
    assert refresh_calls == [{"rolling": True}, {"rolling": True}, {"rolling": True}]
    assert (
        len(get_usage_samples(current_epoch[0] - 1, current_epoch[0], database_path=database_path))
        == 1
    )
    window.close()


@pytest.mark.parametrize("history_error", [sqlite3.OperationalError, OSError])
def test_usage_history_write_failure_keeps_usage_and_polling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    history_error: type[Exception],
) -> None:
    _application()
    client = FakeClient()
    window = MainWindow(
        _config(),
        api_client=client,
        tray_available=False,
        usage_history_path=tmp_path / "usage-history.sqlite3",
    )

    def fail_record(*args: object, **kwargs: object) -> None:
        raise history_error("history unavailable")

    monkeypatch.setattr(main_window_module, "record_usage_sample", fail_record)
    window._usage_request_in_flight = True
    window._usage_request_mode = "periodic"
    _set_usage_ready(window)

    client.result(
        "usage",
        {"rateLimits": {"primary": {"windowDurationMins": 300, "usedPercent": 28}}},
    )

    assert window.usage_status_label.text() == "Codex Usage  5h 72% · Week —"
    assert window.usage_poll_timer.isActive()
    window.close()


def test_usage_failure_message_is_bounded() -> None:
    _application()
    window = _usage_window(FakeClient())

    window._apply_usage_failure("x" * 500)

    assert window._usage_refresh_error == "x" * 256
    assert len(window.usage_detail_label.text()) < 300
    window.close()


def test_completed_activity_schedules_one_delayed_snapshot_for_unique_turn() -> None:
    _application()
    client = FakeClient()
    window = _usage_window(client)
    _set_usage_ready(window)
    window.select_thread("thread-a")
    client.requests.clear()
    event = {
        "activity_id": "completed",
        "thread_id": "thread-a",
        "turn_id": "turn-a",
        "type": "turn_completed",
        "status": "completed",
        "summary": "Turn completed",
    }

    client.activity(window._selection_generation, event)
    client.activity(window._selection_generation, event)

    assert hasattr(window, "turn_usage_snapshot_timer")
    assert window.turn_usage_snapshot_timer.isActive()
    assert window.turn_usage_snapshot_timer.isSingleShot()
    assert window.turn_usage_snapshot_timer.interval() == 1_000
    assert not any(key == "usage" for key, _, _ in client.requests)
    window.turn_usage_snapshot_timer.stop()
    window._on_turn_usage_snapshot_timeout()
    assert sum(key == "usage" for key, _, _ in client.requests) == 1
    assert window._usage_request_mode == "turn_snapshot"
    client.result(
        "usage",
        {"rateLimits": {"primary": {"windowDurationMins": 300, "usedPercent": 28}}},
    )
    client.activity(window._selection_generation, event)
    assert not window.turn_usage_snapshot_timer.isActive()
    assert sum(key == "usage" for key, _, _ in client.requests) == 1
    window.close()


@pytest.mark.parametrize(
    ("activity_type", "status"),
    [
        ("turn_failed", "failed"),
        ("turn_interrupted", "interrupted"),
        ("error", "error"),
        ("turn_completed", "failed"),
        ("turn_completed", "interrupted"),
    ],
)
def test_non_completed_activity_does_not_schedule_usage_snapshot(
    activity_type: str, status: str
) -> None:
    _application()
    client = FakeClient()
    window = _usage_window(client)
    window.select_thread("thread-a")
    client.activity(
        window._selection_generation,
        {
            "activity_id": "terminal",
            "thread_id": "thread-a",
            "turn_id": "turn-a",
            "type": activity_type,
            "status": status,
        },
    )

    assert hasattr(window, "turn_usage_snapshot_timer")
    assert not window.turn_usage_snapshot_timer.isActive()
    window.close()


def test_completed_status_snapshot_does_not_capture_historical_turn_usage() -> None:
    _application()
    client = FakeClient()
    window = _usage_window(client)
    window.select_thread("thread-a")
    status_key = next(key for key, _, _ in client.requests if key.endswith(":status"))
    client.result(
        status_key,
        {
            "thread_id": "thread-a",
            "state": "completed",
            "recent_activities": [
                {
                    "activity_id": "old-completion",
                    "thread_id": "thread-a",
                    "turn_id": "turn-a",
                    "type": "turn_completed",
                    "status": "completed",
                }
            ],
        },
    )

    assert hasattr(window, "turn_usage_snapshot_timer")
    assert not window.turn_usage_snapshot_timer.isActive()
    assert not window._usage_snapshots
    window.close()


def test_completion_with_non_string_turn_id_does_not_schedule_snapshot() -> None:
    _application()
    client = FakeClient()
    window = _usage_window(client)
    window.select_thread("thread-a")
    client.activity(
        window._selection_generation,
        {
            "activity_id": "completed",
            "thread_id": "thread-a",
            "turn_id": 17,
            "type": "turn_completed",
            "status": "completed",
        },
    )

    assert hasattr(window, "turn_usage_snapshot_timer")
    assert not window.turn_usage_snapshot_timer.isActive()
    window.close()


def test_simultaneous_completed_turns_share_one_usage_snapshot_request() -> None:
    _application()
    client = FakeClient()
    window = _usage_window(client)
    _set_usage_ready(window)
    window.select_thread("thread-a")
    for turn_id in ("turn-a", "turn-b"):
        client.activity(
            window._selection_generation,
            {
                "activity_id": f"completed-{turn_id}",
                "thread_id": "thread-a",
                "turn_id": turn_id,
                "type": "turn_completed",
                "status": "completed",
            },
        )

    assert len(window._pending_usage_snapshot_turns) == 2
    window.turn_usage_snapshot_timer.stop()
    window._on_turn_usage_snapshot_timeout()
    assert sum(key == "usage" for key, _, _ in client.requests) == 1
    assert window._usage_snapshot_request_turns == {
        ("thread-a", "turn-a"),
        ("thread-a", "turn-b"),
    }
    client.result(
        "usage",
        {"rateLimits": {"primary": {"windowDurationMins": 300, "usedPercent": 28}}},
    )
    assert window._usage_snapshots[("thread-a", "turn-a")] is not None
    assert window._usage_snapshots[("thread-a", "turn-b")] is not None
    window.close()


@pytest.mark.parametrize("waiting_timer_name", ["usage_initial_timer", "usage_retry_timer"])
def test_turn_snapshot_wait_does_not_cancel_initial_usage_sequence(
    waiting_timer_name: str,
) -> None:
    _application()
    client = FakeClient()
    window = _usage_window(client)
    _set_usage_ready(window)
    window._begin_usage_sequence()
    waiting_timer = getattr(window, waiting_timer_name)
    if waiting_timer_name == "usage_retry_timer":
        window.usage_initial_timer.stop()
        window._usage_attempts = 1
        waiting_timer.start()
    window.select_thread("thread-a")
    client.activity(
        window._selection_generation,
        {
            "activity_id": "completed",
            "thread_id": "thread-a",
            "turn_id": "turn-a",
            "type": "turn_completed",
            "status": "completed",
        },
    )

    window.turn_usage_snapshot_timer.stop()
    window._on_turn_usage_snapshot_timeout()

    assert waiting_timer.isActive()
    assert window._usage_sequence_active
    assert window._usage_request_mode is None
    assert not any(key == "usage" for key, _, _ in client.requests)
    assert window.turn_usage_snapshot_timer.interval() == 500
    window.close()


def test_turn_snapshot_waits_for_existing_usage_request_without_overwriting_mode() -> None:
    _application()
    client = FakeClient()
    window = _usage_window(client)
    _set_usage_ready(window)
    window._on_usage_poll_timeout()
    window.select_thread("thread-a")
    client.activity(
        window._selection_generation,
        {
            "activity_id": "completed",
            "thread_id": "thread-a",
            "turn_id": "turn-a",
            "type": "turn_completed",
            "status": "completed",
        },
    )

    assert hasattr(window, "turn_usage_snapshot_timer")
    window.turn_usage_snapshot_timer.stop()
    window._on_turn_usage_snapshot_timeout()

    assert window._usage_request_mode == "periodic"
    assert sum(key == "usage" for key, _, _ in client.requests) == 1
    assert window.turn_usage_snapshot_timer.isActive()
    assert window.turn_usage_snapshot_timer.interval() == 500
    client.result(
        "usage",
        {"rateLimits": {"primary": {"windowDurationMins": 300, "usedPercent": 20}}},
    )
    assert window._usage_snapshots.get(("thread-a", "turn-a")) is None
    window.turn_usage_snapshot_timer.stop()
    window._on_turn_usage_snapshot_timeout()
    assert window._usage_request_mode == "turn_snapshot"
    assert sum(key == "usage" for key, _, _ in client.requests) == 2
    window.close()


def test_turn_snapshot_is_saved_per_turn_without_changing_global_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _application()
    client = FakeClient()
    database_path = tmp_path / "usage-history.sqlite3"
    current_epoch = [1_790_000_000.0]
    monkeypatch.setattr(main_window_module, "time", lambda: current_epoch[0])
    monkeypatch.setattr(usage_history_window_module, "time", lambda: current_epoch[0])
    window = MainWindow(
        _config(),
        api_client=client,
        tray_available=False,
        usage_history_path=database_path,
    )
    _set_usage_ready(window)
    window._usage_request_in_flight = True
    window._usage_request_mode = "periodic"
    periodic_payload = {
        "rateLimitsByLimitId": {
            "codex": {
                "primary": {"windowDurationMins": 300, "usedPercent": 28},
                "secondary": {"windowDurationMins": 10080, "usedPercent": 82},
            }
        }
    }
    client.result("usage", periodic_payload)
    assert window.usage_status_label.text() == "Codex Usage  5h 72% · Week 18%"
    assert [
        sample.weekly_remaining
        for sample in get_usage_samples(
            current_epoch[0] - 1, current_epoch[0] + 1, database_path=database_path
        )
    ] == [18]

    window.usage_history_button.click()
    history_window = window._usage_history_window
    assert history_window is not None and history_window.isVisible()
    refresh_calls: list[dict[str, bool]] = []
    monkeypatch.setattr(
        history_window,
        "refresh",
        lambda *, rolling=False: refresh_calls.append({"rolling": rolling}),
    )
    current_epoch[0] += 60
    window._usage_request_in_flight = True
    window._usage_request_mode = "periodic"
    client.result("usage", periodic_payload)
    assert refresh_calls == [{"rolling": True}]
    global_usage = window._usage

    window.select_thread("thread-a")
    window._timeline_entries = [
        TimelineEntry("turn-a", "item-a", "Agent", "Agent", "answer", None, ())
    ]
    client.activity(
        window._selection_generation,
        {
            "activity_id": "completed",
            "thread_id": "thread-a",
            "turn_id": "turn-a",
            "type": "turn_completed",
            "status": "completed",
        },
    )
    assert hasattr(window, "turn_usage_snapshot_timer")
    window.turn_usage_snapshot_timer.stop()
    window._on_turn_usage_snapshot_timeout()
    assert window._usage_request_mode == "turn_snapshot"
    current_epoch[0] += 60
    client.result(
        "usage",
        {
            "rateLimits": {
                "primary": {"windowDurationMins": 300, "usedPercent": 28},
                "secondary": {"windowDurationMins": 10080, "usedPercent": 39},
            }
        },
    )

    assert hasattr(window, "_usage_snapshots")
    snapshot = window._usage_snapshots[("thread-a", "turn-a")]
    assert snapshot is not None
    assert snapshot.usage.five_hour is not None
    assert snapshot.usage.five_hour.remaining_percent == 72
    assert snapshot.usage.weekly is not None
    assert snapshot.usage.weekly.remaining_percent == 61
    assert snapshot.captured_at.tzinfo is not None
    assert window._usage is global_usage
    assert window.usage_status_label.text() == "Codex Usage  5h 72% · Week 18%"
    labels = {label.text() for label in window.history_pane.findChildren(QLabel)}
    assert any(
        label.startswith("Usage snapshot: 5h 72% · Week 61% · captured ") for label in labels
    )
    history_samples = get_usage_samples(
        current_epoch[0] - 180, current_epoch[0] + 1, database_path=database_path
    )
    assert [sample.weekly_remaining for sample in history_samples] == [18, 18]
    assert get_recent_usage_events(database_path=database_path) == []
    assert refresh_calls == [{"rolling": True}]
    assert not window._pending_usage_snapshot_turns
    window.close()


def test_turn_snapshot_failure_preserves_current_usage_and_saves_no_snapshot() -> None:
    _application()
    client = FakeClient()
    window = _usage_window(client)
    _set_usage_ready(window)
    window._apply_usage({"rateLimits": {"primary": {"windowDurationMins": 300, "usedPercent": 28}}})
    known = window._usage
    window.select_thread("thread-a")
    client.activity(
        window._selection_generation,
        {
            "activity_id": "completed",
            "thread_id": "thread-a",
            "turn_id": "turn-a",
            "type": "turn_completed",
            "status": "completed",
        },
    )
    assert hasattr(window, "turn_usage_snapshot_timer")
    window.turn_usage_snapshot_timer.stop()
    window._on_turn_usage_snapshot_timeout()
    client.failure("usage", "snapshot unavailable")

    assert window._usage is known
    assert "5h 72%" in window.usage_status_label.text()
    assert "refresh failed" in window.usage_status_label.text()
    assert window._usage_snapshots.get(("thread-a", "turn-a")) is None
    assert not window._pending_usage_snapshot_turns
    window.close()


def test_unavailable_usage_does_not_create_turn_snapshot() -> None:
    _application()
    client = FakeClient()
    window = _usage_window(client)
    _set_usage_ready(window)
    window.select_thread("thread-a")
    client.activity(
        window._selection_generation,
        {
            "activity_id": "completed",
            "thread_id": "thread-a",
            "turn_id": "turn-a",
            "type": "turn_completed",
            "status": "completed",
        },
    )
    assert hasattr(window, "turn_usage_snapshot_timer")
    window.turn_usage_snapshot_timer.stop()
    window._on_turn_usage_snapshot_timeout()
    client.result("usage", {"rateLimits": {}})

    assert window._usage_snapshots.get(("thread-a", "turn-a")) is None
    assert not window._pending_usage_snapshot_turns
    window.close()


def test_usage_snapshot_survives_thread_switch_and_store_is_bounded() -> None:
    _application()
    client = FakeClient()
    window = _usage_window(client)
    _set_usage_ready(window)
    window.select_thread("thread-a")
    client.activity(
        window._selection_generation,
        {
            "activity_id": "completed",
            "thread_id": "thread-a",
            "turn_id": "turn-a",
            "type": "turn_completed",
            "status": "completed",
        },
    )
    assert hasattr(window, "turn_usage_snapshot_timer")
    window.turn_usage_snapshot_timer.stop()
    window._on_turn_usage_snapshot_timeout()
    client.result(
        "usage",
        {"rateLimits": {"primary": {"windowDurationMins": 300, "usedPercent": 28}}},
    )
    window.select_thread("thread-b")
    window.select_thread("thread-a")
    window._apply_items(
        {
            "items": [
                {
                    "turn_id": "turn-a",
                    "item": {"id": "item-a", "type": "agentMessage", "text": "answer"},
                }
            ]
        },
        prepend=False,
    )

    assert any(
        label.text().startswith("Usage snapshot: 5h 72% · Week — · captured ")
        for label in window.history_pane.findChildren(QLabel)
    )

    assert hasattr(window, "_schedule_turn_usage_snapshot")
    for index in range(501):
        window._schedule_turn_usage_snapshot("thread-a", f"turn-{index}")
    assert len(window._usage_snapshots) == 500
    assert ("thread-a", "turn-a") not in window._usage_snapshots
    window.close()
