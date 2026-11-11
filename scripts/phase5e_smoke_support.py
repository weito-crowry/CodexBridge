from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

PRODUCTION_PORTS = frozenset({18000, 18001})


def build_isolated_environment(
    base_environ: dict[str, str],
    *,
    temp_root: Path,
    config_path: Path,
    control_token: str,
) -> dict[str, str]:
    blocked_names = {
        "CODEX_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "CODEX_BASE_URL",
        "PYTHONHOME",
        "PYTHONPATH",
    }
    child = {
        key: value
        for key, value in base_environ.items()
        if not key.startswith(("CODEX_BRIDGE_", "DBOS_")) and key not in blocked_names
    }
    child["CODEX_BRIDGE_CONFIG"] = str(config_path)
    child["CODEX_BRIDGE_CONTROL_TOKEN"] = control_token
    child["LOCALAPPDATA"] = str(temp_root / "localappdata")
    child["APPDATA"] = str(temp_root / "appdata")
    child["TEMP"] = str(temp_root / "tmp")
    child["TMP"] = str(temp_root / "tmp")
    child["CODEX_HOME"] = str(temp_root / "codex_home")
    child["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    return child


def build_phase5e2c2_environment(
    base_environ: Mapping[str, str],
    *,
    temp_root: Path,
    config_path: Path,
    control_token: str,
    python_path: Path,
    path_entries: Sequence[str],
) -> dict[str, str]:
    temp = temp_root.resolve(strict=True)
    config = config_path.resolve(strict=False)
    source = python_path.resolve(strict=False)
    if not config.is_relative_to(temp) or not source.is_dir() or not source.is_absolute():
        raise ValueError("isolated bridge paths are invalid")
    if not path_entries or any(not Path(entry).is_absolute() for entry in path_entries):
        raise ValueError("isolated executable search paths are invalid")

    child = {
        key: value
        for key in (
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "PATHEXT",
            "PROCESSOR_ARCHITECTURE",
            "NUMBER_OF_PROCESSORS",
        )
        if isinstance((value := base_environ.get(key)), str) and value
    }
    child.update(
        {
            "PATH": os.pathsep.join(path_entries),
            "CODEX_BRIDGE_CONFIG": str(config),
            "CODEX_BRIDGE_CONTROL_TOKEN": control_token,
            "LOCALAPPDATA": str(temp / "localappdata"),
            "APPDATA": str(temp / "appdata"),
            "TEMP": str(temp / "tmp"),
            "TMP": str(temp / "tmp"),
            "CODEX_HOME": str(temp / "codex_home"),
            "USERPROFILE": str(temp),
            "HOME": str(temp),
            "HOMEDRIVE": temp.drive,
            "HOMEPATH": str(temp)[len(temp.drive) :],
            "PYTHONPATH": str(source),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    return child


def claim_evidence_path(path: Path, evidence: Mapping[str, Any]) -> None:
    if path.parent.is_symlink() or path.is_symlink():
        raise OSError("evidence path must not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        path.unlink(missing_ok=True)
        raise


def roots_are_separate(state_root: Path, workspace_root: Path) -> bool:
    state = Path(state_root).resolve(strict=False)
    workspace = Path(workspace_root).resolve(strict=False)
    return state != workspace and state not in workspace.parents and workspace not in state.parents


def ports_are_safe(mcp_port: int, ui_port: int) -> bool:
    return (
        type(mcp_port) is int
        and type(ui_port) is int
        and 1 <= mcp_port <= 65535
        and 1 <= ui_port <= 65535
        and mcp_port != ui_port
        and mcp_port not in PRODUCTION_PORTS
        and ui_port not in PRODUCTION_PORTS
    )
