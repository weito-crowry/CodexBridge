from __future__ import annotations

import importlib
import io
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def _load_relay() -> ModuleType | None:
    try:
        return importlib.import_module("scripts.phase5e2c2_stderr_relay")
    except ImportError:
        return None


def _diagnostic_event() -> bytes:
    return (
        b"workflow.startup_diagnostic.persistence_failed "
        b"stage=worker_config_write original_exception=RuntimeError "
        b"persistence_exception=OSError\n"
    )


class _TrackingSource:
    def __init__(self, data: bytes) -> None:
        self._stream = io.BytesIO(data)
        self.read_bytes = 0
        self.eof = False

    def read1(self, size: int) -> bytes:
        chunk = self._stream.read1(size)
        self.read_bytes += len(chunk)
        self.eof = not chunk
        return chunk


def test_relay_emits_only_bounded_valid_diagnostic_events() -> None:
    relay = _load_relay()
    assert relay is not None
    invalid = _diagnostic_event().replace(b"stage=worker_config_write", b"stage=private_path")
    invalid_class = _diagnostic_event().replace(
        b"original_exception=RuntimeError", b"original_exception=token=private"
    )
    private = b"api_key=synthetic-secret C:\\private\\prompt.txt\n"
    source = _TrackingSource(
        private
        + b"x" * (relay.MAX_LINE_BYTES + 10)
        + b"\n"
        + invalid
        + invalid_class
        + _diagnostic_event() * (relay.MAX_EVENTS + 3)
    )
    output = io.BytesIO()

    exit_code, output_attached = relay.relay_stderr(source, output)

    lines = output.getvalue().splitlines()
    assert exit_code == 0
    assert output_attached is True
    assert lines[0] == relay.READY_LINE.encode("ascii")
    assert lines.count(_diagnostic_event().rstrip(b"\n")) == relay.MAX_EVENTS
    assert relay.EVENTS_TRUNCATED_LINE.encode("ascii") in lines
    assert relay.LONG_LINE_DISCARDED_LINE.encode("ascii") in lines
    assert all(
        b"synthetic-secret" not in line
        and b"private_path" not in line
        and b"token=private" not in line
        for line in lines
    )
    assert source.eof is True


def test_relay_continues_reading_after_runner_output_disconnects() -> None:
    relay = _load_relay()
    assert relay is not None

    class _BrokenOutput:
        def write(self, _data: bytes) -> int:
            raise BrokenPipeError("private output path token=synthetic-secret")

        def flush(self) -> None:
            raise BrokenPipeError("private output path token=synthetic-secret")

    payload = _diagnostic_event() + (b"ordinary private log\n" * 80_000)
    source = _TrackingSource(payload)

    exit_code, output_attached = relay.relay_stderr(source, _BrokenOutput())

    assert exit_code == 0
    assert output_attached is False
    assert source.eof is True
    assert source.read_bytes == len(payload)


def test_relay_read_failure_returns_only_a_fixed_failure_code() -> None:
    relay = _load_relay()
    assert relay is not None

    class _FailingSource:
        def read1(self, _size: int) -> bytes:
            raise OSError("private path token=synthetic-secret")

    output = io.BytesIO()
    exit_code, output_attached = relay.relay_stderr(_FailingSource(), output)

    assert exit_code == relay.RELAY_READ_FAILURE_EXIT_CODE
    assert output_attached is True
    assert output.getvalue() == relay.READY_LINE.encode("ascii") + b"\n"
    assert b"synthetic-secret" not in output.getvalue()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle inheritance test")
