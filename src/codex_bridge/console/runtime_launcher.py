from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QProcess, QProcessEnvironment


@dataclass(frozen=True, slots=True)
class DetachedLaunchResult:
    started: bool
    pid: int | None


ProcessFactory = Callable[[], Any]
EnvironmentFactory = Callable[[], Any]


class BridgeRuntimeLauncher:
    """Start a Bridge detached from the Console without owning its lifetime."""

    def __init__(
        self,
        *,
        process_factory: ProcessFactory | None = None,
        environment_factory: EnvironmentFactory | None = None,
    ) -> None:
        self._process_factory = process_factory or QProcess
        self._environment_factory = environment_factory or QProcessEnvironment.systemEnvironment
        self._process: Any | None = None

    def launch(
        self,
        *,
        codex_executable: str,
        ui_port: int,
        control_token: str,
    ) -> DetachedLaunchResult:
        process = self._process_factory()
        environment = self._environment_factory()
        environment.insert("CODEX_BRIDGE_CODEX_EXECUTABLE", codex_executable)
        environment.insert("CODEX_BRIDGE_UI_PORT", str(ui_port))
        environment.insert("CODEX_BRIDGE_CONTROL_TOKEN", control_token)
        process.setProgram(sys.executable)
        process.setArguments(["-m", "codex_bridge"])
        process.setProcessEnvironment(environment)
        self._process = process
        outcome = process.startDetached()
        if isinstance(outcome, tuple):
            started = bool(outcome[0])
            pid = outcome[1] if len(outcome) > 1 else None
            return DetachedLaunchResult(started, int(pid) if started and pid else None)
        return DetachedLaunchResult(bool(outcome), None)

    def close(self) -> None:
        """Release Console references without sending any child-process signal."""

        self._process = None
