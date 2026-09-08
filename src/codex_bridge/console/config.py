from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

from ..config import ConfigurationError, validate_allowed_roots
from ..config_file import ConfigFileError, load_user_config


class ConsoleConfigurationError(ValueError):
    """Raised when console-only configuration cannot be used safely."""


def parse_ui_port(value: object, *, source: str = "UI port") -> int:
    if isinstance(value, bool):
        raise ConsoleConfigurationError(f"{source} must be an integer between 1 and 65535")
    if isinstance(value, int):
        port = value
    elif isinstance(value, str):
        try:
            port = int(value)
        except ValueError as exc:
            raise ConsoleConfigurationError(
                f"{source} must be an integer between 1 and 65535"
            ) from exc
    else:
        raise ConsoleConfigurationError(f"{source} must be an integer between 1 and 65535")
    if not 1 <= port <= 65535:
        raise ConsoleConfigurationError(f"{source} must be an integer between 1 and 65535")
    return port


_TUNNEL_PROFILE_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}")


def parse_tunnel_profile(value: object, *, source: str = "Tunnel profile") -> str:
    if not isinstance(value, str) or _TUNNEL_PROFILE_PATTERN.fullmatch(value) is None:
        raise ConsoleConfigurationError(
            f"{source} must contain 1-64 ASCII letters, digits, '.', '_' or '-'"
        )
    return value


@dataclass(frozen=True, slots=True)
class ConsoleConfig:
    """Small console-only configuration; the host is intentionally fixed."""

    host: str = "127.0.0.1"
    port: int = 8001
    tunnel_executable: str | None = None
    tunnel_profile: str = "codex-bridge"
    allowed_roots: tuple[str, ...] = ()
    codex_executable: str | None = None
    codex_executable_source: str = "default"
    tunnel_executable_source: str = "default"

    def __post_init__(self) -> None:
        if self.host != "127.0.0.1":
            raise ConsoleConfigurationError("console host is fixed to 127.0.0.1")
        parse_ui_port(self.port)
        parse_tunnel_profile(self.tunnel_profile)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def roots_count(self) -> int:
        return len(self.allowed_roots)

    @property
    def roots_error(self) -> str | None:
        try:
            validate_allowed_roots(self.allowed_roots)
        except ConfigurationError as exc:
            return str(exc)
        return None

    @property
    def roots_ready(self) -> bool:
        return self.roots_error is None

    @classmethod
    def from_sources(
        cls,
        explicit_port: int | None = None,
        *,
        explicit_allowed_roots: tuple[str, ...] | None = None,
        explicit_codex_executable: str | None = None,
        explicit_tunnel_executable: str | None = None,
        explicit_tunnel_profile: str | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> ConsoleConfig:
        values = os.environ if environ is None else environ
        try:
            file_config = load_user_config(values)
        except ConfigFileError as exc:
            raise ConsoleConfigurationError(str(exc)) from exc
        bridge_config = file_config.get("bridge", {})
        console_config = file_config.get("console", {})
        tunnel_config = file_config.get("tunnel", {})
        if not all(
            isinstance(section, Mapping)
            for section in (bridge_config, console_config, tunnel_config)
        ):
            raise ConsoleConfigurationError("CodexBridge configuration sections are malformed")
        if explicit_port is not None:
            port = parse_ui_port(explicit_port, source="--ui-port")
        elif "CODEX_BRIDGE_UI_PORT" in values:
            raw_port = values["CODEX_BRIDGE_UI_PORT"]
            port = (
                8001 if raw_port is None else parse_ui_port(raw_port, source="CODEX_BRIDGE_UI_PORT")
            )
        elif "ui_port" in console_config:
            port = parse_ui_port(console_config["ui_port"], source="console.ui_port")
        else:
            port = 8001

        if explicit_allowed_roots is not None:
            allowed_roots = tuple(explicit_allowed_roots)
        elif "CODEX_BRIDGE_ALLOWED_ROOTS" in values:
            raw_roots = values["CODEX_BRIDGE_ALLOWED_ROOTS"]
            allowed_roots = tuple(
                part.strip() for part in raw_roots.split(os.pathsep) if part.strip()
            )
        else:
            raw_roots = bridge_config.get("allowed_roots", ())
            allowed_roots = tuple(raw_roots) if isinstance(raw_roots, list) else ()

        if explicit_codex_executable is not None:
            codex_executable = explicit_codex_executable
            codex_source = "explicit"
        elif "CODEX_BRIDGE_CODEX_EXECUTABLE" in values:
            codex_executable = values["CODEX_BRIDGE_CODEX_EXECUTABLE"]
            codex_source = "environment"
        elif isinstance(bridge_config.get("codex_executable"), str):
            codex_executable = bridge_config["codex_executable"]
            codex_source = "config"
        else:
            codex_executable = None
            codex_source = "default"
        if codex_executable is not None and not codex_executable.strip():
            raise ConsoleConfigurationError("CODEX_BRIDGE_CODEX_EXECUTABLE must not be empty")

        if explicit_tunnel_executable is not None:
            tunnel_executable = explicit_tunnel_executable
            tunnel_source = "explicit"
        elif "CODEX_BRIDGE_TUNNEL_EXECUTABLE" in values:
            tunnel_executable = values["CODEX_BRIDGE_TUNNEL_EXECUTABLE"]
            tunnel_source = "environment"
        elif isinstance(tunnel_config.get("executable"), str):
            tunnel_executable = tunnel_config["executable"]
            tunnel_source = "config"
        else:
            tunnel_executable = None
            tunnel_source = "default"
        if tunnel_executable is not None and not tunnel_executable.strip():
            raise ConsoleConfigurationError("CODEX_BRIDGE_TUNNEL_EXECUTABLE must not be empty")

        if explicit_tunnel_profile is not None:
            profile = explicit_tunnel_profile
        elif "CODEX_BRIDGE_TUNNEL_PROFILE" in values:
            profile = values["CODEX_BRIDGE_TUNNEL_PROFILE"]
        elif "profile" in tunnel_config:
            profile = tunnel_config["profile"]
        else:
            profile = "codex-bridge"
        return cls(
            port=port,
            tunnel_executable=tunnel_executable,
            tunnel_profile=parse_tunnel_profile(profile, source="CODEX_BRIDGE_TUNNEL_PROFILE"),
            allowed_roots=allowed_roots,
            codex_executable=codex_executable,
            codex_executable_source=codex_source,
            tunnel_executable_source=tunnel_source,
        )
