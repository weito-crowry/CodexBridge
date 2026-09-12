from __future__ import annotations

from typing import Any

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtNetwork import QNetworkRequest
from PySide6.QtWidgets import QApplication

from codex_bridge.console.tunnel_supervisor import TunnelSupervisor, parse_tunnel_version


class Signal:
    def __init__(self) -> None:
        self._slots: list[Any] = []

    def connect(self, slot: Any) -> None:
        self._slots.append(slot)

    def emit(self, *args: Any) -> None:
        for slot in tuple(self._slots):
            slot(*args)


class FakeProcess:
    def __init__(self) -> None:
        self.started = Signal()
        self.finished = Signal()
        self.errorOccurred = Signal()
        self.readyReadStandardOutput = Signal()
        self.readyReadStandardError = Signal()
        self.program: str | None = None
        self.arguments: list[str] = []
        self.stdout = b""
        self.stderr = b""
        self.exit_code = 0
        self.start_calls = 0
        self.terminate_calls = 0
        self.kill_calls = 0
        self.delete_calls = 0
        self.set_environment_calls = 0

    def setProgram(self, program: str) -> None:
        self.program = program

    def setArguments(self, arguments: list[str]) -> None:
        self.arguments = arguments

    def setProcessEnvironment(self, _environment: object) -> None:
        self.set_environment_calls += 1

    def start(self) -> None:
        self.start_calls += 1

    def readAllStandardOutput(self) -> bytes:
        output, self.stdout = self.stdout, b""
        return output

    def readAllStandardError(self) -> bytes:
        output, self.stderr = self.stderr, b""
        return output

    def exitCode(self) -> int:
        return self.exit_code

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1

    def deleteLater(self) -> None:
        self.delete_calls += 1


class FakeReply:
    def __init__(self, url: str) -> None:
        self.finished = Signal()
        self.url = url
        self.status = 0
        self.body = b""
        self.aborted = False
        self.deleted = False
        self.read_calls = 0

    def attribute(self, _attribute: QNetworkRequest.Attribute) -> int:
        return self.status

    def readAll(self) -> bytes:
        self.read_calls += 1
        body, self.body = self.body, b""
        return body

    def abort(self) -> None:
        self.aborted = True

    def deleteLater(self) -> None:
        self.deleted = True


class FakeNetworkManager:
    def __init__(self) -> None:
        self.replies: list[FakeReply] = []

    def get(self, request: QNetworkRequest) -> FakeReply:
        reply = FakeReply(request.url().toString())
        self.replies.append(reply)
        return reply


def _application() -> QApplication:
    application = QCoreApplication.instance()
    return application if isinstance(application, QApplication) else QApplication([])


def _doctor(
    process: FakeProcess,
    *,
    network: FakeNetworkManager | None = None,
) -> TunnelSupervisor:
    supervisor = TunnelSupervisor(
        executable="C:/tools/tunnel-client.exe",
        profile="codex-bridge",
        process_factory=lambda _parent: process,
        network_manager=network or FakeNetworkManager(),
        health_port_provider=lambda: 41001,
    )
    supervisor.set_bridge_ready(True)
    return supervisor


@pytest.mark.parametrize(
    ("output", "expected"),
    [(b"0.0.14\n", "0.0.14"), (b"tunnel-client 0.0.15\n", "0.0.15")],
)
def test_tunnel_version_parser_uses_leading_semantic_version(output, expected) -> None:
    assert parse_tunnel_version(output) == expected


@pytest.mark.parametrize("output", [b"", b"version unknown", b"0.0", b"0.0.13\n"])
def test_tunnel_version_parser_rejects_malformed_or_old_versions(output) -> None:
    with pytest.raises(ValueError):
        parse_tunnel_version(output, minimum="0.0.14")


