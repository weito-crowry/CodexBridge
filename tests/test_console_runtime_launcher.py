from __future__ import annotations

import sys

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

    def setProgram(self, program: str) -> None:
        self.program = program

    def setArguments(self, arguments: list[str]) -> None:
        self.arguments = arguments

    def setProcessEnvironment(self, environment: FakeEnvironment) -> None:
        self.environment = environment

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

    result = launcher.launch(codex_executable="C:/Codex/codex.exe", ui_port=8456)

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
    }


def test_launcher_accepts_qprocess_bool_detached_result_without_pid() -> None:
    _application()
    process = FakeProcess(True)
    launcher = BridgeRuntimeLauncher(
        process_factory=lambda: process,
        environment_factory=lambda: FakeEnvironment({}),
    )

    result = launcher.launch(codex_executable="codex", ui_port=8001)

    assert result.started
    assert result.pid is None


def test_launcher_close_does_not_terminate_detached_process() -> None:
    _application()
    process = FakeProcess()
    launcher = BridgeRuntimeLauncher(
        process_factory=lambda: process,
        environment_factory=lambda: FakeEnvironment({}),
    )
    launcher.launch(codex_executable="codex", ui_port=8001)

    launcher.close()

    assert process.detached_calls == 1
    assert not process.deleted
