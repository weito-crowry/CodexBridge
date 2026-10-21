from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ..config import ConfigurationError, validate_allowed_roots
from ..config_file import ConfigFileError, active_config_source, load_user_config


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


def parse_attachment_staging_directory(value: object, allowed_roots: tuple[str, ...]) -> str:
    source = "console.attachment_staging_directory"
    if not isinstance(value, str) or not value.strip() or not Path(value).is_absolute():
        raise ConsoleConfigurationError(f"{source} must be an absolute path")
    candidate = Path(value)
    if ".." in candidate.parts:
        raise ConsoleConfigurationError(f"{source} must not contain parent traversal")
    try:
        canonical = candidate.resolve(strict=False)
        roots = tuple(Path(root).resolve(strict=True) for root in allowed_roots)
    except (OSError, RuntimeError) as exc:
        raise ConsoleConfigurationError(f"{source} cannot be resolved") from exc
    for root in roots:
        try:
            if os.path.commonpath(
                (os.path.normcase(os.fspath(root)), os.path.normcase(os.fspath(canonical)))
            ) == os.path.normcase(os.fspath(root)):
                return os.fspath(canonical)
        except ValueError:
            continue
    raise ConsoleConfigurationError(f"{source} must be inside bridge.allowed_roots")


@dataclass(frozen=True, slots=True)
class ConsoleConfig:
    """Small console-only configuration; the host is intentionally fixed."""

    host: str = "127.0.0.1"
    port: int = 8001
    tunnel_executable: str | None = None
    tunnel_profile: str = "codex-bridge"
    tunnel_profile_dir: str | None = None
    tunnel_profile_file: str | None = None
    tunnel_profile_settings: Mapping[str, object] = field(default_factory=dict)
    allowed_roots: tuple[str, ...] = ()
    attachment_staging_directory: str | None = None
    codex_executable: str | None = None
    codex_executable_source: str = "default"
    tunnel_executable_source: str = "default"
    config_path: str | None = None
    config_mode: str = "legacy"
    explicit_overrides: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.host != "127.0.0.1":
            raise ConsoleConfigurationError("console host is fixed to 127.0.0.1")
        parse_ui_port(self.port)
        parse_tunnel_profile(self.tunnel_profile)
        if self.attachment_staging_directory is not None:
            parse_attachment_staging_directory(
                self.attachment_staging_directory, self.allowed_roots
            )

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
        explicit_tunnel_profile_dir: str | None = None,
        explicit_attachment_staging_directory: str | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> ConsoleConfig:
        values = os.environ if environ is None else environ
        try:
            source = active_config_source(values)
            file_config = load_user_config(path=source.path)
        except ConfigFileError as exc:
            raise ConsoleConfigurationError(str(exc)) from exc
        if source.mode == "managed":
            managed_settings = {
                "CODEX_BRIDGE_UI_PORT": ("console", "ui_port"),
                "CODEX_BRIDGE_ALLOWED_ROOTS": ("bridge", "allowed_roots"),
                "CODEX_BRIDGE_ATTACHMENT_STAGING_DIRECTORY": (
                    "console",
                    "attachment_staging_directory",
                ),
                "CODEX_BRIDGE_CODEX_EXECUTABLE": ("bridge", "codex_executable"),
                "CODEX_BRIDGE_TUNNEL_EXECUTABLE": ("tunnel", "executable"),
                "CODEX_BRIDGE_TUNNEL_PROFILE": ("tunnel", "profile"),
                "CODEX_BRIDGE_TUNNEL_PROFILE_DIR": ("tunnel", "profile_dir"),
            }
            for variable, (section, key) in managed_settings.items():
                if variable in values:
                    raise ConsoleConfigurationError(
                        f"{variable} conflicts with managed TOML configuration ({section}.{key})"
                    )
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

        if explicit_attachment_staging_directory is not None:
            staging_value: object = explicit_attachment_staging_directory
        elif "CODEX_BRIDGE_ATTACHMENT_STAGING_DIRECTORY" in values:
            staging_value = values["CODEX_BRIDGE_ATTACHMENT_STAGING_DIRECTORY"]
        else:
            staging_value = console_config.get("attachment_staging_directory")
        staging_directory = (
            parse_attachment_staging_directory(staging_value, allowed_roots)
            if staging_value is not None
            else None
        )

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
        if explicit_tunnel_profile_dir is not None:
            profile_dir: object = explicit_tunnel_profile_dir
        else:
            profile_dir = tunnel_config.get("profile_dir")
        if profile_dir is not None and (
            not isinstance(profile_dir, str) or not profile_dir.strip()
        ):
            raise ConsoleConfigurationError("tunnel.profile_dir must be a non-empty path")
        if profile_dir is not None and not Path(profile_dir).is_absolute():
            raise ConsoleConfigurationError("tunnel.profile_dir must be an absolute path")
        profile_file: Path | None = None
        if source.mode in {"explicit", "managed"}:
            profile_directory = (
                Path(profile_dir)
                if profile_dir is not None
                else source.path.resolve(strict=False).parent / ".runtime"
            )
            profile_file = profile_directory / "tunnel-profile.yaml"
        profile_settings = {
            key: value
            for key, value in tunnel_config.items()
            if key not in {"executable", "profile", "profile_dir"}
        }
        profile_settings.setdefault("config_version", 1)
        explicit_overrides: dict[str, object] = {}
        for name, value in (
            ("explicit_port", explicit_port),
            ("explicit_allowed_roots", explicit_allowed_roots),
            ("explicit_codex_executable", explicit_codex_executable),
            ("explicit_tunnel_executable", explicit_tunnel_executable),
            ("explicit_tunnel_profile", explicit_tunnel_profile),
            ("explicit_tunnel_profile_dir", explicit_tunnel_profile_dir),
            ("explicit_attachment_staging_directory", explicit_attachment_staging_directory),
        ):
            if value is not None:
                explicit_overrides[name] = value
        return cls(
            port=port,
            tunnel_executable=tunnel_executable,
            tunnel_profile=parse_tunnel_profile(profile, source="CODEX_BRIDGE_TUNNEL_PROFILE"),
            tunnel_profile_dir=profile_dir,
            tunnel_profile_file=None if profile_file is None else str(profile_file),
            tunnel_profile_settings=profile_settings,
            allowed_roots=allowed_roots,
            attachment_staging_directory=staging_directory,
            codex_executable=codex_executable,
            codex_executable_source=codex_source,
            tunnel_executable_source=tunnel_source,
            config_path=str(source.path),
            config_mode=source.mode,
            explicit_overrides=explicit_overrides,
        )
