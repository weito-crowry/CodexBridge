from __future__ import annotations

from typing import Any

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from codex_bridge.console.codex_resolver import CodexResolution
from codex_bridge.console.codex_updates import CodexUpdateProbe, parse_codex_update_info


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
        self.finished = Signal()
        self.errorOccurred = Signal()
        self.readyReadStandardOutput = Signal()
        self.readyReadStandardError = Signal()
        self.program: str | None = None
        self.arguments: list[str] = []
        self.native_arguments: str | None = None
        self.stdout = b""
        self.stderr = b""
        self.exit_code = 0
        self.started = False
        self.killed = False

    def setProgram(self, program: str) -> None:
        self.program = program

    def setArguments(self, arguments: list[str]) -> None:
        self.arguments = arguments

    def setNativeArguments(self, arguments: str) -> None:
        self.native_arguments = arguments

    def start(self) -> None:
        self.started = True

    def readAllStandardOutput(self) -> bytes:
        output, self.stdout = self.stdout, b""
        return output

    def readAllStandardError(self) -> bytes:
        output, self.stderr = self.stderr, b""
        return output

    def exitCode(self) -> int:
        return self.exit_code

    def kill(self) -> None:
        self.killed = True

    def deleteLater(self) -> None:
        pass


def _application() -> QApplication:
    application = QCoreApplication.instance()
    return application if isinstance(application, QApplication) else QApplication([])


def _doctor_payload(*, status: str = "ok", latest: str = "0.154.0") -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "codexVersion": "0.153.4",
        "checks": {
            "updates.status": {
                "status": status,
                "details": {
                    "cached latest version": latest,
                    "latest version status": "available",
                    "update action": "codex update",
                    "last checked at": "2030-01-02T03:04:05Z",
                },
            }
        },
    }


def test_doctor_json_extracts_update_fields_and_uses_resolved_version() -> None:
    info = parse_codex_update_info(_doctor_payload(), current_version="0.153.4")

    assert info.current_version == "0.153.4"
    assert info.doctor_version == "0.153.4"
    assert info.latest_version == "0.154.0"
    assert info.latest_status == "ok"
    assert info.latest_version_status == "available"
    assert info.update_action == "codex update"
    assert info.last_checked_at == "2030-01-02T03:04:05Z"
    assert info.update_available
    assert info.can_update


def test_warning_doctor_update_check_does_not_claim_cached_update_available() -> None:
    info = parse_codex_update_info(
        _doctor_payload(status="warning", latest="0.150.1"), current_version="0.153.4"
    )

    assert info.display_status == "Unavailable"
    assert not info.update_available
    assert not info.can_update


def test_update_check_runs_doctor_json_asynchronously_and_rejects_duplicates() -> None:
    _application()
    process = FakeProcess()
    probe = CodexUpdateProbe(
        platform="win32",
        process_factory=lambda _parent: process,
    )
    resolution = CodexResolution("C:/Codex/codex.exe", "0.153.4", "codex_app")
    results: list[object] = []
    probe.check_succeeded.connect(results.append)

    assert probe.check_for_updates(resolution)
    assert not probe.check_for_updates(resolution)
    assert process.started
    assert process.program == resolution.path
    assert process.arguments == ["doctor", "--json"]

    process.stdout = b'{"checks": {"updates.status": {"status": "ok", "details": {}}}}'
    process.readyReadStandardOutput.emit()
    process.finished.emit(0, 0)

    assert len(results) == 1
    assert not probe.busy


def test_update_check_accepts_valid_updates_json_when_doctor_exit_code_is_nonzero() -> None:
    _application()
    process = FakeProcess()
    probe = CodexUpdateProbe(
        platform="win32",
        process_factory=lambda _parent: process,
    )
    results: list[object] = []
    failures: list[str] = []
    probe.check_succeeded.connect(results.append)
    probe.check_failed.connect(failures.append)

    assert probe.check_for_updates(CodexResolution("codex.exe", "1.2.3", "path"))
    process.stdout = (
        b'{"overallStatus":"fail","checks":{"updates.status":{"status":"ok","details":{}}}}'
    )
    process.readyReadStandardOutput.emit()
    process.exit_code = 1
    process.finished.emit(1, 0)

    assert len(results) == 1
    assert failures == []


def test_update_check_rejects_invalid_or_missing_updates_status_json() -> None:
    _application()
    process = FakeProcess()
    probe = CodexUpdateProbe(
        platform="win32",
        process_factory=lambda _parent: process,
    )
    failures: list[str] = []
    probe.check_failed.connect(failures.append)

    assert probe.check_for_updates(CodexResolution("codex.exe", "1.2.3", "path"))
    process.stdout = b'{"checks": {}}'
    process.readyReadStandardOutput.emit()
    process.finished.emit(0, 0)

    assert failures == ["Codex update check failed"]


def test_update_uses_resolved_cmd_path_and_reports_failure_without_success() -> None:
    _application()
    process = FakeProcess()
    probe = CodexUpdateProbe(
        platform="win32",
        environ={"COMSPEC": "C:/Windows/System32/cmd.exe"},
        which=lambda _name: "C:/Windows/System32/cmd.exe",
        process_factory=lambda _parent: process,
    )
    resolution = CodexResolution(r"C:\Program Files\Codex\codex.cmd", "0.153.4", "npm")
    failures: list[str] = []
    successes: list[bool] = []
    probe.update_failed.connect(failures.append)
    probe.update_succeeded.connect(lambda: successes.append(True))

    assert probe.update(resolution)
    assert process.program == "C:/Windows/System32/cmd.exe"
    assert process.native_arguments == (f'/d /s /c ""{resolution.path}" update"')
    process.exit_code = 1
    process.finished.emit(1, 0)

    assert failures == ["Codex update failed"]
    assert successes == []
    assert not probe.busy
