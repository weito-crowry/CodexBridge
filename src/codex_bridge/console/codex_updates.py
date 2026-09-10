from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Callable, Mapping
from typing import Any

from PySide6.QtCore import QObject, QProcess, Signal

from . import codex_resolver

CodexResolution = codex_resolver.CodexResolution

_MAX_DOCTOR_OUTPUT = 64 * 1024
_VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+)*)(?:[-+].*)?$")
_UNKNOWN_ACTIONS = {"", "manual", "manual or unknown", "unknown", "unavailable"}


def _text(mapping: Mapping[str, object], *keys: str) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _compare_versions(left: str, right: str) -> int | None:
    left_match = _VERSION_RE.fullmatch(left.strip())
    right_match = _VERSION_RE.fullmatch(right.strip())
    if left_match is None or right_match is None:
        return None
    left_parts = tuple(int(part) for part in left_match.group(1).split("."))
    right_parts = tuple(int(part) for part in right_match.group(1).split("."))
    width = max(len(left_parts), len(right_parts))
    left_key = left_parts + (0,) * (width - len(left_parts))
    right_key = right_parts + (0,) * (width - len(right_parts))
    return (left_key > right_key) - (left_key < right_key)


class CodexUpdateInfo:
    def __init__(
        self,
        current_version: str,
        latest_version: str | None,
        latest_status: str | None,
        update_action: str | None,
        last_checked_at: str | None,
        doctor_version: str | None,
        latest_version_status: str | None = None,
    ) -> None:
        self.current_version = current_version
        self.latest_version = latest_version
        self.check_status = latest_status
        self.latest_status = latest_status
        self.latest_version_status = latest_version_status
        self.update_action = update_action
        self.last_checked_at = last_checked_at
        self.doctor_version = doctor_version

    @property
    def update_available(self) -> bool:
        if self.latest_version is None or self.check_status != "ok":
            return False
        comparison = _compare_versions(self.latest_version, self.current_version)
        return comparison is not None and comparison > 0

    @property
    def can_update(self) -> bool:
        return self.update_available and (self.update_action or "").casefold() not in {
            value.casefold() for value in _UNKNOWN_ACTIONS
        }

    @property
    def display_status(self) -> str:
        if self.update_available:
            return "Update available"
        if self.latest_version is not None and self.check_status == "ok":
            return "Up to date"
        return "Unavailable"


def parse_codex_update_info(payload: object, *, current_version: str) -> CodexUpdateInfo:
    checks = payload.get("checks") if isinstance(payload, Mapping) else None
    check = checks.get("updates.status") if isinstance(checks, Mapping) else None
    details = check.get("details") if isinstance(check, Mapping) else None
    detail_mapping = details if isinstance(details, Mapping) else {}
    return CodexUpdateInfo(
        current_version=current_version,
        latest_version=_text(detail_mapping, "latest version", "cached latest version"),
        latest_status=_text(check, "status") if isinstance(check, Mapping) else None,
        update_action=_text(detail_mapping, "update action"),
        last_checked_at=_text(detail_mapping, "last checked at"),
        doctor_version=_text(payload, "codexVersion") if isinstance(payload, Mapping) else None,
        latest_version_status=_text(detail_mapping, "latest version status"),
    )


ProcessFactory = Callable[[QObject], Any]


