from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config_file import ConfigFileError, load_user_config


class ConfigurationError(ValueError):
    """Raised when an environment setting cannot be used safely."""


WAIT_HARD_MAX_SECONDS = 55.0


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


def _boolean(value: object, source: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ConfigurationError(f"{source} must be a boolean")


def _patterns(value: object, source: str, *, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    if isinstance(value, str):
        parts: Sequence[object] = value.split(",")
    elif isinstance(value, (list, tuple)):
        parts = value
    else:
        raise ConfigurationError(f"{source} must be comma-separated patterns or an array")

    normalized: list[str] = []
    for item in parts:
        if not isinstance(item, str) or not item.strip():
            raise ConfigurationError(f"{source} contains an empty pattern")
        pattern = item.strip()
        if "\x00" in pattern:
            raise ConfigurationError(f"{source} contains an invalid pattern")
        bracket_depth = 0
        for character in pattern:
            if character == "[":
                bracket_depth += 1
            elif character == "]":
                if bracket_depth == 0:
                    raise ConfigurationError(f"{source} contains an invalid pattern")
                bracket_depth -= 1
        if bracket_depth:
            raise ConfigurationError(f"{source} contains an invalid pattern")
        normalized.append(pattern)
    return tuple(normalized)


def _toolsets(value: object, source: str) -> tuple[str, ...]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return ()
    if isinstance(value, str):
        parts: Sequence[object] = value.split(",")
    elif isinstance(value, (list, tuple)):
        parts = value
    else:
        raise ConfigurationError(f"{source} must be comma-separated toolsets or an array")

    normalized: list[str] = []
    for item in parts:
        if not isinstance(item, str) or not item.strip():
            raise ConfigurationError(f"{source} contains an empty toolset")
        normalized.append(item.strip())
    return tuple(normalized)


def _github_url(value: object, source: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{source} must be an HTTP(S) URL")
    url = value.strip()
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        parsed_port = parsed.port
    except ValueError as exc:
        raise ConfigurationError(f"{source} must be an HTTP(S) URL") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (parsed_port is not None and not 1 <= parsed_port <= 65_535)
    ):
        raise ConfigurationError(f"{source} must be an HTTP(S) URL")
    return url


def _github_prefix(value: object, source: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigurationError(f"{source} must not be empty")
    if re.fullmatch(r"[A-Za-z0-9_.-]+", value) is None:
        raise ConfigurationError(f"{source} contains unsupported tool-name characters")
    return value


@dataclass(frozen=True, slots=True)
class GitHubMcpConfig:
    enabled: bool = False
    url: str = "https://api.githubcopilot.com/mcp/x/all"
    prefix: str = "github_"
    include: tuple[str, ...] = ("*",)
    exclude: tuple[str, ...] = ()
    toolsets: tuple[str, ...] = ()
    max_tools: int = 0
    pat: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ConfigurationError("github_mcp.enabled must be a boolean")
        _github_url(self.url, "github_mcp.url")
        _github_prefix(self.prefix, "github_mcp.prefix")
        _patterns(self.include, "github_mcp.include", default=("*",))
        _patterns(self.exclude, "github_mcp.exclude", default=())
        object.__setattr__(self, "toolsets", _toolsets(self.toolsets, "github_mcp.toolsets"))
        if isinstance(self.max_tools, bool) or not isinstance(self.max_tools, int):
            raise ConfigurationError("CODEX_BRIDGE_GITHUB_MCP_MAX_TOOLS must be an integer")
        if self.max_tools < 0:
            raise ConfigurationError("CODEX_BRIDGE_GITHUB_MCP_MAX_TOOLS must be zero or greater")
        if self.enabled and not self.pat:
            raise ConfigurationError(
                "CODEX_BRIDGE_GITHUB_PAT is required when GitHub MCP is enabled"
            )

    @classmethod
    def from_sources(
        cls,
        *,
        environ: Mapping[str, str],
        config_data: Mapping[str, Any],
    ) -> GitHubMcpConfig:
        def setting(name: str, key: str, default: object) -> object:
            return environ[name] if name in environ else config_data.get(key, default)

        enabled = _boolean(
            setting("CODEX_BRIDGE_GITHUB_MCP_ENABLED", "enabled", False),
            "CODEX_BRIDGE_GITHUB_MCP_ENABLED",
        )
        url = _github_url(
            setting(
                "CODEX_BRIDGE_GITHUB_MCP_URL",
                "url",
                "https://api.githubcopilot.com/mcp/x/all",
            ),
            "CODEX_BRIDGE_GITHUB_MCP_URL",
        )
        prefix = _github_prefix(
            setting("CODEX_BRIDGE_GITHUB_MCP_PREFIX", "prefix", "github_"),
            "CODEX_BRIDGE_GITHUB_MCP_PREFIX",
        )
        include = _patterns(
            setting("CODEX_BRIDGE_GITHUB_MCP_INCLUDE", "include", ("*",)),
            "CODEX_BRIDGE_GITHUB_MCP_INCLUDE",
            default=("*",),
        )
        exclude = _patterns(
            setting("CODEX_BRIDGE_GITHUB_MCP_EXCLUDE", "exclude", ()),
            "CODEX_BRIDGE_GITHUB_MCP_EXCLUDE",
            default=(),
        )
        toolsets = _toolsets(
            setting("CODEX_BRIDGE_GITHUB_MCP_TOOLSETS", "toolsets", ()),
            "CODEX_BRIDGE_GITHUB_MCP_TOOLSETS",
        )
        raw_max_tools = setting("CODEX_BRIDGE_GITHUB_MCP_MAX_TOOLS", "max_tools", 0)
        if isinstance(raw_max_tools, str):
            try:
                max_tools = int(raw_max_tools.strip())
            except ValueError as exc:
                raise ConfigurationError(
                    "CODEX_BRIDGE_GITHUB_MCP_MAX_TOOLS must be an integer"
                ) from exc
        elif isinstance(raw_max_tools, int) and not isinstance(raw_max_tools, bool):
            max_tools = raw_max_tools
        else:
            raise ConfigurationError("CODEX_BRIDGE_GITHUB_MCP_MAX_TOOLS must be an integer")
        pat = environ.get("CODEX_BRIDGE_GITHUB_PAT") or None
        return cls(
            enabled=enabled,
            url=url,
            prefix=prefix,
            include=include,
            exclude=exclude,
            toolsets=toolsets,
            max_tools=max_tools,
            pat=pat,
        )


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
    github_mcp: GitHubMcpConfig = field(default_factory=GitHubMcpConfig)

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
        github_mcp_config = file_config.get("github_mcp", {})
        if (
            not isinstance(bridge_config, Mapping)
            or not isinstance(console_config, Mapping)
            or not isinstance(github_mcp_config, Mapping)
        ):
            raise ConfigurationError("CodexBridge configuration sections are malformed")

        wait_max = _positive_float("CODEX_BRIDGE_WAIT_MAX_SECONDS", WAIT_HARD_MAX_SECONDS, values)
        wait_default = _positive_float("CODEX_BRIDGE_WAIT_DEFAULT_SECONDS", 50.0, values)
        if wait_max > WAIT_HARD_MAX_SECONDS:
            raise ConfigurationError(
                f"maximum wait must not exceed {WAIT_HARD_MAX_SECONDS:g} seconds"
            )
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
            github_mcp=GitHubMcpConfig.from_sources(
                environ=values,
                config_data=github_mcp_config,
            ),
        )
