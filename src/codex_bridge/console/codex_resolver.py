from __future__ import annotations

import ntpath
import os
import re
import shutil
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

_CODEX_OVERRIDE = "CODEX_BRIDGE_CODEX_EXECUTABLE"
_MAX_PROBE_OUTPUT = 4 * 1024
_MAX_APP_CANDIDATES = 32
_VERSION_PATTERN = re.compile(r"^codex-cli\s+(\S+)$")


class CodexResolutionError(ValueError):
    """Raised when an explicit Codex executable setting cannot be used."""


@dataclass(frozen=True, slots=True)
class CodexCandidate:
    path: str
    source: str


@dataclass(frozen=True, slots=True)
class CodexResolution:
    path: str
    version: str
    source: str


def _is_windows(platform: str | None) -> bool:
    return (sys.platform if platform is None else platform).startswith("win")


def _canonical_path(path: str, *, windows: bool) -> str:
    resolved = os.path.realpath(path)
    return ntpath.normcase(resolved) if windows else os.path.normcase(resolved)


def _is_usable_path(path: str) -> bool:
    return bool(path) and Path(path).is_file() and Path(path).suffix.casefold() != ".ps1"


def resolve_cmd_executable(
    environ: Mapping[str, str] | None = None,
    *,
    which: Callable[[str], str | None] = shutil.which,
) -> str | None:
    """Resolve a usable Windows command interpreter for batch-file probes."""

    values = os.environ if environ is None else environ
    comspec = values.get("COMSPEC")
    if comspec and _is_usable_path(comspec):
        return comspec
    fallback = which("cmd.exe")
    return fallback if fallback and _is_usable_path(fallback) else None


def _append_candidate(
    candidates: list[CodexCandidate],
    seen: set[str],
    candidate: CodexCandidate,
    *,
    windows: bool,
    validated: bool = False,
) -> None:
    if not candidate.path or candidate.path.casefold().endswith(".ps1"):
        return
    if not validated and not Path(candidate.path).is_file():
        return
    key = _canonical_path(candidate.path, windows=windows)
    if key in seen:
        return
    seen.add(key)
    candidates.append(candidate)


def _explicit_candidate(
    value: str,
    *,
    windows: bool,
    which: Callable[[str], str | None],
) -> CodexCandidate:
    raw = value.strip()
    if not raw:
        raise CodexResolutionError("Configured Codex executable was not found")
    path = raw if os.path.isabs(raw) or (windows and ntpath.isabs(raw)) else which(raw)
    if path is None or not _is_usable_path(path):
        raise CodexResolutionError("Configured Codex executable was not found")
    return CodexCandidate(path, "explicit")


def _native_app_candidates(local_app_data: str, *, windows: bool) -> list[CodexCandidate]:
    if not windows:
        return []
    root = Path(local_app_data) / "OpenAI" / "Codex" / "bin"
    records: list[tuple[int, int, int, str, str]] = []
    try:
        paths = root.glob("*/codex.exe")
    except OSError:
        return []
    for path in paths:
        try:
            if not path.is_file():
                continue
            file_stat = path.stat()
            directory_stat = path.parent.stat()
            records.append(
                (
                    file_stat.st_mtime_ns,
                    directory_stat.st_mtime_ns,
                    file_stat.st_ctime_ns,
                    _canonical_path(str(path), windows=True),
                    str(path),
                )
            )
        except OSError:
            continue
    records.sort(key=lambda record: (-record[0], -record[1], -record[2], record[3]))
    return [CodexCandidate(path, "codex_app") for _, _, _, _, path in records[:_MAX_APP_CANDIDATES]]


def enumerate_candidates(
    environ: Mapping[str, str] | None = None,
    *,
    platform: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> tuple[CodexCandidate, ...]:
    """Return deterministic Codex candidates in the Phase 4A priority order."""

    values = os.environ if environ is None else environ
    windows = _is_windows(platform)
    if _CODEX_OVERRIDE in values:
        return (_explicit_candidate(values[_CODEX_OVERRIDE], windows=windows, which=which),)

    candidates: list[CodexCandidate] = []
    seen: set[str] = set()
    local_app_data = values.get("LOCALAPPDATA")
    if local_app_data:
        for candidate in _native_app_candidates(local_app_data, windows=windows):
            _append_candidate(candidates, seen, candidate, windows=windows)

    path_names = ("codex.exe", "codex.cmd", "codex") if windows else ("codex",)
    for name in path_names:
        found = which(name)
        if found is not None:
            _append_candidate(
                candidates,
                seen,
                CodexCandidate(found, "path"),
                windows=windows,
                validated=True,
            )

    app_data = values.get("APPDATA")
    if app_data:
        npm_path = Path(app_data) / "npm" / "codex.cmd"
        _append_candidate(
            candidates,
            seen,
            CodexCandidate(str(npm_path), "npm"),
            windows=windows,
        )
    return tuple(candidates)


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
        chunk = bytes(reader())
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
        if process.exitCode() != 0:
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
