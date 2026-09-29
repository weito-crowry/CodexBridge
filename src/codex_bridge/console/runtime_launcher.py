from __future__ import annotations

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QIODevice, QProcess, QProcessEnvironment

from ..observability import bridge_runtime_stderr_log_path, bridge_runtime_stdout_log_path
from ..runtime_mode import INTERNAL_RUNTIME_FLAG


@dataclass(frozen=True, slots=True)
class DetachedLaunchResult:
    started: bool
    pid: int | None


ProcessFactory = Callable[[], Any]
EnvironmentFactory = Callable[[], Any]
_RUNTIME_LOG_MAX_BYTES = 2 * 1024 * 1024


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
        allowed_roots: tuple[str, ...] = (),
    ) -> DetachedLaunchResult:
        process = self._process_factory()
        environment = self._environment_factory()
        environment.insert("CODEX_BRIDGE_CODEX_EXECUTABLE", codex_executable)
        environment.insert("CODEX_BRIDGE_UI_PORT", str(ui_port))
        environment.insert("CODEX_BRIDGE_CONTROL_TOKEN", control_token)
        if allowed_roots:
            environment.insert("CODEX_BRIDGE_ALLOWED_ROOTS", os.pathsep.join(allowed_roots))
        frozen = bool(getattr(sys, "frozen", False))
        if frozen:
            environment.insert("PYINSTALLER_RESET_ENVIRONMENT", "1")
        process.setProgram(sys.executable)
        process.setArguments([INTERNAL_RUNTIME_FLAG] if frozen else ["-m", "codex_bridge"])
        process.setProcessEnvironment(environment)
        self._configure_output_redirects(process)
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

    @staticmethod
    def _configure_output_redirects(process: Any) -> None:
        for path_factory, setter_name in (
            (bridge_runtime_stdout_log_path, "setStandardOutputFile"),
            (bridge_runtime_stderr_log_path, "setStandardErrorFile"),
        ):
            try:
                path = path_factory()
                path.parent.mkdir(parents=True, exist_ok=True)
                BridgeRuntimeLauncher._rotate_runtime_log(path)
                setter = getattr(process, setter_name)
                setter(os.fspath(path), QIODevice.OpenModeFlag.Append)
            except Exception:
                # Diagnostics setup must never prevent the detached Bridge launch.
                continue

    @staticmethod
    def _rotate_runtime_log(path: Path) -> None:
        try:
            if path.stat().st_size <= _RUNTIME_LOG_MAX_BYTES:
                return
            backup = path.with_name(f"{path.name}.1")
            try:
                backup.unlink(missing_ok=True)
            except OSError:
                return
            path.replace(backup)
        except OSError:
            return