def test_version_preflight_runs_before_doctor_and_rejects_old_client() -> None:
    _application()
    version = FakeProcess()
    doctor = FakeProcess()
    processes = [version, doctor]
    supervisor = TunnelSupervisor(
        executable="C:/tools/tunnel-client.exe",
        profile="codex-bridge",
        process_factory=lambda _parent: processes.pop(0),
        validate_version=True,
        network_manager=FakeNetworkManager(),
        health_port_provider=lambda: 41001,
    )

    supervisor.set_bridge_ready(True)

    assert version.arguments == ["--version"]
    assert doctor.start_calls == 0
    version.stdout = b"tunnel-client 0.0.9\n"
    version.readyReadStandardOutput.emit()
    version.finished.emit(0, 0)

    assert supervisor.state == "failed"
    assert supervisor.client_version is None
    assert doctor.start_calls == 0
    supervisor.close()


def test_version_preflight_allows_supported_future_client_then_runs_doctor() -> None:
    _application()
    version = FakeProcess()
    doctor = FakeProcess()
    processes = [version, doctor]
    supervisor = TunnelSupervisor(
        executable="C:/tools/tunnel-client.exe",
        profile="codex-bridge",
        process_factory=lambda _parent: processes.pop(0),
        validate_version=True,
        network_manager=FakeNetworkManager(),
        health_port_provider=lambda: 41001,
    )

    supervisor.set_bridge_ready(True)
    version.stdout = b"tunnel-client 0.0.99\n"
    version.readyReadStandardOutput.emit()
    version.finished.emit(0, 0)

    assert supervisor.client_version == "0.0.99"
    assert doctor.arguments[:2] == ["doctor", "--profile"]
    supervisor.close()


def test_doctor_uses_direct_process_with_profile_and_ephemeral_health_option() -> None:
    _application()
    process = FakeProcess()
    supervisor = _doctor(process)

    assert process.start_calls == 1
    assert process.program == "C:/tools/tunnel-client.exe"
    assert process.arguments == [
        "doctor",
        "--profile",
        "codex-bridge",
        "--explain",
        "--health.listen-addr",
        "127.0.0.1:0",
    ]
    assert process.set_environment_calls == 0
    supervisor.close()


def test_successful_doctor_autostarts_and_discards_raw_output() -> None:
    _application()
    process = FakeProcess()
    messages: list[str] = []
    supervisor = _doctor(process)
    supervisor.message_changed.connect(messages.append)
    process.stdout = b"Tunnel ID: private-identity"
    process.stderr = b"CONTROL_PLANE_API_KEY=private-secret"
    process.readyReadStandardOutput.emit()
    process.readyReadStandardError.emit()
    process.finished.emit(0, 0)

    assert supervisor.state == "starting"
    assert messages[-1] == "Tunnel: starting"
    assert all("private" not in message.casefold() for message in messages)
    supervisor.close()


def test_failed_or_timed_out_doctor_is_safe_and_does_not_expose_output() -> None:
    _application()
    failed_process = FakeProcess()
    failed_process.stderr = b"private tunnel hostname and token"
    messages: list[str] = []
    failed = _doctor(failed_process)
    failed.message_changed.connect(messages.append)
    failed_process.readyReadStandardError.emit()
    failed_process.exit_code = 1
    failed_process.finished.emit(1, 0)

    assert failed.state == "failed"
    assert messages[-1] == "Tunnel: configuration check failed"
    assert all("private" not in message.casefold() for message in messages)
    failed.close()

    timeout_process = FakeProcess()
    timed_out = _doctor(timeout_process)
    timed_out._on_doctor_timeout()

    assert timeout_process.kill_calls == 1
    assert timed_out.state == "failed"
    timed_out.close()


def test_doctor_combined_output_is_bounded_and_runs_once() -> None:
    _application()
    process = FakeProcess()
    supervisor = _doctor(process)
    process.stdout = b"x" * (8 * 1024 + 1)
    process.readyReadStandardOutput.emit()
    supervisor.set_bridge_ready(True)

    assert process.kill_calls == 1
    assert process.start_calls == 1
    assert supervisor.state == "failed"
    supervisor.close()


