from __future__ import annotations

import sys

from PySide6.QtCore import QIODevice
from PySide6.QtWidgets import QApplication

from codex_bridge.console.runtime_launcher import BridgeRuntimeLauncher


class FakeEnvironment:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def insert(self, key: str, value: str) -> None:
        self.values[key] = value


class FakeProcess:
    def __init__(self, outcome: object = (True, 4321)) -> None:
        self.program: str | None = None
        self.arguments: list[str] = []
        self.environment: FakeEnvironment | None = None
        self.outcome = outcome
        self.detached_calls = 0
        self.deleted = False
        self.stdout_file: tuple[str, object] | None = None
        self.stderr_file: tuple[str, object] | None = None
        self.fail_redirect = False

    def setProgram(self, program: str) -> None:
        self.program = program

    def setArguments(self, arguments: list[str]) -> None:
        self.arguments = arguments

    def setProcessEnvironment(self, environment: FakeEnvironment) -> None:
        self.environment = environment

    def setStandardOutputFile(self, path: str, mode: object) -> None:
        if self.fail_redirect:
            raise OSError("stdout redirect unavailable")
        self.stdout_file = (path, mode)

    def setStandardErrorFile(self, path: str, mode: object) -> None:
        if self.fail_redirect:
            raise OSError("stderr redirect unavailable")
        self.stderr_file = (path, mode)

    def startDetached(self) -> object:
        self.detached_calls += 1
        return self.outcome

    def deleteLater(self) -> None:
        self.deleted = True


def _application() -> QApplication:
    application = QApplication.instance()
    return application if isinstance(application, QApplication) else QApplication([])


def test_launcher_uses_python_module_and_only_authorized_environment_overrides(monkeypatch) -> None:
    _application()
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    process = FakeProcess()
    environments: list[FakeEnvironment] = []

    def environment_factory() -> FakeEnvironment:
        environment = FakeEnvironment(
            {
                "CODEX_BRIDGE_ALLOWED_ROOTS": "C:/allowed",
                "CODEX_BRIDGE_PORT": "8123",
                "CODEX_BRIDGE_UI_PORT": "old",
            }
        )
        environments.append(environment)
        return environment

    launcher = BridgeRuntimeLauncher(
        process_factory=lambda: process,
        environment_factory=environment_factory,
    )

    token = "A" * 32
    result = launcher.launch(
        codex_executable="C:/Codex/codex.exe",
        ui_port=8456,
        control_token=token,
    )

    assert result.started
    assert result.pid == 4321
    assert process.program == sys.executable
    assert process.arguments == ["-m", "codex_bridge"]
    assert process.detached_calls == 1
    assert process.environment is environments[0]
    assert process.environment.values == {
        "CODEX_BRIDGE_ALLOWED_ROOTS": "C:/allowed",
        "CODEX_BRIDGE_PORT": "8123",
        "CODEX_BRIDGE_UI_PORT": "8456",
        "CODEX_BRIDGE_CODEX_EXECUTABLE": "C:/Codex/codex.exe",
        "CODEX_BRIDGE_CONTROL_TOKEN": token,
    }
    assert token not in process.arguments
    assert token not in str(process.stdout_file)
    assert token not in str(process.stderr_file)
    assert "PYINSTALLER_RESET_ENVIRONMENT" not in process.environment.values


