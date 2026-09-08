from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config_file import ConfigFileError, load_user_config


class ConfigurationError(ValueError):
    """Raised when an environment setting cannot be used safely."""


def validate_allowed_roots(allowed_roots: tuple[str, ...]) -> tuple[str, ...]:
    if not allowed_roots:
        raise ConfigurationError(
            "CODEX_BRIDGE_ALLOWED_ROOTS must contain at least one allowed root"
        )
    canonical: list[str] = []
    for root in allowed_roots:
        if not isinstance(root, str) or not root or not Path(root).is_absolute():
            raise ConfigurationError("allowed roots must be absolute directories")
        try:
            resolved = Path(root).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ConfigurationError("allowed roots must be existing directories") from exc
        if not resolved.is_dir():
            raise ConfigurationError("allowed roots must be existing directories")
        canonical.append(os.path.normcase(os.fspath(resolved)))
    return tuple(canonical)


_CONTROL_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,256}", re.ASCII)


def _control_token(environ: Mapping[str, str]) -> str | None:
    raw = environ.get("CODEX_BRIDGE_CONTROL_TOKEN")
    if raw is None or raw == "":
        return None
    if _CONTROL_TOKEN_PATTERN.fullmatch(raw) is None:
        raise ConfigurationError(
            "CODEX_BRIDGE_CONTROL_TOKEN must be 32-256 ASCII URL-safe characters"
        )
    return raw


def _split(value: str | None, separator: str) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(part.strip() for part in value.split(separator) if part.strip())


def _config(environ: Mapping[str, str], config_data: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if config_data is not None:
        return config_data
    try:
        return load_user_config(environ)
    except ConfigFileError as exc:
        raise ConfigurationError(str(exc)) from exc


def _port(value: object, source: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65_535:
        raise ConfigurationError(f"{source} must be an integer between 1 and 65535")
    return value


def _positive_float(name: str, default: float, environ: Mapping[str, str]) -> float:
    raw = environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a positive number") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be a positive number")
    return value


@dataclass(frozen=True, slots=True)
class BridgeConfig:
    host: str
    port: int
    ui_port: int
    allowed_roots: tuple[str, ...]
    allowed_hosts: tuple[str, ...]
    allowed_origins: tuple[str, ...]
    codex_executable: str
    wait_default_seconds: float
    wait_max_seconds: float
    shutdown_grace_seconds: float
    control_token: str | None = None
    codex_executable_source: str = "default"

    @classmethod
    def from_env(cls) -> BridgeConfig:
        return cls.from_sources(environ=os.environ)

    @classmethod
    def from_sources(
        cls,
        *,
        explicit_allowed_roots: tuple[str, ...] | None = None,
        explicit_codex_executable: str | None = None,
        explicit_ui_port: int | None = None,
        environ: Mapping[str, str] | None = None,
        config_data: Mapping[str, Any] | None = None,
    ) -> BridgeConfig:
        values = os.environ if environ is None else environ
        file_config = _config(values, config_data)
        bridge_config = file_config.get("bridge", {})
        console_config = file_config.get("console", {})
        if not isinstance(bridge_config, Mapping) or not isinstance(console_config, Mapping):
            raise ConfigurationError("CodexBridge configuration sections are malformed")

        wait_max = _positive_float("CODEX_BRIDGE_WAIT_MAX_SECONDS", 30.0, values)
        wait_default = _positive_float("CODEX_BRIDGE_WAIT_DEFAULT_SECONDS", 18.0, values)
        if wait_max > 30.0:
            raise ConfigurationError("maximum wait must not exceed 30 seconds")
        if wait_default > wait_max:
            raise ConfigurationError(
                "default wait cannot exceed the configured default wait maximum"
            )

        port_raw = values.get("CODEX_BRIDGE_PORT", "8000")
        try:
            port = int(port_raw)
        except ValueError as exc:
            raise ConfigurationError("CODEX_BRIDGE_PORT must be an integer") from exc
        if not 1 <= port <= 65535:
            raise ConfigurationError("CODEX_BRIDGE_PORT must be between 1 and 65535")

        if explicit_ui_port is not None:
            ui_port = _port(explicit_ui_port, "--ui-port")
        elif "CODEX_BRIDGE_UI_PORT" in values:
            try:
                ui_port = _port(int(values["CODEX_BRIDGE_UI_PORT"]), "CODEX_BRIDGE_UI_PORT")
            except ValueError as exc:
                raise ConfigurationError("CODEX_BRIDGE_UI_PORT must be an integer") from exc
        elif "ui_port" in console_config:
            ui_port = _port(console_config["ui_port"], "console.ui_port")
        else:
            ui_port = 8001
        if ui_port == port:
            raise ConfigurationError("CODEX_BRIDGE_UI_PORT must differ from MCP port")

        if explicit_codex_executable is not None:
            executable = explicit_codex_executable.strip()
            executable_source = "explicit"
        elif "CODEX_BRIDGE_CODEX_EXECUTABLE" in values:
            executable = values["CODEX_BRIDGE_CODEX_EXECUTABLE"].strip()
            executable_source = "environment"
        elif "codex_executable" in bridge_config:
            configured_executable = bridge_config["codex_executable"]
            if not isinstance(configured_executable, str):
                raise ConfigurationError("bridge.codex_executable must be a string")
            executable = configured_executable.strip()
            executable_source = "config"
        else:
            executable = "codex"
            executable_source = "default"
        if not executable:
            raise ConfigurationError("CODEX_BRIDGE_CODEX_EXECUTABLE must not be empty")

        if explicit_allowed_roots is not None:
            allowed_roots = tuple(explicit_allowed_roots)
        elif "CODEX_BRIDGE_ALLOWED_ROOTS" in values:
            allowed_roots = _split(values["CODEX_BRIDGE_ALLOWED_ROOTS"], os.pathsep)
        else:
            raw_roots = bridge_config.get("allowed_roots", ())
            allowed_roots = tuple(raw_roots) if isinstance(raw_roots, list) else ()

        return cls(
            host=values.get("CODEX_BRIDGE_HOST", "127.0.0.1").strip() or "127.0.0.1",
            port=port,
            ui_port=ui_port,
            allowed_roots=allowed_roots,
            allowed_hosts=_split(values.get("CODEX_BRIDGE_ALLOWED_HOSTS"), ","),
            allowed_origins=_split(values.get("CODEX_BRIDGE_ALLOWED_ORIGINS"), ","),
            codex_executable=executable,
            wait_default_seconds=wait_default,
            wait_max_seconds=wait_max,
            shutdown_grace_seconds=_positive_float(
                "CODEX_BRIDGE_SHUTDOWN_GRACE_SECONDS", 3.0, values
            ),
            control_token=_control_token(values),
            codex_executable_source=executable_source,
        )
