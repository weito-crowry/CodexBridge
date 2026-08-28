from __future__ import annotations

import ntpath
import os
import re
import shutil
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path


_TUNNEL_OVERRIDE = "CODEX_BRIDGE_TUNNEL_EXECUTABLE"
_PS1_SUFFIX = re.compile(r"\.ps1$", re.IGNORECASE)


class TunnelResolutionError(ValueError):
    """Raised when the configured Tunnel executable cannot be used."""


@dataclass(frozen=True, slots=True)
class TunnelCandidate:
    path: str
    source: str


def _is_windows(platform: str | None) -> bool:
    return (sys.platform if platform is None else platform).startswith("win")


def _canonical_path(path: str, *, windows: bool) -> str:
    resolved = os.path.realpath(path)
    return ntpath.normcase(resolved) if windows else os.path.normcase(resolved)


def _is_usable_path(path: str) -> bool:
    return bool(path) and not _PS1_SUFFIX.search(path) and Path(path).is_file()


def _append_candidate(
    candidates: list[TunnelCandidate],
    seen: set[str],
    candidate: TunnelCandidate,
    *,
    windows: bool,
    validated: bool = False,
) -> None:
    if not candidate.path or _PS1_SUFFIX.search(candidate.path):
        return
    if not validated and not _is_usable_path(candidate.path):
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
) -> TunnelCandidate:
    raw = value.strip()
    if not raw or _PS1_SUFFIX.search(raw):
        raise TunnelResolutionError("Configured Tunnel executable was not found")
    path = raw if os.path.isabs(raw) or (windows and ntpath.isabs(raw)) else which(raw)
    if path is None or not _is_usable_path(path):
        raise TunnelResolutionError("Configured Tunnel executable was not found")
    return TunnelCandidate(path, "explicit")


def enumerate_candidates(
    environ: Mapping[str, str] | None = None,
    *,
    platform: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> tuple[TunnelCandidate, ...]:
    """Return bounded Tunnel executable candidates in priority order."""

    values = os.environ if environ is None else environ
    windows = _is_windows(platform)
    if _TUNNEL_OVERRIDE in values:
        return (_explicit_candidate(values[_TUNNEL_OVERRIDE], windows=windows, which=which),)

    candidates: list[TunnelCandidate] = []
    seen: set[str] = set()
    path_names = ("tunnel-client.exe", "tunnel-client") if windows else ("tunnel-client",)
    for name in path_names:
        found = which(name)
        if found is not None:
            _append_candidate(
                candidates,
                seen,
                TunnelCandidate(found, "path"),
                windows=windows,
                validated=True,
            )
    return tuple(candidates)