def test_tunnel_start_uses_one_managed_process_and_ephemeral_health_port() -> None:
    _application()
    doctor = FakeProcess()
    tunnel = FakeProcess()
    network = FakeNetworkManager()
    processes = [doctor, tunnel]
    ports = iter((41234, 45678))
    supervisor = TunnelSupervisor(
        executable="C:/tools/tunnel-client.exe",
        profile="codex-bridge",
        process_factory=lambda _parent: processes.pop(0),
        network_manager=network,
        health_port_provider=lambda: next(ports),
    )
    supervisor.set_bridge_ready(True)
    doctor.finished.emit(0, 0)

    assert not supervisor.start()
    assert not supervisor.start()
    assert tunnel.start_calls == 1
    assert supervisor.state == "starting"
    assert tunnel.program == "C:/tools/tunnel-client.exe"
    assert tunnel.arguments == [
        "run",
        "--profile",
        "codex-bridge",
        "--health.listen-addr",
        "127.0.0.1:41234",
    ]
    tunnel.started.emit()
    assert supervisor.state == "running"
    supervisor.close()


def test_unexpected_exit_fails_without_automatic_restart() -> None:
    _application()
    doctor = FakeProcess()
    tunnel = FakeProcess()
    processes = [doctor, tunnel]
    supervisor = TunnelSupervisor(
        executable="tunnel-client.exe",
        profile="codex-bridge",
        process_factory=lambda _parent: processes.pop(0),
        network_manager=FakeNetworkManager(),
        health_port_provider=lambda: 41001,
    )
    supervisor.set_bridge_ready(True)
    doctor.finished.emit(0, 0)
    supervisor.start()
    tunnel.started.emit()
    tunnel.finished.emit(1, 0)

    assert supervisor.state == "failed"
    assert processes == []
    supervisor.close()


def _managed_supervisor(processes: list[FakeProcess]) -> TunnelSupervisor:
    supervisor = TunnelSupervisor(
        executable="tunnel-client.exe",
        profile="codex-bridge",
        process_factory=lambda _parent: processes.pop(0),
        network_manager=FakeNetworkManager(),
        health_port_provider=iter(range(41001, 41020)).__next__,
    )
    supervisor.set_bridge_ready(True)
    return supervisor


def test_doctor_pass_and_bridge_ready_auto_starts_tunnel() -> None:
    _application()
    doctor = FakeProcess()
    tunnel = FakeProcess()
    supervisor = _managed_supervisor([doctor, tunnel])

    doctor.finished.emit(0, 0)

    assert tunnel.start_calls == 1
    assert supervisor.state == "starting"
    supervisor.close()


def test_bridge_not_ready_never_auto_starts_tunnel() -> None:
    _application()
    doctor = FakeProcess()
    tunnel = FakeProcess()
    supervisor = TunnelSupervisor(
        executable="tunnel-client.exe",
        profile="codex-bridge",
        process_factory=lambda _parent: [doctor, tunnel].pop(0),
        network_manager=FakeNetworkManager(),
        health_port_provider=lambda: 41001,
    )
    supervisor._start_doctor_once()

    doctor.finished.emit(0, 0)

    assert tunnel.start_calls == 0
    assert supervisor.state == "unavailable"
    supervisor.close()


def test_unexpected_exit_uses_bounded_then_fallback_recovery_delays() -> None:
    _application()
    doctor = FakeProcess()
    tunnels = [FakeProcess() for _ in range(6)]
    supervisor = _managed_supervisor([doctor, *tunnels])
    doctor.finished.emit(0, 0)

    expected_delays = [1_000, 3_000, 10_000, 30_000, 60_000, 60_000]
    for tunnel, expected_delay in zip(tunnels, expected_delays, strict=True):
        tunnel.finished.emit(1, 0)
        assert supervisor.recovery_timer.isActive()
        assert supervisor.recovery_timer.interval() == expected_delay
        if tunnel is not tunnels[-1]:
            supervisor._on_recovery_timeout()
    supervisor.close()


