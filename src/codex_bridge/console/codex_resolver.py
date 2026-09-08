from __future__ import annotations

import os
import re
import shutil
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

from ..codex_resolver import (
    CodexCandidate,
    CodexResolution,
    CodexResolutionError,
    _is_windows,
    resolve_cmd_executable,
)
from ..codex_resolver import (
    enumerate_candidates as _enumerate_candidates,
)

_MAX_PROBE_OUTPUT = 4 * 1024
_VERSION_PATTERN = re.compile(r"^codex-cli\s+(\S+)$")

__all__ = [
    "CodexCandidate",
    "CodexResolution",
    "CodexResolutionError",
    "CodexVersionProbe",
    "enumerate_candidates",
    "parse_codex_version",
]


def enumerate_candidates(
    environ: Mapping[str, str] | None = None,
    *,
    config_executable: str | None = None,
    platform: str | None = None,
    which: Callable[[str], str | None] | None = None,
) -> tuple[CodexCandidate, ...]:
    return _enumerate_candidates(
        environ,
        config_executable=config_executable,
        platform=platform,
        which=which or shutil.which,
    )


def parse_codex_version(output: bytes) -> str:
    """Parse one bounded `codex-cli <version>` line without exposing raw output."""

    if len(output) > _MAX_PROBE_OUTPUT:
        raise ValueError("Codex version output is too large")
    try:
        text = output.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("Codex version output is invalid") from exc
    match = _VERSION_PATTERN.fullmatch(text)
    if match is None:
        raise ValueError("Codex version output is invalid")
    return match.group(1)


ProcessFactory = Callable[[QObject], Any]


class CodexVersionProbe(QObject):
    """Asynchronously validate candidates using a bounded Qt process probe."""

    resolved = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        candidates: Sequence[CodexCandidate],
        *,
        platform: str | None = None,
        environ: Mapping[str, str] | None = None,
        which: Callable[[str], str | None] = shutil.which,
        process_factory: ProcessFactory | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._candidates = tuple(candidates)
        self._windows = _is_windows(platform)
        self._environ = os.environ if environ is None else environ
        self._which = which
        self._process_factory = process_factory or (lambda owner: QProcess(owner))
        self._process: Any | None = None
        self._candidate_index = 0
        self._stdout = bytearray()
        self._output_size = 0
        self._started = False
        self._finished = False
        self._timeout_timer = QTimer(self)
        self._timeout_timer.setSingleShot(True)
        self._timeout_timer.setInterval(3_000)
        self._timeout_timer.timeout.connect(self._on_timeout)

    def start(self) -> bool:
        if self._started or self._finished:
            return False
        self._started = True
        self._start_next()
        return True

    def abort(self) -> None:
        self._finished = True
        self._timeout_timer.stop()
        process, self._process = self._process, None
        if process is not None:
            process.kill()

    def _start_next(self) -> None:
        if self._finished:
            return
        if self._candidate_index >= len(self._candidates):
            self._fail()
            return
        candidate = self._candidates[self._candidate_index]
        if self._windows and candidate.path.casefold().endswith(".cmd"):
            cmd_executable = resolve_cmd_executable(self._environ, which=self._which)
            if cmd_executable is None:
                self._advance_after_failure(candidate)
                return
        else:
            cmd_executable = None
        self._stdout.clear()
        self._output_size = 0
        process = self._process_factory(self)
        self._process = process
        process.finished.connect(lambda *_args, process=process: self._on_finished(process))
        process.errorOccurred.connect(lambda *_args, process=process: self._on_error(process))
        process.readyReadStandardOutput.connect(
            lambda process=process: self._read_output(process, standard_error=False)
        )
        process.readyReadStandardError.connect(
            lambda process=process: self._read_output(process, standard_error=True)
        )
        if cmd_executable is None:
            process.setProgram(candidate.path)
            process.setArguments(["--version"])
        else:
            process.setProgram(cmd_executable)
            process.setNativeArguments(f'/d /s /c ""{candidate.path}" --version"')
        process.start()
        self._timeout_timer.start()

    def _read_output(self, process: Any, *, standard_error: bool) -> None:
        if process is not self._process or self._finished:
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
            self._reject_current(process, kill=False)
            return
        self._output_size += len(chunk)
        if self._output_size > _MAX_PROBE_OUTPUT:
            self._reject_current(process, kill=True)
            return
        if not standard_error:
            self._stdout.extend(chunk)

    def _on_finished(self, process: Any) -> None:
        if process is not self._process or self._finished:
            return
        self._read_output(process, standard_error=False)
        self._read_output(process, standard_error=True)
        if process is not self._process or self._finished:
            return
        try:
            exit_code = process.exitCode()
        except RuntimeError:
            self._reject_current(process, kill=False)
            return
        if exit_code != 0:
            self._reject_current(process, kill=False)
            return
        try:
            version = parse_codex_version(bytes(self._stdout))
        except ValueError:
            self._reject_current(process, kill=False)
            return
        self._timeout_timer.stop()
        self._process = None
        self._finished = True
        candidate = self._candidates[self._candidate_index]
        self.resolved.emit(CodexResolution(candidate.path, version, candidate.source))

    def _on_error(self, process: Any) -> None:
        if process is self._process and not self._finished:
            self._reject_current(process, kill=True)

    def _on_timeout(self) -> None:
        process = self._process
        if process is not None:
            self._reject_current(process, kill=True)

    def _reject_current(self, process: Any, *, kill: bool) -> None:
        if process is not self._process or self._finished:
            return
        candidate = self._candidates[self._candidate_index]
        self._timeout_timer.stop()
        self._process = None
        if kill:
            active_process: Any = process
            active_process.kill()
        self._advance_after_failure(candidate)

    def _advance_after_failure(self, candidate: CodexCandidate) -> None:
        if candidate.source == "explicit":
            self._fail()
            return
        self._candidate_index += 1
        self._start_next()

    def _fail(self) -> None:
        self._finished = True
        self.failed.emit("Codex version could not be verified")
