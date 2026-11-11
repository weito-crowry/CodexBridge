from __future__ import annotations

from pathlib import Path

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
