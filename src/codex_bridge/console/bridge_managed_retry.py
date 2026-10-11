from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

_BRIDGE_START_RETRY_DELAYS_MS = (2_000, 5_000, 10_000)


class BridgeManagedRetryTimer(Protocol):
    def stop(self) -> None: ...

    def start(self) -> None: ...

    def setInterval(self, msec: int) -> None: ...

    def isActive(self) -> bool: ...


class BridgeManagedRetryCoordinator:
    def __init__(
        self,
        bridge_start_retry_timer: BridgeManagedRetryTimer,
        *,
        get_is_closing: Callable[[], bool],
        can_start_bridge: Callable[[], bool],
        start_bridge_managed: Callable[[], None],
        update_start_button: Callable[[], None],
    ) -> None:
        self._timer = bridge_start_retry_timer
        self._get_is_closing = get_is_closing
        self._can_start_bridge = can_start_bridge
        self._start_bridge_managed = start_bridge_managed
        self._update_start_button = update_start_button
        self._managed_active = False
        self._retry_index = 0
        self._retry_exhausted = False

    @property
    def managed_active(self) -> bool:
        return self._managed_active

    @property
    def retry_index(self) -> int:
        return self._retry_index

    @property
    def retry_exhausted(self) -> bool:
        return self._retry_exhausted

    def activate(self) -> None:
        self._managed_active = True

    def maybe_auto_start_bridge(self) -> None:
        if self._retry_exhausted or self._managed_active:
            return
        if not self._can_start_bridge():
            return
        self.activate()
        self._start_bridge_managed()

    def schedule_retry(self) -> None:
        if self._get_is_closing() or not self._managed_active:
            return
        if self._retry_index >= len(_BRIDGE_START_RETRY_DELAYS_MS):
            self._retry_exhausted = True
            self._managed_active = False
            self._update_start_button()
            return
        delay = _BRIDGE_START_RETRY_DELAYS_MS[self._retry_index]
        self._retry_index += 1
        self._timer.setInterval(delay)
        self._timer.start()

    def on_retry_timeout(self) -> None:
        self._timer.stop()
        if self._get_is_closing() or self._retry_exhausted:
            return
        if self._can_start_bridge():
            self._start_bridge_managed()
        else:
            self._managed_active = False

    def reset_after_ready(self) -> None:
        self._timer.stop()
        self._retry_index = 0
        self._retry_exhausted = False
        self._managed_active = False

    def reset_for_retry_now(self) -> None:
        self._timer.stop()
        self._retry_index = 0
        self._retry_exhausted = False
        self._managed_active = True
