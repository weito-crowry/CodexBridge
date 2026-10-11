from __future__ import annotations

from typing import TYPE_CHECKING

from .usage import parse_codex_usage

if TYPE_CHECKING:
    from .main_window import MainWindow


_USAGE_MAX_ATTEMPTS = 3


class UsageRequestCoordinator:
    def __init__(self, window: MainWindow) -> None:
        self._window = window
        self._sequence_active = False
        self._attempts = 0
        self._request_in_flight = False
        self._request_mode: str | None = None

    @property
    def sequence_active(self) -> bool:
        return self._sequence_active

    @sequence_active.setter
    def sequence_active(self, value: bool) -> None:
        self._sequence_active = value

    @property
    def attempts(self) -> int:
        return self._attempts

    @attempts.setter
    def attempts(self, value: int) -> None:
        self._attempts = value

    @property
    def request_in_flight(self) -> bool:
        return self._request_in_flight

    @request_in_flight.setter
    def request_in_flight(self, value: bool) -> None:
        self._request_in_flight = value

    @property
    def request_mode(self) -> str | None:
        return self._request_mode

    @request_mode.setter
    def request_mode(self, value: str | None) -> None:
        self._request_mode = value

    def request_usage(self, *, force: bool = False) -> None:
        window = self._window
        if window._closing:
            return
        if force:
            if self._request_in_flight:
                return
            window.usage_initial_timer.stop()
            window.usage_retry_timer.stop()
            self._sequence_active = False
            self._attempts = 0
            self._request_mode = "manual"
            self.start_request("manual")
            return
        self.start_request("initial")

    def begin_sequence(self) -> None:
        window = self._window
        self._sequence_active = True
        self._attempts = 0
        if not self._request_in_flight:
            self._request_mode = None
        window.usage_initial_timer.start()
        window.usage_retry_timer.stop()
        window.usage_poll_timer.start()
        if window._pending_usage_snapshot_turns and not window.turn_usage_snapshot_timer.isActive():
            window._start_usage_snapshot_timer()

    def invalidate_sequence(self) -> None:
        window = self._window
        self._sequence_active = False
        self._attempts = 0
        window.usage_initial_timer.stop()
        window.usage_retry_timer.stop()
        window.usage_poll_timer.stop()
        window.turn_usage_snapshot_timer.stop()
        if self._request_in_flight:
            window._client.abort_json_group("usage")
        if self._request_mode == "turn_snapshot":
            window._usage_snapshot_request_turns.clear()
        self._request_in_flight = False
        self._request_mode = None

    def start_request(self, mode: str) -> bool:
        window = self._window
        if window._closing or self._request_in_flight:
            return False
        if mode != "manual" and not window._usage_ready:
            return False
        if mode == "initial":
            if not self._sequence_active or self._attempts >= _USAGE_MAX_ATTEMPTS:
                return False
            self._attempts += 1
        if window._client.get_json("/ui-api/account/rate-limits", key="usage"):
            self._request_in_flight = True
            self._request_mode = mode
            return True
        elif mode == "initial":
            self._attempts -= 1
        return False

    def initial_timeout(self) -> None:
        self._window.usage_initial_timer.stop()
        self.start_request("initial")

    def retry_timeout(self) -> None:
        self._window.usage_retry_timer.stop()
        if self._sequence_active:
            self.start_request("initial")

    def poll_timeout(self) -> None:
        if self._window._usage_ready:
            self.start_request("periodic")

    def apply_result(self, payload: object) -> None:
        window = self._window
        mode = self._request_mode
        snapshot_candidates = (
            set(window._usage_snapshot_request_turns) if mode == "turn_snapshot" else set()
        )
        self._request_in_flight = False
        self._request_mode = None
        window._usage_snapshot_request_turns.clear()
        if mode != "turn_snapshot":
            self._sequence_active = False
            window.usage_initial_timer.stop()
            window.usage_retry_timer.stop()
        if window._usage_ready:
            window.usage_poll_timer.start()
        if mode == "turn_snapshot":
            snapshot_usage = parse_codex_usage(payload)
            window._finish_turn_usage_snapshot(snapshot_candidates, snapshot_usage)
        else:
            window._apply_usage(payload)
            window._record_usage_history()

    def apply_error(self, message: str) -> None:
        window = self._window
        if not self._request_in_flight:
            return
        mode = self._request_mode
        snapshot_candidates = (
            set(window._usage_snapshot_request_turns) if mode == "turn_snapshot" else set()
        )
        self._request_in_flight = False
        self._request_mode = None
        window._usage_snapshot_request_turns.clear()
        window._apply_usage_failure(message)
        if mode == "turn_snapshot":
            for candidate in snapshot_candidates:
                window._pending_usage_snapshot_turns.discard(candidate)
            if (
                window._pending_usage_snapshot_turns
                and not window.turn_usage_snapshot_timer.isActive()
            ):
                window._start_usage_snapshot_timer()
        if mode != "turn_snapshot":
            if (
                mode == "initial"
                and self._sequence_active
                and window._usage_ready
                and self._attempts < _USAGE_MAX_ATTEMPTS
            ):
                window.usage_retry_timer.start()
            else:
                self._sequence_active = False
