from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QTimer

from .history_requests import HISTORY_REFRESH_DEBOUNCE_MS
from .usage_snapshots import _USAGE_SNAPSHOT_DELAY_MS

if TYPE_CHECKING:
    from .main_window import MainWindow


_READINESS_INTERVAL_MS = 350
_USAGE_INITIAL_DELAY_MS = 1_500
_USAGE_RETRY_DELAY_MS = 3_000
_USAGE_POLL_INTERVAL_MS = 60 * 1_000
_DIAGNOSTICS_POLL_INTERVAL_MS = 1_000
_STOP_CONFIRMATION_INTERVAL_MS = 350


def build_console_timers(window: MainWindow, *, exit_timeout_seconds: float) -> None:
    window.health_timer = QTimer(window)
    window.health_timer.setInterval(5_000)
    window.health_timer.timeout.connect(window._request_health)
    window.thread_timer = QTimer(window)
    window.thread_timer.setInterval(10_000)
    window.thread_timer.timeout.connect(window._request_threads)
    window.selected_status_timer = QTimer(window)
    window.selected_status_timer.setInterval(5_000)
    window.selected_status_timer.timeout.connect(window._request_selected_status)
    window._history_refresh_timer = QTimer(window)
    window._history_refresh_timer.setSingleShot(True)
    window._history_refresh_timer.setInterval(HISTORY_REFRESH_DEBOUNCE_MS)
    window._history_refresh_timer.timeout.connect(window._request_selected_history)
    window.readiness_timer = QTimer(window)
    window.readiness_timer.setInterval(_READINESS_INTERVAL_MS)
    window.readiness_timer.timeout.connect(window._on_readiness_tick)
    window.bridge_start_retry_timer = QTimer(window)
    window.bridge_start_retry_timer.setSingleShot(True)
    window.bridge_start_retry_timer.timeout.connect(window._on_bridge_start_retry_timeout)
    window.usage_initial_timer = QTimer(window)
    window.usage_initial_timer.setSingleShot(True)
    window.usage_initial_timer.setInterval(_USAGE_INITIAL_DELAY_MS)
    window.usage_initial_timer.timeout.connect(window._on_usage_initial_timeout)
    window.usage_retry_timer = QTimer(window)
    window.usage_retry_timer.setSingleShot(True)
    window.usage_retry_timer.setInterval(_USAGE_RETRY_DELAY_MS)
    window.usage_retry_timer.timeout.connect(window._on_usage_retry_timeout)
    window.usage_poll_timer = QTimer(window)
    window.usage_poll_timer.setInterval(_USAGE_POLL_INTERVAL_MS)
    window.usage_poll_timer.timeout.connect(window._on_usage_poll_timeout)
    window.turn_usage_snapshot_timer = QTimer(window)
    window.turn_usage_snapshot_timer.setSingleShot(True)
    window.turn_usage_snapshot_timer.setInterval(_USAGE_SNAPSHOT_DELAY_MS)
    window.turn_usage_snapshot_timer.timeout.connect(window._on_turn_usage_snapshot_timeout)
    window.codex_update_auto_timer = QTimer(window)
    window.codex_update_auto_timer.setSingleShot(True)
    window.codex_update_auto_timer.setInterval(5_000)
    window.codex_update_auto_timer.timeout.connect(window._on_codex_update_auto_timeout)
    window.stop_confirmation_timer = QTimer(window)
    window.stop_confirmation_timer.setInterval(_STOP_CONFIRMATION_INTERVAL_MS)
    window.stop_confirmation_timer.timeout.connect(window._on_stop_confirmation_tick)
    window.diagnostics_timer = QTimer(window)
    window.diagnostics_timer.setInterval(_DIAGNOSTICS_POLL_INTERVAL_MS)
    window.diagnostics_timer.timeout.connect(window._on_diagnostics_timeout)
    window.exit_timer = QTimer(window)
    window.exit_timer.setSingleShot(True)
    window.exit_timer.setInterval(int(exit_timeout_seconds * 1_000))
    window.exit_timer.timeout.connect(window._on_exit_timeout)
    window.signal_timer = QTimer(window)
    window.signal_timer.setInterval(50)
    window.signal_timer.timeout.connect(window._on_signal_tick)
    window.health_timer.start()
    window.thread_timer.start()
    window.signal_timer.start()