class CodexUpdateProbe(QObject):
    """Run doctor/update asynchronously for one already-resolved Codex executable."""

    check_succeeded = Signal(object)
    check_failed = Signal(str)
    update_succeeded = Signal()
    update_failed = Signal(str)
    busy_changed = Signal(bool)

    def __init__(
        self,
        *,
        platform: str | None = None,
        environ: Mapping[str, str] | None = None,
        which: Callable[[str], str | None] = shutil.which,
        process_factory: ProcessFactory | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._windows = codex_resolver._is_windows(platform)  # type: ignore[attr-defined]
        self._environ = os.environ if environ is None else environ
        self._which = which
        self._process_factory = process_factory or (lambda owner: QProcess(owner))
        self._process: Any | None = None
        self._operation: str | None = None
        self._stdout = bytearray()
        self._stderr = bytearray()

    @property
    def busy(self) -> bool:
        return self._process is not None

    def check_for_updates(self, resolution: CodexResolution) -> bool:
        return self._start("check", resolution)

    def update(self, resolution: CodexResolution) -> bool:
        return self._start("update", resolution)

    def abort(self) -> None:
        process, self._process = self._process, None
        self._operation = None
        if process is not None:
            process.kill()
            process.deleteLater()
            self.busy_changed.emit(False)

    def _start(self, operation: str, resolution: CodexResolution) -> bool:
        if self._process is not None:
            return False
        command = None
        if self._windows and resolution.path.casefold().endswith(".cmd"):
            command = codex_resolver.resolve_cmd_executable(  # type: ignore[attr-defined]
                self._environ, which=self._which
            )
            if command is None:
                self._emit_failure(operation)
                return False
        process = self._process_factory(self)
        self._process = process
        self._operation = operation
        self._stdout.clear()
        self._stderr.clear()
        process.finished.connect(lambda *_args, process=process: self._on_finished(process))
        process.errorOccurred.connect(lambda *_args, process=process: self._on_error(process))
        process.readyReadStandardOutput.connect(
            lambda process=process: self._read_output(process, standard_error=False)
        )
        process.readyReadStandardError.connect(
            lambda process=process: self._read_output(process, standard_error=True)
        )
        if command is None:
            process.setProgram(resolution.path)
            process.setArguments(["doctor", "--json"] if operation == "check" else ["update"])
        else:
            arguments = "doctor --json" if operation == "check" else "update"
            process.setProgram(command)
            process.setNativeArguments(f'/d /s /c ""{resolution.path}" {arguments}"')
        self.busy_changed.emit(True)
        process.start()
        return True

    def _read_output(self, process: Any, *, standard_error: bool) -> None:
        if process is not self._process:
            return
        active_process: Any = process
        reader = (
            active_process.readAllStandardError
            if standard_error
            else active_process.readAllStandardOutput
        )
        chunk = bytes(reader())
        target = self._stderr if standard_error else self._stdout
        if len(target) < _MAX_DOCTOR_OUTPUT:
            target.extend(chunk[: _MAX_DOCTOR_OUTPUT - len(target)])

    def _on_error(self, process: Any) -> None:
        if process is self._process:
            self._finish_failure(process)

    def _on_finished(self, process: Any) -> None:
        if process is not self._process:
            return
        self._read_output(process, standard_error=False)
        self._read_output(process, standard_error=True)
        operation = self._operation
        active_process: Any = process
        exit_code = active_process.exitCode()
        self._clear_process(process)
        if operation == "check":
            try:
                payload = json.loads(bytes(self._stdout).decode("utf-8"))
                if not isinstance(payload, Mapping):
                    raise ValueError
            except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError):
                self.check_failed.emit("Codex update check failed")
            else:
                if not self._has_updates_status(payload):
                    self.check_failed.emit("Codex update check failed")
                else:
                    self.check_succeeded.emit(payload)
        elif operation == "update" and exit_code == 0:
            self.update_succeeded.emit()
        else:
            self._emit_failure(operation)

    @staticmethod
    def _has_updates_status(payload: Mapping[str, object]) -> bool:
        checks = payload.get("checks")
        return isinstance(checks, Mapping) and isinstance(checks.get("updates.status"), Mapping)

    def _finish_failure(self, process: Any) -> None:
        operation = self._operation
        self._clear_process(process, kill=True)
        self._emit_failure(operation)

    def _clear_process(self, process: Any, *, kill: bool = False) -> None:
        if process is not self._process:
            return
        self._process = None
        self._operation = None
        active_process: Any = process
        if kill:
            active_process.kill()
        active_process.deleteLater()
        self.busy_changed.emit(False)

    def _emit_failure(self, operation: str | None) -> None:
        if operation == "check":
            self.check_failed.emit("Codex update check failed")
        elif operation == "update":
            self.update_failed.emit("Codex update failed")
