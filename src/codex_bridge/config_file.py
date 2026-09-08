from __future__ import annotations

import os
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any


class ConfigFileError(ValueError):
    """Raised when the user-local TOML configuration cannot be used safely."""


_SCHEMA: dict[str, dict[str, type[object]]] = {
    "bridge": {"allowed_roots": list, "codex_executable": str},
    "console": {"ui_port": int},
    "tunnel": {"executable": str, "profile": str},
}
_SECRET_TERMS = ("key", "token", "secret", "password", "credential")


def default_config_path(
    environ: Mapping[str, str] | None = None,
    *,
    platform: str | None = None,
) -> Path:
    values = os.environ if environ is None else environ
    override = values.get("CODEX_BRIDGE_CONFIG")
    if override is not None:
        if not override.strip():
            raise ConfigFileError("CODEX_BRIDGE_CONFIG must be a non-empty file path")
        return Path(override).expanduser()
    current_platform = sys.platform if platform is None else platform
    if current_platform.startswith("win"):
        root = values.get("APPDATA")
        base = Path(root) if root else Path.home() / "AppData" / "Roaming"
        return base / "CodexBridge" / "config.toml"
    root = values.get("XDG_CONFIG_HOME")
    base = Path(root).expanduser() if root else Path.home() / ".config"
    return base / "codexbridge" / "config.toml"


def _secret_setting(section: str, key: str) -> bool:
    lowered = f"{section}.{key}".casefold()
    return any(term in lowered for term in _SECRET_TERMS)


def load_user_config(
    environ: Mapping[str, str] | None = None,
    *,
    platform: str | None = None,
    path: str | os.PathLike[str] | None = None,
) -> dict[str, dict[str, Any]]:
    config_path = (
        Path(path).expanduser()
        if path is not None
        else default_config_path(environ, platform=platform)
    )
    try:
        raw = config_path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ConfigFileError("CodexBridge configuration file cannot be read") from exc
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigFileError("CodexBridge configuration file contains invalid TOML") from exc
    if not isinstance(document, dict):
        raise ConfigFileError("CodexBridge configuration must be a TOML table")

    result: dict[str, dict[str, Any]] = {}
    for section, values in document.items():
        if section not in _SCHEMA or not isinstance(values, dict):
            raise ConfigFileError(f"unsupported CodexBridge configuration section: {section}")
        allowed = _SCHEMA[section]
        normalized: dict[str, Any] = {}
        for key, value in values.items():
            if _secret_setting(section, key):
                raise ConfigFileError(
                    f"secret setting {section}.{key} must not be stored in the config file"
                )
            expected = allowed.get(key)
            if expected is None:
                raise ConfigFileError(f"unsupported CodexBridge configuration key: {section}.{key}")
            if expected is int and (not isinstance(value, int) or isinstance(value, bool)):
                raise ConfigFileError(f"{section}.{key} must be an integer")
            if expected is list:
                if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                    raise ConfigFileError(f"{section}.{key} must be an array of strings")
            elif expected is str and not isinstance(value, str):
                raise ConfigFileError(f"{section}.{key} must be a string")
            normalized[key] = value
        result[section] = normalized
    return result