def test_successful_running_state_resets_recovery_sequence() -> None:
    _application()
    doctor = FakeProcess()
    first = FakeProcess()
    second = FakeProcess()
    third = FakeProcess()
    supervisor = _managed_supervisor([doctor, first, second, third])
    doctor.finished.emit(0, 0)

    first.started.emit()
    first.finished.emit(1, 0)
    assert supervisor.recovery_timer.interval() == 1_000
    supervisor._on_recovery_timeout()
    second.started.emit()
    assert supervisor._recovery_index == 1
    health_reply = supervisor._network_manager.replies[-1]
    health_reply.status = 200
    health_reply.finished.emit()
    ready_reply = supervisor._network_manager.replies[-1]
    ready_reply.status = 200
    ready_reply.finished.emit()
    assert supervisor.state == "ready"
    assert supervisor._recovery_index == 0
    second.finished.emit(1, 0)

    assert supervisor.recovery_timer.interval() == 1_000
    supervisor.close()


def test_explicit_stop_does_not_schedule_recovery() -> None:
    _application()
    doctor = FakeProcess()
    tunnel = FakeProcess()
    supervisor = _managed_supervisor([doctor, tunnel])
    doctor.finished.emit(0, 0)
    tunnel.started.emit()

    assert supervisor.stop()
    tunnel.finished.emit(0, 0)

    assert not supervisor.recovery_timer.isActive()
    supervisor.close()


def test_close_does_not_schedule_recovery() -> None:
    _application()
    doctor = FakeProcess()
    tunnel = FakeProcess()
    supervisor = _managed_supervisor([doctor, tunnel])
    doctor.finished.emit(0, 0)
    tunnel.started.emit()

    supervisor.close()
    tunnel.finished.emit(1, 0)

    assert not supervisor.recovery_timer.isActive()


def test_retry_now_attempts_immediately_when_bridge_and_preflight_are_ready() -> None:
    _application()
    doctor = FakeProcess()
    first = FakeProcess()
    second = FakeProcess()
    supervisor = _managed_supervisor([doctor, first, second])
    doctor.finished.emit(0, 0)
    first.started.emit()
    first.finished.emit(1, 0)
    assert supervisor.recovery_timer.isActive()

    assert supervisor.retry_now()

    assert second.start_calls == 1
    assert supervisor.state == "starting"
    supervisor.close()


def test_stop_terminates_then_kills_after_bounded_timeout() -> None:
    _application()
    doctor = FakeProcess()
    tunnel = FakeProcess()
    processes = [doctor, tunnel]
    supervisor = TunnelSupervisor(
        executable="tunnel-client.exe",
        profile="codex-bridge",
        process_factory=lambda _parent: processes.pop(0),
        network_manager=FakeNetworkManager(),
        health_port_provider=lambda: 41001,
    )
    supervisor.set_bridge_ready(True)
    doctor.finished.emit(0, 0)
    supervisor.start()
    tunnel.started.emit()

    assert supervisor.stop()
    assert supervisor.state == "stopping"
    assert tunnel.terminate_calls == 1
    assert not supervisor.stop()
    supervisor._on_stop_timeout()
    assert tunnel.kill_calls == 1
    tunnel.finished.emit(0, 0)
    assert supervisor.state == "stopped"
    supervisor.close()


def test_restart_starts_second_process_only_after_first_finished_with_new_port() -> None:
    _application()
    doctor = FakeProcess()
    first = FakeProcess()
    second = FakeProcess()
    processes = [doctor, first, second]
    network = FakeNetworkManager()
    supervisor = TunnelSupervisor(
        executable="tunnel-client.exe",
        profile="codex-bridge",
        process_factory=lambda _parent: processes.pop(0),
        network_manager=network,
        health_port_provider=iter((41001, 41002)).__next__,
    )
    supervisor.set_bridge_ready(True)
    doctor.finished.emit(0, 0)
    supervisor.start()
    first.started.emit()

    assert supervisor.restart()
    assert supervisor.state == "stopping"
    assert first.terminate_calls == 1
    assert second.start_calls == 0
    first.finished.emit(0, 0)

    assert second.start_calls == 1
    assert supervisor.state == "starting"
    assert second.arguments[-1] == "127.0.0.1:41002"
    supervisor.close()


