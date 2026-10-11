from __future__ import annotations

from typing import Any, cast

from PySide6.QtCore import QCoreApplication, QObject, QTimer

from codex_bridge.console import main_window as main_window_module
from codex_bridge.console.timer_bootstrap import build_console_timers

_APP: QCoreApplication | None = None


def _application() -> QCoreApplication:
    global _APP
    if _APP is None:
        application = QCoreApplication.instance()
        _APP = application if application is not None else QCoreApplication([])
    return _APP


class FakeMainWindow(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.timeout_calls: list[str] = []
        callback_names = (
            "_request_health",
            "_request_threads",
            "_request_selected_status",
            "_request_selected_history",
            "_on_readiness_tick",
            "_on_bridge_start_retry_timeout",
            "_on_usage_initial_timeout",
            "_on_usage_retry_timeout",
            "_on_usage_poll_timeout",
            "_on_turn_usage_snapshot_timeout",
            "_on_codex_update_auto_timeout",
            "_on_stop_confirmation_tick",
            "_on_diagnostics_timeout",
            "_on_exit_timeout",
            "_on_signal_tick",
        )
        for callback_name in callback_names:

            def callback(name: str = callback_name) -> None:
                self.timeout_calls.append(name)

            setattr(self, callback_name, callback)


TIMER_SPECS = (
    ("health_timer", 5_000, False, True, "_request_health"),
    ("thread_timer", 10_000, False, True, "_request_threads"),
    ("selected_status_timer", 5_000, False, False, "_request_selected_status"),
    ("_history_refresh_timer", 250, True, False, "_request_selected_history"),
    ("readiness_timer", 350, False, False, "_on_readiness_tick"),
    ("bridge_start_retry_timer", 0, True, False, "_on_bridge_start_retry_timeout"),
    ("usage_initial_timer", 1_500, True, False, "_on_usage_initial_timeout"),
    ("usage_retry_timer", 3_000, True, False, "_on_usage_retry_timeout"),
    ("usage_poll_timer", 60_000, False, False, "_on_usage_poll_timeout"),
    ("turn_usage_snapshot_timer", 1_000, True, False, "_on_turn_usage_snapshot_timeout"),
    ("codex_update_auto_timer", 5_000, True, False, "_on_codex_update_auto_timeout"),
    ("stop_confirmation_timer", 350, False, False, "_on_stop_confirmation_tick"),
    ("diagnostics_timer", 1_000, False, False, "_on_diagnostics_timeout"),
    ("exit_timer", 12_000, True, False, "_on_exit_timeout"),
    ("signal_timer", 50, False, True, "_on_signal_tick"),
)


def test_console_timers_preserve_parent_intervals_flags_callbacks_and_activity() -> None:
    _application()
    window = FakeMainWindow()

    build_console_timers(cast(Any, window), exit_timeout_seconds=12.0)

    for timer_name, interval, single_shot, active, _callback_name in TIMER_SPECS:
        timer = getattr(window, timer_name)
        assert isinstance(timer, QTimer)
        assert timer.parent() is window
        assert timer.interval() == interval
        assert timer.isSingleShot() is single_shot
        assert timer.isActive() is active

    for timer_name, _interval, _single_shot, _active, callback_name in TIMER_SPECS:
        getattr(window, timer_name).timeout.emit()
        assert window.timeout_calls[-1] == callback_name

    assert window.timeout_calls == [spec[4] for spec in TIMER_SPECS]
    for timer_name, _interval, _single_shot, _active, _callback_name in TIMER_SPECS:
        getattr(window, timer_name).stop()


def test_main_window_wrapper_passes_current_exit_timeout_to_timer_and_deadline(
    monkeypatch: Any,
) -> None:
    _application()
    window = FakeMainWindow()
    window._closing = False
    window._history_requests = type("HistoryRequests", (), {"reset": lambda self: None})()
    window._codex_probe = type("CodexProbe", (), {"abort": lambda self: None})()
    window._tunnel = type("Tunnel", (), {"close": lambda self, *, on_finished: None})()
    window._on_exit_tunnel_stopped = lambda: None
    monkeypatch.setattr(main_window_module, "_EXIT_TIMEOUT_SECONDS", 8.25)
    monkeypatch.setattr(main_window_module, "monotonic", lambda: 100.0)

    main_window_module.MainWindow._build_timers(cast(Any, window))

    assert window.exit_timer.interval() == 8_250
    main_window_module.MainWindow._begin_exit(cast(Any, window))
    assert window._exit_deadline == 108.25

    for timer_name, _interval, _single_shot, _active, _callback_name in TIMER_SPECS:
        getattr(window, timer_name).stop()
