from __future__ import annotations

from collections.abc import Callable
from time import monotonic
from typing import Protocol

_STOP_CONFIRMATION_TIMEOUT_SECONDS = 10.0


class StopConfirmationTimer(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...


class BridgeStopConfirmationCoordinator:
    def __init__(
        self,
        timer: StopConfirmationTimer,
        *,
        on_confirmed: Callable[[], None],
        on_timed_out: Callable[[], None],
        request_health: Callable[[], None],
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._timer = timer
        self._on_confirmed = on_confirmed
        self._on_timed_out = on_timed_out
        self._request_health = request_health
        self._clock = clock
        self._active = False
        self._health_unavailable = False
        self._status_unavailable = False
        self._deadline = 0.0

    @property
    def active(self) -> bool:
        return self._active

    @property
    def health_unavailable(self) -> bool:
        return self._health_unavailable

    @property
    def status_unavailable(self) -> bool:
        return self._status_unavailable

    @property
    def deadline(self) -> float:
        return self._deadline

    @deadline.setter
    def deadline(self, value: float) -> None:
        self._deadline = value

    def begin(self) -> None:
        self._active = True
        self._health_unavailable = False
        self._status_unavailable = False
        self._deadline = self._clock() + _STOP_CONFIRMATION_TIMEOUT_SECONDS
        self._timer.start()

    def observe_health(self, available: bool) -> None:
        if not self._active:
            return
        self._health_unavailable = not available
        self.maybe_finish()

    def observe_status(self, available: bool) -> None:
        if not self._active:
            return
        self._status_unavailable = not available
        self.maybe_finish()

    def maybe_finish(self) -> None:
        if self._active and self._health_unavailable and self._status_unavailable:
            self.complete()
            self._on_confirmed()

    def tick(self) -> None:
        if not self._active:
            self._timer.stop()
        elif self._health_unavailable and self._status_unavailable:
            self.complete()
            self._on_confirmed()
        elif self._clock() >= self._deadline:
            self.cancel()
            self._on_timed_out()
        else:
            self._request_health()

    def cancel(self) -> None:
        self._active = False
        self._timer.stop()

    def complete(self) -> None:
        self._active = False
        self._timer.stop()