def test_health_and_readiness_use_status_only_and_discard_response_body() -> None:
    _application()
    doctor = FakeProcess()
    tunnel = FakeProcess()
    network = FakeNetworkManager()
    processes = [doctor, tunnel]
    supervisor = TunnelSupervisor(
        executable="tunnel-client.exe",
        profile="codex-bridge",
        process_factory=lambda _parent: processes.pop(0),
        network_manager=network,
        health_port_provider=lambda: 41001,
    )
    supervisor.set_bridge_ready(True)
    doctor.finished.emit(0, 0)
    supervisor.start()
    tunnel.started.emit()

    health_reply = network.replies[-1]
    assert health_reply.url.endswith("/healthz")
    health_reply.status = 204
    health_reply.body = b"private body must not be shown"
    health_reply.finished.emit()
    assert supervisor.state == "running"

    ready_reply = network.replies[-1]
    assert ready_reply.url.endswith("/readyz")
    ready_reply.status = 200
    ready_reply.body = b"private readiness body"
    ready_reply.finished.emit()
    assert supervisor.state == "ready"
    assert health_reply.read_calls == 1
    assert ready_reply.read_calls == 1
    supervisor.close()


def test_startup_timeout_becomes_not_ready_and_later_ready_recovers() -> None:
    _application()
    doctor = FakeProcess()
    tunnel = FakeProcess()
    network = FakeNetworkManager()
    processes = [doctor, tunnel]
    supervisor = TunnelSupervisor(
        executable="tunnel-client.exe",
        profile="codex-bridge",
        process_factory=lambda _parent: processes.pop(0),
        network_manager=network,
        health_port_provider=lambda: 41001,
    )
    supervisor.set_bridge_ready(True)
    doctor.finished.emit(0, 0)
    supervisor.start()
    tunnel.started.emit()
    supervisor._startup_deadline = 0.0
    supervisor._on_health_poll_tick()

    assert supervisor.state == "not_ready"
    assert supervisor._health_timer.interval() == 5_000

    health_reply = network.replies[-1]
    health_reply.status = 200
    health_reply.finished.emit()
    ready_reply = network.replies[-1]
    ready_reply.status = 200
    ready_reply.finished.emit()

    assert supervisor.state == "ready"
    supervisor.close()


def test_steady_health_poll_rechecks_readiness_and_recovers() -> None:
    _application()
    doctor = FakeProcess()
    tunnel = FakeProcess()
    network = FakeNetworkManager()
    processes = [doctor, tunnel]
    supervisor = TunnelSupervisor(
        executable="tunnel-client.exe",
        profile="codex-bridge",
        process_factory=lambda _parent: processes.pop(0),
        network_manager=network,
        health_port_provider=lambda: 41001,
    )
    supervisor.set_bridge_ready(True)
    doctor.finished.emit(0, 0)
    supervisor.start()
    tunnel.started.emit()

    initial_health = network.replies[-1]
    initial_health.status = 200
    initial_health.finished.emit()
    initial_ready = network.replies[-1]
    initial_ready.status = 200
    initial_ready.finished.emit()
    assert supervisor.state == "ready"

    supervisor._poll_health()
    steady_health = network.replies[-2]
    steady_ready = network.replies[-1]
    assert steady_health.url.endswith("/healthz")
    assert steady_ready.url.endswith("/readyz")
    assert len(steady_ready.finished._slots) == 1

    steady_health.status = 200
    steady_health.finished.emit()
    steady_ready.status = 503
    steady_ready.finished.emit()
    assert supervisor.state == "not_ready"

    supervisor._poll_health()
    recovery_health = network.replies[-2]
    recovery_ready = network.replies[-1]
    recovery_health.status = 200
    recovery_health.finished.emit()
    recovery_ready.status = 200
    recovery_ready.finished.emit()

    assert supervisor.state == "ready"
    supervisor.close()
