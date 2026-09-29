from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

_MAX_STATE_BYTES = 8 * 1024 * 1024
_MAX_PROJECTS = 1_000
_MAX_ROOTS_PER_PROJECT = 100
_MAX_PROJECT_NAME_LENGTH = 200
_MAX_ROOT_PATH_LENGTH = 4_096


def normalize_cwd(value: object) -> str | None:
    """Return a platform-native key for an absolute cwd without filesystem access."""
    if not isinstance(value, str) or not value.strip() or not os.path.isabs(value):
        return None
    try:
        return os.path.normcase(os.path.normpath(value))
    except (OSError, ValueError):
        return None


def read_local_project_names(state_path: Path | None = None) -> dict[str, str]:
    """Read friendly names for exact local-project roots from Codex global state."""
    try:
        path = state_path or (Path.home() / ".codex" / ".codex-global-state.json")
        with path.open("rb") as state_file:
            raw_state = state_file.read(_MAX_STATE_BYTES + 1)
    except (OSError, RuntimeError, ValueError):
        return {}

    if len(raw_state) > _MAX_STATE_BYTES:
        return {}
    try:
        state = json.loads(raw_state.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return {}
    if not isinstance(state, Mapping):
        return {}
    projects = state.get("local-projects")
    if not isinstance(projects, Mapping) or len(projects) > _MAX_PROJECTS:
        return {}

    project_names: dict[str, str] = {}
    for project in projects.values():
        if not isinstance(project, Mapping):
            return {}
        name = project.get("name")
        roots = project.get("rootPaths")
        if (
            not isinstance(name, str)
            or len(name) > _MAX_PROJECT_NAME_LENGTH
            or not isinstance(roots, list)
            or len(roots) > _MAX_ROOTS_PER_PROJECT
            or any(not isinstance(root, str) or len(root) > _MAX_ROOT_PATH_LENGTH for root in roots)
        ):
            return {}
        friendly_name = name.strip()
        if not friendly_name:
            continue
        for root in roots:
            key = normalize_cwd(root)
            if key is not None:
                project_names.setdefault(key, friendly_name)
    return project_names
