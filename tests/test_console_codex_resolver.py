from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtWidgets import QApplication

from codex_bridge.console.codex_resolver import (
    CodexCandidate,
    CodexResolutionError,
    CodexVersionProbe,
    enumerate_candidates,
    parse_codex_version,
)


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
        self.killed = False
        self.started = False

    def setProgram(self, program: str) -> None:
        self.program = program

    def setArguments(self, arguments: list[str]) -> None:
        self.arguments = arguments

    def setNativeArguments(self, arguments: str) -> None:
        self.native_arguments = arguments

    def setProcessChannelMode(self, mode: object) -> None:
        del mode

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


def _application() -> QApplication:
    application = QApplication.instance()
    return application if isinstance(application, QApplication) else QApplication([])


def test_candidates_prefer_newest_codex_app_then_path_then_npm(tmp_path: Path) -> None:
    local = tmp_path / "local"
    app_bin = local / "OpenAI" / "Codex" / "bin"
    older = app_bin / "a" / "codex.exe"
    newer = app_bin / "b" / "codex.exe"
    older.parent.mkdir(parents=True)
    newer.parent.mkdir(parents=True)
    older.write_bytes(b"")
    newer.write_bytes(b"")
    os.utime(older, (1, 1))
    os.utime(newer, (2, 2))

    npm = tmp_path / "app" / "npm" / "codex.cmd"
    npm.parent.mkdir(parents=True)
    npm.write_bytes(b"")
    path_candidate = tmp_path / "path" / "codex.exe"
    path_candidate.parent.mkdir()
    path_candidate.write_bytes(b"")

    def fake_which(name: str) -> str | None:
        return str(path_candidate) if name == "codex.exe" else None

    candidates = enumerate_candidates(
        {
            "LOCALAPPDATA": str(local),
            "APPDATA": str(tmp_path / "app"),
        },
        platform="win32",
        which=fake_which,
    )

    assert [(candidate.source, Path(candidate.path).name) for candidate in candidates] == [
        ("codex_app", "codex.exe"),
        ("codex_app", "codex.exe"),
        ("path", "codex.exe"),
        ("npm", "codex.cmd"),
    ]
    assert candidates[0].path == str(newer)
    assert candidates[1].path == str(older)


def test_explicit_override_is_fail_closed_without_fallback(tmp_path: Path) -> None:
    explicit = tmp_path / "configured" / "codex.exe"
    explicit.parent.mkdir()
    explicit.write_bytes(b"")

    candidates = enumerate_candidates(
        {"CODEX_BRIDGE_CODEX_EXECUTABLE": str(explicit)},
        platform="win32",
        which=lambda _: None,
    )

    assert candidates == (CodexCandidate(str(explicit), "explicit"),)

    with pytest.raises(CodexResolutionError):
        enumerate_candidates(
            {
                "CODEX_BRIDGE_CODEX_EXECUTABLE": str(tmp_path / "missing.exe"),
                "LOCALAPPDATA": str(tmp_path),
            },
            platform="win32",
            which=lambda _: str(tmp_path / "other.exe"),
        )


def test_windows_candidates_are_deduplicated_case_insensitively(tmp_path: Path) -> None:
    native = tmp_path / "OpenAI" / "Codex" / "bin" / "a" / "codex.exe"
    native.parent.mkdir(parents=True)
    native.write_bytes(b"")

    candidates = enumerate_candidates(
        {"LOCALAPPDATA": str(tmp_path)},
        platform="win32",
        which=lambda name: str(native).upper() if name == "codex.exe" else None,
    )

    assert candidates == (CodexCandidate(str(native), "codex_app"),)