def test_windows_pipe_handles_are_inherited_by_relay_and_fake_bridge() -> None:
    relay_module = _load_relay()
    assert relay_module is not None
    script_path = Path(relay_module.__file__).resolve(strict=True)
    runner = importlib.import_module("scripts.phase5e2c2_isolated_smoke")
    relay_environment = runner._stderr_relay_environment()
    assert "CODEX_BRIDGE_CONTROL_TOKEN" not in relay_environment
    assert "CODEX_HOME" not in relay_environment
    read_fd, write_fd = os.pipe()
    read_stream = os.fdopen(read_fd, "rb", buffering=0)
    write_stream = os.fdopen(write_fd, "wb", buffering=0)
    relay_process: subprocess.Popen[bytes] | None = None
    writer_process: subprocess.Popen[bytes] | None = None
    try:
        relay_process = subprocess.Popen(
            [sys.executable, "-I", str(script_path)],
            env=relay_environment,
            stdin=read_stream,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        read_stream.close()
        assert relay_process.stdout is not None
        ready_line = relay_process.stdout.readline()
        assert ready_line == relay_module.READY_LINE.encode("ascii") + b"\n"

        writer_code = (
            "import sys; "
            "sys.stderr.write('private token=synthetic-secret\\n'); "
            "sys.stderr.write('"
            "workflow.startup_diagnostic.persistence_failed "
            "stage=worker_config_write original_exception=RuntimeError "
            "persistence_exception=OSError\\n')"
        )
        writer_process = subprocess.Popen(
            [sys.executable, "-c", writer_code],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=write_stream,
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        write_stream.close()
        relay_output, _ = relay_process.communicate(timeout=10)
        assert writer_process.wait(timeout=10) == 0
        assert relay_process.returncode == 0
        assert (ready_line + relay_output).splitlines() == [
            relay_module.READY_LINE.encode("ascii"),
            _diagnostic_event().rstrip(b"\n"),
        ]
        assert b"synthetic-secret" not in relay_output
    finally:
        read_stream.close()
        write_stream.close()
        for process in (writer_process, relay_process):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)


@pytest.mark.skipif(os.name != "nt", reason="Windows inherited-handle disconnect test")
def test_windows_relay_keeps_draining_after_runner_closes_output_pipe() -> None:
    relay_module = _load_relay()
    assert relay_module is not None
    script_path = Path(relay_module.__file__).resolve(strict=True)
    runner = importlib.import_module("scripts.phase5e2c2_isolated_smoke")
    relay_environment = runner._stderr_relay_environment()
    read_fd, write_fd = os.pipe()
    read_stream = os.fdopen(read_fd, "rb", buffering=0)
    write_stream = os.fdopen(write_fd, "wb", buffering=0)
    relay_process: subprocess.Popen[bytes] | None = None
    writer_process: subprocess.Popen[bytes] | None = None
    try:
        relay_process = subprocess.Popen(
            [sys.executable, "-I", str(script_path)],
            env=relay_environment,
            stdin=read_stream,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        read_stream.close()
        assert relay_process.stdout is not None
        assert relay_process.stdout.readline() == relay_module.READY_LINE.encode("ascii") + b"\n"
        relay_process.stdout.close()

        writer_code = (
            "import sys; "
            "sys.stderr.write('"
            "workflow.startup_diagnostic.persistence_failed "
            "stage=worker_spawn original_exception=RuntimeError "
            "persistence_exception=OSError\\n'); "
            "sys.stderr.write('discard token=synthetic-secret\\n' * 80000)"
        )
        writer_process = subprocess.Popen(
            [sys.executable, "-c", writer_code],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=write_stream,
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        write_stream.close()
        assert writer_process.wait(timeout=15) == 0
        assert relay_process.wait(timeout=15) == 0
    finally:
        read_stream.close()
        write_stream.close()
        for process in (writer_process, relay_process):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def test_relay_start_failure_blocks_before_bridge_process_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = importlib.import_module("scripts.phase5e2c2_isolated_smoke")
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def fail_relay_start(*args: Any, **kwargs: Any) -> None:
        calls.append((args, kwargs))
        raise OSError("private path token=synthetic-secret")

    monkeypatch.setattr(runner.subprocess, "Popen", fail_relay_start)

    with pytest.raises(runner.SmokeAbort) as error:
        runner._start_bridge({})

    assert error.value.code == "isolated_bridge_stderr_relay_could_not_start"
    assert len(calls) == 1
    assert calls[0][1]["stdin"] is not None
    assert calls[0][1]["stdout"] == runner.subprocess.PIPE


def test_bridge_start_waits_for_relay_and_closes_parent_pipe_copies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = importlib.import_module("scripts.phase5e2c2_isolated_smoke")
    relay_module = _load_relay()
    assert relay_module is not None
    calls: list[tuple[list[str], dict[str, Any]]] = []

    class _ReadyPipe:
        def __init__(self) -> None:
            self._ready_sent = False
            self._closed = threading.Event()

        def read1(self, _size: int) -> bytes:
            if not self._ready_sent:
                self._ready_sent = True
                return relay_module.READY_LINE.encode("ascii") + b"\n"
            self._closed.wait(timeout=2)
            return b""

        def close(self) -> None:
            self._closed.set()

    class _FakeProcess:
        def __init__(self, pid: int, stdout: Any = None) -> None:
            self.pid = pid
            self.stdout = stdout
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            if self.stdout is not None:
                self.stdout.close()
            self.returncode = 0
            return 0

        def terminate(self) -> None:
            self.returncode = -15

    relay_process = _FakeProcess(54321, _ReadyPipe())
    bridge_process = _FakeProcess(54322)

    def fake_popen(args: list[str], **kwargs: Any) -> _FakeProcess:
        calls.append((args, kwargs))
        return relay_process if str(relay_module.__file__) in args[-1] else bridge_process

    monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)

    started_bridge, relay = runner._start_bridge({"CODEX_BRIDGE_CONTROL_TOKEN": "secret"})

    assert started_bridge is bridge_process
    assert len(calls) == 2
    assert calls[0][1]["stdin"].closed is True
    assert calls[0][1]["stdout"] == runner.subprocess.PIPE
    assert calls[0][1]["stderr"] == runner.subprocess.DEVNULL
    assert "CODEX_BRIDGE_CONTROL_TOKEN" not in calls[0][1]["env"]
    assert calls[1][1]["stderr"].closed is True
    assert relay.finish_after_bridge(bridge_reaped=True)["status"] == "REAPED"


class _FakeRelayProcess:
    def __init__(self, returncode: int | None) -> None:
        self.pid = 54321
        self.returncode = returncode
        self.terminate_called = False
        self.wait_called = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.wait_called = True
        if self.returncode is None:
            raise subprocess.TimeoutExpired("stderr relay", timeout)
        return self.returncode

    def terminate(self) -> None:
        self.terminate_called = True
        self.returncode = -15


class _FakeCollector:
    def __init__(self, read_status: str = "EOF") -> None:
        self.read_status = read_status

    def wait(self, timeout: float) -> bool:
        return True

    def snapshot(self) -> dict[str, Any]:
        return {"read_status": self.read_status}


def test_relay_lifecycle_preserves_helper_for_uncertain_bridge() -> None:
    runner = importlib.import_module("scripts.phase5e2c2_isolated_smoke")
    process = _FakeRelayProcess(returncode=None)
    relay = runner._BridgeStderrRelay(process, _FakeCollector("READING"))

    record = relay.finish_after_bridge(bridge_reaped=False)

    assert record["status"] == "PRESERVED_WITH_BRIDGE_OR_UNKNOWN"
    assert record["failure_reason"] is None
    assert process.wait_called is False
    assert process.terminate_called is False


def test_relay_lifecycle_reaps_after_bridge_shutdown_and_records_abnormal_exit() -> None:
    runner = importlib.import_module("scripts.phase5e2c2_isolated_smoke")
    normal_process = _FakeRelayProcess(returncode=0)
    normal_relay = runner._BridgeStderrRelay(normal_process, _FakeCollector())
    normal_record = normal_relay.finish_after_bridge(bridge_reaped=True)
    assert normal_record["status"] == "REAPED"
    assert normal_process.wait_called is True
    assert normal_process.terminate_called is False

    failed_process = _FakeRelayProcess(returncode=7)
    failed_relay = runner._BridgeStderrRelay(failed_process, _FakeCollector())
    failed_record = failed_relay.finish_after_bridge(bridge_reaped=False)
    assert failed_record["status"] == "EXITED_UNEXPECTEDLY"
    assert failed_record["failure_reason"] == "stderr_relay_exited_while_bridge_active"
    assert failed_process.terminate_called is False


def test_relay_lifecycle_detects_residual_after_bridge_exit() -> None:
    runner = importlib.import_module("scripts.phase5e2c2_isolated_smoke")
    process = _FakeRelayProcess(returncode=None)
    relay = runner._BridgeStderrRelay(process, _FakeCollector())

    record = relay.finish_after_bridge(bridge_reaped=True)

    assert record["status"] == "TERMINATED_AFTER_BRIDGE_EXIT"
    assert record["failure_reason"] == "stderr_relay_eof_timeout"
    assert process.terminate_called is True
    assert record["exit_code"] == -15


def test_reconciliation_cleanup_decision_preserves_bridge_and_relay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = importlib.import_module("scripts.phase5e2c2_isolated_smoke")
    monkeypatch.setattr(runner, "_worker_process_count", lambda _pid: 0)
    monkeypatch.setattr(
        runner,
        "_read_request_ledger",
        lambda _root: [
            {
                "client_request_id": "phase5e2c2-test",
                "state": "needs_reconciliation",
                "run_id": "run-0123456789abcdef0123456789abcdef",
            }
        ],
    )
    bridge = type("FakeBridge", (), {"pid": 54320})()
    relay_process = _FakeRelayProcess(returncode=None)
    relay = runner._BridgeStderrRelay(relay_process, _FakeCollector("READING"))

    assert (
        runner._can_safely_stop_after_post(
            bridge,
            Path("unused-state-root"),
            "phase5e2c2-test",
        )
        is False
    )
    record = relay.finish_after_bridge(bridge_reaped=False)
    assert record["status"] == "PRESERVED_WITH_BRIDGE_OR_UNKNOWN"
    assert relay_process.terminate_called is False
