from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigurationError(ValueError):
    """Raised when an environment setting cannot be used safely."""


def _split(value: str | None, separator: str) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(part.strip() for part in value.split(separator) if part.strip())


def _positive_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
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

    @classmethod
    def from_env(cls) -> BridgeConfig:
        wait_max = _positive_float("CODEX_BRIDGE_WAIT_MAX_SECONDS", 30.0)
        wait_default = _positive_float("CODEX_BRIDGE_WAIT_DEFAULT_SECONDS", 18.0)
        if wait_max > 30.0:
            raise ConfigurationError("maximum wait must not exceed 30 seconds")
        if wait_default > wait_max:
            raise ConfigurationError(
                "default wait cannot exceed the configured default wait maximum"
            )

        port_raw = os.environ.get("CODEX_BRIDGE_PORT", "8000")
        try:
            port = int(port_raw)
        except ValueError as exc:
            raise ConfigurationError("CODEX_BRIDGE_PORT must be an integer") from exc
        if not 1 <= port <= 65535:
            raise ConfigurationError("CODEX_BRIDGE_PORT must be between 1 and 65535")

        ui_port_raw = os.environ.get("CODEX_BRIDGE_UI_PORT", "8001")
        try:
            ui_port = int(ui_port_raw)
        except ValueError as exc:
            raise ConfigurationError("CODEX_BRIDGE_UI_PORT must be an integer") from exc
        if not 1 <= ui_port <= 65535:
            raise ConfigurationError("CODEX_BRIDGE_UI_PORT must be between 1 and 65535")
        if ui_port == port:
            raise ConfigurationError("CODEX_BRIDGE_UI_PORT must differ from MCP port")

        executable = os.environ.get("CODEX_BRIDGE_CODEX_EXECUTABLE", "codex").strip()
        if not executable:
            raise ConfigurationError("CODEX_BRIDGE_CODEX_EXECUTABLE must not be empty")

        return cls(
            host=os.environ.get("CODEX_BRIDGE_HOST", "127.0.0.1").strip() or "127.0.0.1",
            port=port,
            ui_port=ui_port,
            allowed_roots=_split(os.environ.get("CODEX_BRIDGE_ALLOWED_ROOTS"), os.pathsep),
            allowed_hosts=_split(os.environ.get("CODEX_BRIDGE_ALLOWED_HOSTS"), ","),
            allowed_origins=_split(os.environ.get("CODEX_BRIDGE_ALLOWED_ORIGINS"), ","),
            codex_executable=executable,
            wait_default_seconds=wait_default,
            wait_max_seconds=wait_max,
            shutdown_grace_seconds=_positive_float("CODEX_BRIDGE_SHUTDOWN_GRACE_SECONDS", 3.0),
        )