def test_frozen_launcher_uses_internal_runtime_mode_and_reset_environment(monkeypatch) -> None:
    _application()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    process = FakeProcess()
    environments: list[FakeEnvironment] = []

    def environment_factory() -> FakeEnvironment:
        environment = FakeEnvironment(
            {
                "CODEX_BRIDGE_PORT": "8123",
                "CODEX_BRIDGE_GITHUB_PAT": "inherited-but-not-logged",
            }
        )
        environments.append(environment)
        return environment

    launcher = BridgeRuntimeLauncher(
        process_factory=lambda: process,
        environment_factory=environment_factory,
    )
    token = "B" * 32

    result = launcher.launch(
        codex_executable="C:/Codex/codex.exe",
        ui_port=8456,
        control_token=token,
    )

    assert result.started
    assert process.program == sys.executable
    assert process.arguments == ["--codexbridge-runtime"]
    assert process.environment is environments[0]
    assert process.environment.values["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert process.environment.values["CODEX_BRIDGE_PORT"] == "8123"
    assert process.environment.values["CODEX_BRIDGE_GITHUB_PAT"] == "inherited-but-not-logged"
    assert process.environment.values["CODEX_BRIDGE_CONTROL_TOKEN"] == token
    assert token not in process.arguments
    assert process.detached_calls == 1


def test_launcher_accepts_qprocess_bool_detached_result_without_pid() -> None:
    _application()
    process = FakeProcess(True)
    launcher = BridgeRuntimeLauncher(
        process_factory=lambda: process,
        environment_factory=lambda: FakeEnvironment({}),
    )

    result = launcher.launch(codex_executable="codex", ui_port=8001, control_token="A" * 32)

    assert result.started
    assert result.pid is None


def test_launcher_propagates_explicit_allowed_roots_to_child() -> None:
    _application()
    process = FakeProcess()
    launcher = BridgeRuntimeLauncher(
        process_factory=lambda: process,
        environment_factory=lambda: FakeEnvironment({}),
    )

    result = launcher.launch(
        codex_executable="codex.exe",
        ui_port=8001,
        control_token="A" * 32,
        allowed_roots=(r"C:\repo", r"D:\work"),
    )

    assert result.started
    assert process.environment is not None
    assert process.environment.values["CODEX_BRIDGE_ALLOWED_ROOTS"] == (
        r"C:\repo" + __import__("os").pathsep + r"D:\work"
    )


def test_launcher_close_does_not_terminate_detached_process() -> None:
    _application()
    process = FakeProcess()
    launcher = BridgeRuntimeLauncher(
        process_factory=lambda: process,
        environment_factory=lambda: FakeEnvironment({}),
    )
    launcher.launch(codex_executable="codex", ui_port=8001, control_token="A" * 32)

    launcher.close()

    assert process.detached_calls == 1
    assert not process.deleted


def test_launcher_redirects_stdout_and_stderr_to_append_files(tmp_path, monkeypatch) -> None:
    from codex_bridge.console import runtime_launcher as launcher_module

    stdout_path = tmp_path / "bridge-runtime-stdout.log"
    stderr_path = tmp_path / "bridge-runtime-stderr.log"
    stdout_path.write_text("existing stdout\n", encoding="utf-8")
    stderr_path.write_text("existing stderr\n", encoding="utf-8")
    monkeypatch.setattr(launcher_module, "bridge_runtime_stdout_log_path", lambda: stdout_path)
    monkeypatch.setattr(launcher_module, "bridge_runtime_stderr_log_path", lambda: stderr_path)
    process = FakeProcess()
    launcher = BridgeRuntimeLauncher(
        process_factory=lambda: process,
        environment_factory=lambda: FakeEnvironment({}),
    )

    result = launcher.launch(codex_executable="codex", ui_port=8001, control_token="A" * 32)

    assert result.started
    assert process.stdout_file == (str(stdout_path), QIODevice.OpenModeFlag.Append)
    assert process.stderr_file == (str(stderr_path), QIODevice.OpenModeFlag.Append)
    assert stdout_path.parent.is_dir()
    assert stdout_path.read_text(encoding="utf-8") == "existing stdout\n"
    assert stderr_path.read_text(encoding="utf-8") == "existing stderr\n"


def test_launcher_rotates_oversized_runtime_logs_to_one_backup(tmp_path, monkeypatch) -> None:
    from codex_bridge.console import runtime_launcher as launcher_module

    stdout_path = tmp_path / "bridge-runtime-stdout.log"
    stderr_path = tmp_path / "bridge-runtime-stderr.log"
    oversized = b"x" * (2 * 1024 * 1024 + 1)
    stdout_path.write_bytes(oversized)
    stdout_path.with_name(f"{stdout_path.name}.1").write_text("old backup", encoding="utf-8")
    monkeypatch.setattr(launcher_module, "bridge_runtime_stdout_log_path", lambda: stdout_path)
    monkeypatch.setattr(launcher_module, "bridge_runtime_stderr_log_path", lambda: stderr_path)
    process = FakeProcess()
    launcher = BridgeRuntimeLauncher(
        process_factory=lambda: process,
        environment_factory=lambda: FakeEnvironment({}),
    )

    result = launcher.launch(codex_executable="codex", ui_port=8001, control_token="A" * 32)

    assert result.started
    assert not stdout_path.exists()
    assert stdout_path.with_name(f"{stdout_path.name}.1").read_bytes() == oversized
    assert len(list(tmp_path.glob("bridge-runtime-stdout.log.*"))) == 1


def test_launcher_redirect_preparation_failure_does_not_block_detached_launch(
    tmp_path, monkeypatch
) -> None:
    from codex_bridge.console import runtime_launcher as launcher_module

    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(
        launcher_module,
        "bridge_runtime_stdout_log_path",
        lambda: blocker / "bridge-runtime-stdout.log",
    )
    monkeypatch.setattr(
        launcher_module,
        "bridge_runtime_stderr_log_path",
        lambda: blocker / "bridge-runtime-stderr.log",
    )
    process = FakeProcess()
    launcher = BridgeRuntimeLauncher(
        process_factory=lambda: process,
        environment_factory=lambda: FakeEnvironment({}),
    )

    result = launcher.launch(codex_executable="codex", ui_port=8001, control_token="A" * 32)

    assert result.started
    assert process.detached_calls == 1
    assert process.arguments == ["-m", "codex_bridge"]


def test_launcher_redirect_setter_failure_does_not_block_detached_launch(
    tmp_path, monkeypatch
) -> None:
    from codex_bridge.console import runtime_launcher as launcher_module

    monkeypatch.setattr(
        launcher_module,
        "bridge_runtime_stdout_log_path",
        lambda: tmp_path / "bridge-runtime-stdout.log",
    )
    monkeypatch.setattr(
        launcher_module,
        "bridge_runtime_stderr_log_path",
        lambda: tmp_path / "bridge-runtime-stderr.log",
    )
    process = FakeProcess()
    process.fail_redirect = True
    launcher = BridgeRuntimeLauncher(
        process_factory=lambda: process,
        environment_factory=lambda: FakeEnvironment({}),
    )

    result = launcher.launch(codex_executable="codex", ui_port=8001, control_token="A" * 32)

    assert result.started
    assert process.detached_calls == 1