def test_ps1_is_never_a_path_candidate(tmp_path: Path) -> None:
    ps1 = tmp_path / "codex.ps1"
    ps1.write_bytes(b"")

    candidates = enumerate_candidates(
        {},
        platform="win32",
        which=lambda _: str(ps1),
    )

    assert candidates == ()


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (b"codex-cli 0.150.0-alpha.8\n", "0.150.0-alpha.8"),
        (b"codex-cli 1.2.3", "1.2.3"),
    ],
)
def test_parse_codex_version_accepts_only_bounded_codex_cli_output(
    output: bytes, expected: str
) -> None:
    assert parse_codex_version(output) == expected


@pytest.mark.parametrize("output", [b"", b"codex 1.2.3\n", b"codex-cli\n", b"codex-cli 1 2\n"])
def test_parse_codex_version_rejects_invalid_output(output: bytes) -> None:
    with pytest.raises(ValueError):
        parse_codex_version(output)


def test_version_probe_is_async_and_returns_bounded_valid_result() -> None:
    _application()
    process = FakeProcess()
    probe = CodexVersionProbe(
        [CodexCandidate("C:/Codex/codex.exe", "path")],
        platform="win32",
        process_factory=lambda _parent: process,
    )
    resolved: list[object] = []
    failures: list[str] = []
    probe.resolved.connect(resolved.append)
    probe.failed.connect(failures.append)

    assert probe.start()
    assert process.started
    assert process.program == "C:/Codex/codex.exe"
    assert process.arguments == ["--version"]
    process.stdout = b"codex-cli 0.150.0-alpha.8\n"
    process.readyReadStandardOutput.emit()
    process.finished.emit(0, 0)

    assert len(resolved) == 1
    assert resolved[0].version == "0.150.0-alpha.8"
    assert resolved[0].source == "path"
    assert failures == []


def test_version_probe_uses_native_cmd_qprocess_for_cmd_candidate(tmp_path: Path) -> None:
    _application()
    process = FakeProcess()
    cmd_exe = tmp_path / "cmd.exe"
    cmd_exe.write_bytes(b"")
    candidate = r"C:\Program Files\Codex\codex.cmd"
    probe = CodexVersionProbe(
        [CodexCandidate(candidate, "npm")],
        platform="win32",
        environ={"COMSPEC": str(cmd_exe)},
        process_factory=lambda _parent: process,
    )

    probe.start()

    assert process.program == str(cmd_exe)
    assert process.arguments == []
    assert process.native_arguments == f'/d /s /c ""{candidate}" --version"'


def test_version_probe_keeps_exe_candidate_on_direct_qprocess() -> None:
    _application()
    process = FakeProcess()
    probe = CodexVersionProbe(
        [CodexCandidate(r"C:\Program Files\Codex\codex.exe", "path")],
        platform="win32",
        environ={"COMSPEC": "unused"},
        process_factory=lambda _parent: process,
    )

    probe.start()

    assert process.program == r"C:\Program Files\Codex\codex.exe"
    assert process.arguments == ["--version"]
    assert process.native_arguments is None


def test_explicit_cmd_probe_failure_does_not_fallback(tmp_path: Path) -> None:
    _application()
    processes = [FakeProcess(), FakeProcess()]
    cmd_exe = tmp_path / "cmd.exe"
    cmd_exe.write_bytes(b"")

    def process_factory(_parent: object) -> FakeProcess:
        return processes.pop(0)

    probe = CodexVersionProbe(
        [
            CodexCandidate(r"C:\Program Files\Codex\codex.cmd", "explicit"),
            CodexCandidate(r"C:\Program Files\Codex\codex.exe", "path"),
        ],
        platform="win32",
        environ={"COMSPEC": str(cmd_exe)},
        process_factory=process_factory,
    )
    failures: list[str] = []
    probe.failed.connect(failures.append)

    explicit_process = processes[0]
    probe.start()
    processes_started = len(processes)
    explicit_process.exit_code = 1
    explicit_process.finished.emit(1, 0)

    assert processes_started == 1
    assert processes[0].started is False
    assert failures == ["Codex version could not be verified"]


