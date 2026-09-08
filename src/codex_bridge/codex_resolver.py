from __future__ import annotations

import ntpath
import os
import shutil
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

_CODEX_OVERRIDE = "CODEX_BRIDGE_CODEX_EXECUTABLE"
_MAX_APP_CANDIDATES = 32


class CodexResolutionError(ValueError):
    """Raised when a Codex executable cannot be resolved safely."""


@dataclass(frozen=True, slots=True)
class CodexCandidate:
    path: str
    source: str


@dataclass(frozen=True, slots=True)
class CodexResolution:
    path: str
    version: str | None
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
    values = os.environ if environ is None else environ
    comspec = values.get("COMSPEC")
    if comspec and _is_usable_path(comspec):
        return comspec
    fallback = which("cmd.exe")
    return fallback if fallback and _is_usable_path(fallback) else None


def _explicit_candidate(
    value: str,
    *,
    source: str,
    windows: bool,
    which: Callable[[str], str | None],
) -> CodexCandidate:
    raw = value.strip()
    path = raw if os.path.isabs(raw) or (windows and ntpath.isabs(raw)) else which(raw)
    if not raw or path is None or not _is_usable_path(path):
        raise CodexResolutionError("Configured Codex executable was not found")
    return CodexCandidate(path, source)


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
    if not validated and not _is_usable_path(candidate.path):
        return
    key = _canonical_path(candidate.path, windows=windows)
    if key in seen:
        return
    seen.add(key)
    candidates.append(candidate)


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
    explicit_executable: str | None = None,
    config_executable: str | None = None,
    platform: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> tuple[CodexCandidate, ...]:
    values = os.environ if environ is None else environ
    windows = _is_windows(platform)
    if explicit_executable is not None:
        return (
            _explicit_candidate(
                explicit_executable, source="explicit", windows=windows, which=which
            ),
        )
    if _CODEX_OVERRIDE in values:
        return (
            _explicit_candidate(
                values[_CODEX_OVERRIDE], source="explicit", windows=windows, which=which
            ),
        )
    if config_executable is not None:
        return (
            _explicit_candidate(config_executable, source="config", windows=windows, which=which),
        )

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
        _append_candidate(
            candidates,
            seen,
            CodexCandidate(str(Path(app_data) / "npm" / "codex.cmd"), "npm"),
            windows=windows,
        )
    return tuple(candidates)


def resolve_codex_executable(
    *,
    explicit_executable: str | None = None,
    config_executable: str | None,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> CodexResolution:
    candidates = enumerate_candidates(
        environ,
        explicit_executable=explicit_executable,
        config_executable=config_executable,
        platform=platform,
        which=which,
    )
    if not candidates:
        raise CodexResolutionError(
            "Codex executable was not found. Set CODEX_BRIDGE_CODEX_EXECUTABLE "
            "or install/use Codex App."
        )
    candidate = candidates[0]
    return CodexResolution(candidate.path, None, candidate.source)
