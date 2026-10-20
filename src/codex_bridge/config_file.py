from __future__ import annotations

import os
import re
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


class ConfigFileError(ValueError):
    """Raised when the user-local TOML configuration cannot be used safely."""


_SCHEMA: dict[str, dict[str, type[object]]] = {
    "bridge": {
        "host": str,
        "port": int,
        "allowed_roots": list,
        "allowed_hosts": list,
        "allowed_origins": list,
        "codex_executable": str,
        "wait_default_seconds": float,
        "wait_max_seconds": float,
        "shutdown_grace_seconds": float,
        "file_put_dir": str,
    },
    "console": {"ui_port": int, "attachment_staging_directory": str},
    "tunnel": {
        "config_version": int,
        "executable": str,
        "profile": str,
        "profile_dir": str,
    },
    "github_mcp": {
        "enabled": bool,
        "url": str,
        "prefix": str,
        "include": list,
        "exclude": list,
        "toolsets": list,
        "max_tools": int,
    },
}
_SECRET_TERMS = ("key", "token", "secret", "password", "credential", "pat")
_TUNNEL_SECTIONS: dict[str, dict[str, type[object]]] = {
    "control_plane": {
        "base_url": str,
        "tunnel_id": str,
        "api_key": str,
        "client_key": str,
        "private_key": str,
        "token": str,
        "secret": str,
        "password": str,
        "authorization": str,
        "bearer": str,
    },
    "health": {"listen_addr": str},
    "admin_ui": {"open_browser": bool},
    "log": {"level": str, "format": str},
    "mcp": {"server_urls": list},
}
_SECRET_REFERENCE = re.compile(r"(?:env:[A-Za-z_][A-Za-z0-9_]*|file:.+)", re.ASCII)


def _repository_config_path() -> Path | None:
    package_root = Path(__file__).resolve().parents[2]
    config_path = package_root / "config.toml"
    if (package_root / "pyproject.toml").is_file() and config_path.is_file():
        return config_path
    return None


@dataclass(frozen=True, slots=True)
class ConfigSource:
    path: Path
    mode: Literal["explicit", "managed", "legacy"]


def active_config_source(environ: Mapping[str, str] | None = None) -> ConfigSource:
    values = os.environ if environ is None else environ
    override = values.get("CODEX_BRIDGE_CONFIG")
    if override is not None:
        if not override.strip():
            raise ConfigFileError("CODEX_BRIDGE_CONFIG must be a non-empty file path")
        return ConfigSource(Path(override).expanduser().resolve(strict=False), "explicit")
    repository_config = _repository_config_path()
    if repository_config is not None:
        return ConfigSource(repository_config, "managed")
    current_platform = sys.platform
    if current_platform.startswith("win"):
        root = values.get("APPDATA")
        base = Path(root) if root else Path.home() / "AppData" / "Roaming"
        return ConfigSource((base / "CodexBridge" / "config.toml").resolve(strict=False), "legacy")
    root = values.get("XDG_CONFIG_HOME")
    base = Path(root).expanduser() if root else Path.home() / ".config"
    return ConfigSource((base / "codexbridge" / "config.toml").resolve(strict=False), "legacy")


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
    repository_config = _repository_config_path()
    if repository_config is not None:
        return repository_config
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


def _validate_tunnel_section(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigFileError(f"tunnel.{name} must be a TOML table")
    schema = _TUNNEL_SECTIONS[name]
    normalized: dict[str, Any] = {}
    for key, item in value.items():
        expected = schema.get(key)
        if expected is None:
            raise ConfigFileError(f"unsupported CodexBridge configuration key: tunnel.{name}.{key}")
        if name == "mcp" and key == "server_urls":
            if not isinstance(item, list) or not all(isinstance(entry, dict) for entry in item):
                raise ConfigFileError("tunnel.mcp.server_urls must be an array of tables")
            entries: list[dict[str, str]] = []
            for entry in item:
                if set(entry) - {"channel", "url"} or not all(
                    isinstance(entry.get(field), str) for field in ("channel", "url")
                ):
                    raise ConfigFileError(
                        "tunnel.mcp.server_urls entries require string channel and url"
                    )
                entries.append({"channel": entry["channel"], "url": entry["url"]})
            normalized[key] = entries
            continue
        if expected is bool and not isinstance(item, bool):
            raise ConfigFileError(f"tunnel.{name}.{key} must be a boolean")
        if expected is str and not isinstance(item, str):
            raise ConfigFileError(f"tunnel.{name}.{key} must be a string")
        if _secret_setting(name, key) and (
            not isinstance(item, str) or _SECRET_REFERENCE.fullmatch(item) is None
        ):
            raise ConfigFileError(f"tunnel.{name}.{key} must be an env: or file: reference")
        normalized[key] = item
    return normalized


def load_user_config(
    environ: Mapping[str, str] | None = None,
    *,
    platform: str | None = None,
    path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
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

    result: dict[str, Any] = {}
    for section, values in document.items():
        if section == "targets":
            if not isinstance(values, dict):
                raise ConfigFileError("targets must be a TOML table")
            targets: dict[str, dict[str, Any]] = {}
            target_fields = {"name": str, "kind": str, "url": str}
            for target_id, definition in values.items():
                if not isinstance(target_id, str) or not isinstance(definition, dict):
                    raise ConfigFileError("each targets entry must be a table")
                normalized_target: dict[str, Any] = {}
                for key, value in definition.items():
                    if _secret_setting("targets", key):
                        raise ConfigFileError(
                            "secret setting "
                            f"targets.{target_id}.{key} must not be stored in the config file"
                        )
                    target_expected = target_fields.get(key)
                    if target_expected is None:
                        raise ConfigFileError(
                            f"unsupported CodexBridge configuration key: targets.{target_id}.{key}"
                        )
                    if not isinstance(value, target_expected):
                        raise ConfigFileError(f"targets.{target_id}.{key} must be a string")
                    normalized_target[key] = value
                targets[target_id] = normalized_target
            result[section] = targets
            continue
        if section not in _SCHEMA or not isinstance(values, dict):
            raise ConfigFileError(f"unsupported CodexBridge configuration section: {section}")
        allowed = _SCHEMA[section]
        normalized: dict[str, Any] = {}
        for key, value in values.items():
            if section == "tunnel" and key in _TUNNEL_SECTIONS:
                normalized[key] = _validate_tunnel_section(value, key)
                continue
            if _secret_setting(section, key):
                if (
                    section != "tunnel"
                    or not isinstance(value, str)
                    or _SECRET_REFERENCE.fullmatch(value) is None
                ):
                    raise ConfigFileError(
                        f"secret setting {section}.{key} must not be stored in the config file"
                    )
            expected = allowed.get(key)
            if expected is None:
                raise ConfigFileError(f"unsupported CodexBridge configuration key: {section}.{key}")
            if expected is int and (not isinstance(value, int) or isinstance(value, bool)):
                raise ConfigFileError(f"{section}.{key} must be an integer")
            if expected is float and (
                not isinstance(value, (int, float)) or isinstance(value, bool)
            ):
                raise ConfigFileError(f"{section}.{key} must be a number")
            if expected is bool and not isinstance(value, bool):
                raise ConfigFileError(f"{section}.{key} must be a boolean")
            if expected is list:
                if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                    raise ConfigFileError(f"{section}.{key} must be an array of strings")
            elif expected is str and not isinstance(value, str):
                raise ConfigFileError(f"{section}.{key} must be a string")
            normalized[key] = value
        result[section] = normalized
    return result