def test_non_explicit_cmd_probe_failure_falls_back_to_next_candidate(tmp_path: Path) -> None:
    _application()
    cmd_process = FakeProcess()
    exe_process = FakeProcess()
    processes = [cmd_process, exe_process]
    cmd_exe = tmp_path / "cmd.exe"
    cmd_exe.write_bytes(b"")

    def process_factory(_parent: object) -> FakeProcess:
        return processes.pop(0)

    probe = CodexVersionProbe(
        [
            CodexCandidate(r"C:\Program Files\Codex\codex.cmd", "npm"),
            CodexCandidate(r"C:\Program Files\Codex\codex.exe", "path"),
        ],
        platform="win32",
        environ={"COMSPEC": str(cmd_exe)},
        process_factory=process_factory,
    )

    probe.start()
    cmd_process.exit_code = 1
    cmd_process.finished.emit(1, 0)

    assert exe_process.program == r"C:\Program Files\Codex\codex.exe"
    assert exe_process.arguments == ["--version"]
    assert exe_process.native_arguments is None


def test_cmd_probe_uses_which_fallback_when_comspec_is_unusable(tmp_path: Path) -> None:
    _application()
    process = FakeProcess()
    fallback = tmp_path / "fallback-cmd.exe"
    fallback.write_bytes(b"")
    candidate = r"C:\Program Files\Codex\codex.cmd"

    probe = CodexVersionProbe(
        [CodexCandidate(candidate, "npm")],
        platform="win32",
        environ={"COMSPEC": str(tmp_path / "missing-cmd.exe")},
        which=lambda name: str(fallback) if name == "cmd.exe" else None,
        process_factory=lambda _parent: process,
    )

    probe.start()

    assert process.program == str(fallback)
    assert process.native_arguments == f'/d /s /c ""{candidate}" --version"'


def test_unavailable_cmd_probe_falls_back_to_next_non_explicit_candidate(tmp_path: Path) -> None:
    _application()
    process = FakeProcess()
    candidate = r"C:\Program Files\Codex\codex.cmd"
    fallback_candidate = r"C:\Program Files\Codex\codex.exe"
    probe = CodexVersionProbe(
        [
            CodexCandidate(candidate, "npm"),
            CodexCandidate(fallback_candidate, "path"),
        ],
        platform="win32",
        environ={"COMSPEC": str(tmp_path / "missing-cmd.exe")},
        which=lambda _name: None,
        process_factory=lambda _parent: process,
    )

    probe.start()

    assert process.program == fallback_candidate
    assert process.arguments == ["--version"]
    assert process.native_arguments is None


@pytest.mark.parametrize(
    "configure",
    [
        lambda process: setattr(process, "exit_code", 1),
        lambda process: setattr(process, "stdout", b"codex 1.2.3"),
        lambda process: setattr(process, "stdout", b"x" * 4_097),
    ],
)
def test_version_probe_rejects_nonzero_invalid_and_oversized_output(configure) -> None:
    _application()
    process = FakeProcess()
    configure(process)
    probe = CodexVersionProbe(
        [CodexCandidate("codex", "path")],
        platform="win32",
        process_factory=lambda _parent: process,
    )
    failures: list[str] = []
    probe.failed.connect(failures.append)

    assert probe.start()
    process.readyReadStandardOutput.emit()
    process.finished.emit(process.exit_code, 0)

    assert len(failures) == 1
    assert failures[0] == "Codex version could not be verified"


def test_version_probe_timeout_aborts_only_probe_process() -> None:
    _application()
    process = FakeProcess()
    probe = CodexVersionProbe(
        [CodexCandidate("codex", "path")],
        platform="win32",
        process_factory=lambda _parent: process,
    )
    failures: list[str] = []
    probe.failed.connect(failures.append)

    probe.start()
    probe._on_timeout()

    assert process.killed
    assert failures == ["Codex version could not be verified"]
