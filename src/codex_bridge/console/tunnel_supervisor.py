from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import Any

from PySide6.QtCore import QObject, QProcess, QTimer, QUrl, Signal
from PySide6.QtNetwork import (
    QHostAddress,
    QNetworkAccessManager,
    QNetworkRequest,
    QTcpServer,
)

_DOCTOR_TIMEOUT_MS = 10_000
_MAX_DOCTOR_OUTPUT = 8 * 1024
_STOP_TIMEOUT_MS = 2_000
_STARTUP_INTERVAL_MS = 350
_STEADY_INTERVAL_MS = 5_000
_STARTUP_TIMEOUT_SECONDS = 10.0
_HEALTH_HOST = "127.0.0.1"
MIN_SUPPORTED_TUNNEL_VERSION = "0.0.14"
_TUNNEL_VERSION_PATTERN = re.compile(
    r"^\s*[^\d]*(?P<version>\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)"
)
_SEMVER_PATTERN = re.compile(
    r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?:-(?P<prerelease>[0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)

_STATE_LABELS = {
    "unavailable": "Tunnel: unavailable",
    "checking": "Tunnel: checking",
    "ready_to_start": "Tunnel: ready to start",
    "starting": "Tunnel: starting",
    "running": "Tunnel: running",
    "ready": "Tunnel: ready",
    "not_ready": "Tunnel: not ready",
    "stopping": "Tunnel: stopping",
    "stopped": "Tunnel: stopped",
    "failed": "Tunnel: failed",
}


@dataclass(frozen=True, slots=True)
class TunnelActionState:
    start_enabled: bool
    stop_enabled: bool
    restart_enabled: bool


ProcessFactory = Callable[[QObject], Any]
HealthPortProvider = Callable[[], int]


def tunnel_state_label(state: str) -> str:
    return _STATE_LABELS.get(state, "Tunnel: unavailable")


def _version_key(version: str) -> tuple[int, int, int, int]:
    match = _SEMVER_PATTERN.fullmatch(version)
    if match is None:
        raise ValueError("Tunnel version is invalid")
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
        0 if match.group("prerelease") else 1,
    )


def parse_tunnel_version(output: bytes, *, minimum: str = MIN_SUPPORTED_TUNNEL_VERSION) -> str:
    if len(output) > _MAX_DOCTOR_OUTPUT:
        raise ValueError("Tunnel version output is too large")
    try:
        text = output.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Tunnel version output is invalid") from exc
    match = _TUNNEL_VERSION_PATTERN.match(text)
    if match is None:
        raise ValueError("Tunnel version output is invalid")
    version = match.group("version")
    if _version_key(version) < _version_key(minimum):
        raise ValueError(f"Tunnel client {version} is too old; CodexBridge requires >= {minimum}")
    return version


def default_health_port_provider() -> int:
    server = QTcpServer()
    if not server.listen(QHostAddress(_HEALTH_HOST), 0):
        raise RuntimeError("Tunnel health port unavailable")
    port = int(server.serverPort())
    server.close()
    if not 1 <= port <= 65_535:
        raise RuntimeError("Tunnel health port unavailable")
    return port


class TunnelSupervisor(QObject):
    """Own and supervise exactly one Console-launched Tunnel process."""

    state_changed = Signal(str)
    message_changed = Signal(str)
    controls_changed = Signal(bool, bool, bool)

    def __init__(
        self,
        *,
        executable: str | None,
        profile: str,
        process_factory: ProcessFactory | None = None,
        network_manager: Any | None = None,
        health_port_provider: HealthPortProvider | None = None,
        clock: Callable[[], float] = monotonic,
        resolution_source: str = "path",
        validate_version: bool = False,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._executable = executable
        self._resolution_source = resolution_source
        self._validate_version = validate_version
        self._client_version: str | None = None
        self._profile = profile
        self._process_factory = process_factory or (lambda owner: QProcess(owner))
        self._network_manager = network_manager or QNetworkAccessManager(self)
        self._health_port_provider = health_port_provider or default_health_port_provider
        self._clock = clock
        self._state = "unavailable"
        self._bridge_ready = False
        self._doctor_started = False
        self._doctor_passed = False
        self._doctor_process: Any | None = None
        self._doctor_output_size = 0
        self._process: Any | None = None
        self._health_port: int | None = None
        self._health_reply: Any | None = None
        self._ready_reply: Any | None = None
        self._health_ok = False
        self._startup_deadline = 0.0
        self._restart_requested = False
        self._closed = False
        self._stop_callbacks: list[Callable[[], None]] = []
        self._version_process: Any | None = None
        self._version_output = bytearray()
        self._version_checked = False

        self._doctor_timer = QTimer(self)
        self._doctor_timer.setSingleShot(True)
        self._doctor_timer.setInterval(_DOCTOR_TIMEOUT_MS)
        self._doctor_timer.timeout.connect(self._on_doctor_timeout)

        self._stop_timer = QTimer(self)
        self._stop_timer.setSingleShot(True)
        self._stop_timer.setInterval(_STOP_TIMEOUT_MS)
        self._stop_timer.timeout.connect(self._on_stop_timeout)

        self._health_timer = QTimer(self)
        self._health_timer.setInterval(_STARTUP_INTERVAL_MS)
        self._health_timer.timeout.connect(self._on_health_poll_tick)

    @property
    def state(self) -> str:
        return self._state

    @property
    def executable(self) -> str | None:
        return self._executable

    @property
    def resolution_source(self) -> str:
        return self._resolution_source

    @property
    def client_version(self) -> str | None:
        return self._client_version

    @property
    def action_state(self) -> TunnelActionState:
        transition = {"checking", "starting", "stopping"}
        process_running = self._process is not None
        start_enabled = (
            not self._closed
            and self._bridge_ready
            and self._executable is not None
            and self._doctor_passed
            and not process_running
            and self._state not in transition | {"running", "ready", "not_ready"}
        )
        owned_running = process_running and self._state in {"running", "ready", "not_ready"}
        return TunnelActionState(
            start_enabled=start_enabled,
            stop_enabled=not self._closed and owned_running,
            restart_enabled=not self._closed and owned_running,
        )

    def set_bridge_ready(self, ready: bool) -> None:
        if self._closed:
            return
        self._bridge_ready = ready
        if ready:
            if self._validate_version:
                self._start_version_once()
            else:
                self._start_doctor_once()
        self._emit_controls()

    def start(self) -> bool:
        if not self.action_state.start_enabled:
            return False
        return self._start_process()

    def stop(self, *, on_finished: Callable[[], None] | None = None) -> bool:
        if self._process is None:
            if on_finished is not None:
                on_finished()
            return False
        if self._state == "stopping":
            if on_finished is not None:
                self._stop_callbacks.append(on_finished)
            return False
        if on_finished is not None:
            self._stop_callbacks.append(on_finished)
        self._begin_stop()
        return True

    def restart(self) -> bool:
        if not self.action_state.restart_enabled:
            return False
        self._restart_requested = True
        return self.stop()

    def close(self, *, on_finished: Callable[[], None] | None = None) -> None:
        if self._closed:
            if on_finished is not None:
                on_finished()
            return
        self._closed = True
        self._doctor_timer.stop()
        version_process, self._version_process = self._version_process, None
        if version_process is not None:
            version_process.kill()
            version_process.deleteLater()
        doctor_process, self._doctor_process = self._doctor_process, None
        if doctor_process is not None:
            doctor_process.kill()
            doctor_process.deleteLater()
        self._stop_health_poll()
        if self._process is None:
            if on_finished is not None:
                on_finished()
            self._emit_controls()
            return
        self._restart_requested = False
        if on_finished is not None:
            self._stop_callbacks.append(on_finished)
        if self._state != "stopping":
            self._begin_stop()

    def _emit_controls(self) -> None:
        actions = self.action_state
        self.controls_changed.emit(
            actions.start_enabled,
            actions.stop_enabled,
            actions.restart_enabled,
        )

    def _start_version_once(self) -> None:
        if (
            self._version_checked
            or self._version_process is not None
            or self._doctor_started
            or self._closed
        ):
            return
        if self._executable is None:
            self._set_state("unavailable")
            return
        self._version_checked = True
        process: Any | None = None
        try:
            process = self._process_factory(self)
            self._version_process = process
            self._version_output.clear()
            process.finished.connect(
                lambda *_args, process=process: self._on_version_finished(process)
            )
            process.errorOccurred.connect(
                lambda *_args, process=process: self._on_version_error(process)
            )
            process.readyReadStandardOutput.connect(
                lambda process=process: self._read_version_output(process, standard_error=False)
            )
            process.readyReadStandardError.connect(
                lambda process=process: self._read_version_output(process, standard_error=True)
            )
            process.setProgram(self._executable)
            process.setArguments(["--version"])
            process.start()
            self._set_state("checking", message="Tunnel: checking version")
            self._doctor_timer.start()
        except Exception:
            self._fail_version(process)

    def _read_version_output(self, process: Any, *, standard_error: bool) -> None:
        if process is not self._version_process:
            return
        active_process: Any = process
        reader = (
            active_process.readAllStandardError
            if standard_error
            else active_process.readAllStandardOutput
        )
        try:
            chunk = bytes(reader())
        except RuntimeError:
            self._fail_version(process)
            return
        if not standard_error:
            self._version_output.extend(chunk)
        if len(self._version_output) > _MAX_DOCTOR_OUTPUT:
            self._fail_version(process)

    def _on_version_finished(self, process: Any) -> None:
        if process is not self._version_process:
            return
        self._read_version_output(process, standard_error=False)
        self._read_version_output(process, standard_error=True)
        if process is not self._version_process:
            return
        self._doctor_timer.stop()
        self._version_process = None
        active_process: Any = process
        active_process.deleteLater()
        try:
            exit_code = active_process.exitCode()
        except RuntimeError:
            self._fail_version(None)
            return
        if exit_code != 0:
            self._fail_version(None)
            return
        try:
            self._client_version = parse_tunnel_version(bytes(self._version_output))
        except ValueError as exc:
            self._fail_version(None, message=str(exc))
            return
        self._start_doctor_once()

    def _on_version_error(self, process: Any) -> None:
        if process is self._version_process:
            self._fail_version(process)

    def _fail_version(self, process: Any | None, *, message: str | None = None) -> None:
        self._doctor_timer.stop()
        if process is not None and process is self._version_process:
            active_process: Any = process
            self._version_process = None
            active_process.kill()
            active_process.deleteLater()
        self._client_version = None
        self._set_state("failed", message=message or "Tunnel: version check failed")

    def _set_state(self, state: str, *, message: str | None = None) -> None:
        self._state = state
        self.state_changed.emit(state)
        self.message_changed.emit(message or tunnel_state_label(state))
        self._emit_controls()

    def _start_doctor_once(self) -> None:
        if self._doctor_started or self._closed:
            return
        if self._executable is None:
            self._set_state("unavailable")
            return
        self._doctor_started = True
        self._set_state("checking")
        process: Any | None = None
        try:
            process = self._process_factory(self)
            self._doctor_process = process
            self._doctor_output_size = 0
            process.finished.connect(
                lambda *_args, process=process: self._on_doctor_finished(process)
            )
            process.errorOccurred.connect(
                lambda *_args, process=process: self._on_doctor_error(process)
            )
            process.readyReadStandardOutput.connect(
                lambda process=process: self._read_doctor_output(process, standard_error=False)
            )
            process.readyReadStandardError.connect(
                lambda process=process: self._read_doctor_output(process, standard_error=True)
            )
            process.setProgram(self._executable)
            process.setArguments(
                [
                    "doctor",
                    "--profile",
                    self._profile,
                    "--explain",
                    "--health.listen-addr",
                    "127.0.0.1:0",
                ]
            )
            process.start()
            self._doctor_timer.start()
        except Exception:
            self._fail_doctor(process)

    def _read_doctor_output(self, process: Any, *, standard_error: bool) -> None:
        if process is not self._doctor_process:
            return
        active_process: Any = process
        reader = (
            active_process.readAllStandardError
            if standard_error
            else active_process.readAllStandardOutput
        )
        try:
            self._doctor_output_size += len(bytes(reader()))
        except RuntimeError:
            self._fail_doctor(process)
            return
        if self._doctor_output_size > _MAX_DOCTOR_OUTPUT:
            self._fail_doctor(process)

    def _on_doctor_finished(self, process: Any) -> None:
        if process is not self._doctor_process:
            return
        self._read_doctor_output(process, standard_error=False)
        self._read_doctor_output(process, standard_error=True)
        if process is not self._doctor_process:
            return
        self._doctor_timer.stop()
        self._doctor_process = None
        process.deleteLater()
        try:
            exit_code = process.exitCode()
        except RuntimeError:
            self._fail_doctor(None)
            return
        if exit_code == 0:
            self._doctor_passed = True
            self._set_state("ready_to_start" if self._bridge_ready else "unavailable")
        else:
            self._fail_doctor(None)

    def _on_doctor_error(self, process: Any) -> None:
        if process is self._doctor_process:
            self._fail_doctor(process)

    def _on_doctor_timeout(self) -> None:
        if self._version_process is not None:
            self._fail_version(self._version_process)
        else:
            self._fail_doctor(self._doctor_process)

    def _fail_doctor(self, process: Any | None) -> None:
        self._doctor_timer.stop()
        if process is not None and process is self._doctor_process:
            active_process: Any = process
            self._doctor_process = None
            active_process.kill()
            active_process.deleteLater()
        self._doctor_passed = False
        self._set_state("failed", message="Tunnel: configuration check failed")

    def _start_process(self) -> bool:
        executable = self._executable
        if executable is None:
            return False
        try:
            port = self._health_port_provider()
            if isinstance(port, bool) or not 1 <= port <= 65_535:
                raise ValueError("invalid health port")
            process = self._process_factory(self)
            self._process = process
            self._health_port = port
            self._restart_requested = False
            self._set_state("starting")
            process.started.connect(lambda process=process: self._on_process_started(process))
            process.finished.connect(
                lambda *_args, process=process: self._on_process_finished(process)
            )
            process.errorOccurred.connect(
                lambda *_args, process=process: self._on_process_error(process)
            )
            process.setProgram(executable)
            process.setArguments(
                [
                    "run",
                    "--profile",
                    self._profile,
                    "--health.listen-addr",
                    f"{_HEALTH_HOST}:{port}",
                ]
            )
            process.start()
            return True
        except Exception:
            process = self._process
            self._process = None
            if process is not None:
                process.deleteLater()
            self._set_state("failed")
            return False

    def _on_process_started(self, process: Any) -> None:
        if process is not self._process or self._closed:
            return
        self._startup_deadline = self._clock() + _STARTUP_TIMEOUT_SECONDS
        self._health_ok = False
        self._set_state("running")
        self._health_timer.setInterval(_STARTUP_INTERVAL_MS)
        self._health_timer.start()
        self._poll_health()

    def _on_process_error(self, process: Any) -> None:
        if process is not self._process:
            return
        if self._state == "stopping":
            return
        self._finish_unexpected_exit(process)

    def _on_process_finished(self, process: Any) -> None:
        if process is not self._process:
            return
        if self._state == "stopping":
            self._finish_stop(process)
        else:
            self._finish_unexpected_exit(process)

    def _begin_stop(self) -> None:
        process = self._process
        if process is None:
            return
        self._set_state("stopping")
        self._stop_health_poll()
        try:
            process.terminate()
        except Exception:
            self._on_stop_timeout()
            return
        self._stop_timer.start()

    def _on_stop_timeout(self) -> None:
        process = self._process
        if process is None or self._state != "stopping":
            return
        try:
            process.kill()
        except Exception:
            self._finish_stop(process)

    def _finish_stop(self, process: Any) -> None:
        if process is not self._process:
            return
        self._stop_timer.stop()
        self._stop_health_poll()
        self._process = None
        active_process: Any = process
        active_process.deleteLater()
        callbacks = self._stop_callbacks
        self._stop_callbacks = []
        restart = self._restart_requested and not self._closed
        self._restart_requested = False
        self._set_state("stopped")
        for callback in callbacks:
            callback()
        if restart:
            self._start_process()

    def _finish_unexpected_exit(self, process: Any) -> None:
        if process is not self._process:
            return
        self._stop_timer.stop()
        self._stop_health_poll()
        self._process = None
        active_process: Any = process
        active_process.deleteLater()
        self._set_state("failed")

    def _on_health_poll_tick(self) -> None:
        if self._process is None or self._closed:
            self._health_timer.stop()
            return
        if self._state not in {"ready", "not_ready"} and self._clock() >= self._startup_deadline:
            self._health_timer.setInterval(_STEADY_INTERVAL_MS)
            self._set_state("not_ready")
        self._poll_health()

    def _poll_health(self) -> None:
        if self._process is None or self._closed:
            return
        if self._health_reply is None:
            reply = self._get_health_reply("healthz")
            if reply is not None:
                self._health_reply = reply
                reply.finished.connect(lambda reply=reply: self._on_health_finished(reply))
        if self._health_ok:
            self._ensure_ready_reply()

    def _get_health_reply(self, endpoint: str) -> Any | None:
        if self._health_port is None:
            return None
        try:
            url = QUrl(f"http://{_HEALTH_HOST}:{self._health_port}/{endpoint}")
            return self._network_manager.get(QNetworkRequest(url))
        except Exception:
            return None

    @staticmethod
    def _reply_status(reply: Any) -> int:
        try:
            value = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
            return int(value) if isinstance(value, int) else 0
        except (AttributeError, TypeError, ValueError):
            return 0

    @staticmethod
    def _discard_reply(reply: Any) -> None:
        try:
            reply.readAll()
        finally:
            reply.deleteLater()

    def _on_health_finished(self, reply: Any) -> None:
        if reply is not self._health_reply:
            return
        self._health_reply = None
        status = self._reply_status(reply)
        self._discard_reply(reply)
        if self._process is None or self._closed:
            return
        self._health_ok = 200 <= status <= 299
        if not self._health_ok:
            self._abort_ready_reply()
            if self._state == "ready":
                self._health_timer.setInterval(_STEADY_INTERVAL_MS)
                self._set_state("not_ready")
            return
        self._ensure_ready_reply()

    def _ensure_ready_reply(self) -> None:
        if self._ready_reply is not None or self._process is None:
            return
        reply = self._get_health_reply("readyz")
        if reply is not None:
            self._ready_reply = reply
            reply.finished.connect(lambda reply=reply: self._on_ready_finished(reply))

    def _on_ready_finished(self, reply: Any) -> None:
        if reply is not self._ready_reply:
            return
        self._ready_reply = None
        status = self._reply_status(reply)
        self._discard_reply(reply)
        if self._process is None or self._closed:
            return
        if 200 <= status <= 299:
            self._health_timer.setInterval(_STEADY_INTERVAL_MS)
            self._set_state("ready")
        elif self._state == "ready":
            self._set_state("not_ready")

    def _abort_ready_reply(self) -> None:
        reply, self._ready_reply = self._ready_reply, None
        if reply is not None:
            reply.abort()
            reply.deleteLater()

    def _stop_health_poll(self) -> None:
        self._health_timer.stop()
        for attribute in ("_health_reply", "_ready_reply"):
            reply = getattr(self, attribute)
            setattr(self, attribute, None)
            if reply is not None:
                reply.abort()
                reply.deleteLater()
        self._health_ok = False
