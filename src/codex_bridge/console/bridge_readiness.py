from __future__ import annotations

from collections.abc import Callable, Mapping
from time import monotonic
from typing import Protocol

_READINESS_TIMEOUT_SECONDS = 10.0


class BridgeReadinessClient(Protocol):
    def get_json(
        self,
        path: str,
        *,
        key: str,
        query: Mapping[str, object] | None = None,
    ) -> bool: ...


class ReadinessTimer(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...


class ReadinessLabel(Protocol):
    def setText(self, text: str) -> None: ...


class BridgeReadinessCoordinator:
    def __init__(
        self,
        timer: ReadinessTimer,
        client: BridgeReadinessClient,
        bridge_status_label: ReadinessLabel,
        app_server_status_label: ReadinessLabel,
        *,
        is_closing: Callable[[], bool],
        is_launch_in_progress: Callable[[], bool],
        on_ready: Callable[[], None],
        on_timeout: Callable[[], None],
    ) -> None:
        self._timer = timer
        self._client = client
        self._bridge_status_label = bridge_status_label
        self._app_server_status_label = app_server_status_label
        self._is_closing = is_closing
        self._is_launch_in_progress = is_launch_in_progress
        self._on_ready = on_ready
        self._on_timeout = on_timeout
        self._deadline = 0.0
        self._health_ok = False
        self._bridge_ready = False
        self._app_server_ready = False

    @property
    def deadline(self) -> float:
        return self._deadline

    @deadline.setter
    def deadline(self, value: float) -> None:
        self._deadline = value

    def begin(self) -> None:
        self._deadline = monotonic() + _READINESS_TIMEOUT_SECONDS
        self._health_ok = False
        self._bridge_ready = False
        self._app_server_ready = False
        self._timer.start()
        self.request()

    def request(self) -> None:
        if self._is_closing() or not self._is_launch_in_progress():
            return
        self._client.get_json("/healthz", key="launch:health")
        self._client.get_json("/ui-api/status", key="launch:status")

    def tick(self) -> None:
        if self._is_closing() or not self._is_launch_in_progress():
            self._timer.stop()
            return
        if monotonic() >= self._deadline:
            self._timer.stop()
            self._on_timeout()
            return
        self.request()

    def apply_health(self, payload: object) -> None:
        self._health_ok = isinstance(payload, Mapping) and payload.get("status") == "ok"
        self._notify_if_ready()

    def apply_status(self, payload: object) -> None:
        if not isinstance(payload, Mapping):
            return
        bridge = payload.get("bridge")
        app_server = payload.get("app_server")
        self._bridge_ready = bridge in {"ready", "connected"}
        self._app_server_ready = app_server in {"ready", "connected"}
        self._bridge_status_label.setText(
            "Bridge: connected" if self._bridge_ready else "Bridge: disconnected"
        )
        self._app_server_status_label.setText(
            "App Server: ready" if self._app_server_ready else "App Server: failed"
        )
        self._notify_if_ready()

    def fail_health(self) -> None:
        self._health_ok = False

    def fail_status(self) -> None:
        self._bridge_ready = False
        self._app_server_ready = False

    def ready(self) -> bool:
        return (
            self._is_launch_in_progress()
            and self._health_ok
            and self._bridge_ready
            and self._app_server_ready
        )

    def _notify_if_ready(self) -> None:
        if self.ready():
            self._on_ready()
