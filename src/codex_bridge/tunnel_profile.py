from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

_PROFILE_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
_ENV_REFERENCE = re.compile(r"env:[A-Za-z_][A-Za-z0-9_]*\Z")
_FILE_REFERENCE = re.compile(r"file:[^\r\n]+\Z")
_HTTP_FIELD_NAME = re.compile(r"[!#$%&'*+\-.^_\`|~0-9A-Za-z]+\Z", re.ASCII)
_INVALID_HEADER_VALUE = re.compile(r"[\r\n\x00\x85\u2028\u2029]")
_SECRET_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "bearer",
        "client_key",
        "password",
        "private_key",
        "secret",
        "token",
    }
)
_CREDENTIAL_HEADER_NAMES = frozenset(
    {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key"}
)
_CREDENTIAL_HEADER_TERMS = ("token", "secret", "credential", "api-key", "authentication")
_SECRET_ASSIGNMENT = re.compile(
    r"(?im)(?:^|[{,]\s*)[\"']?(?P<key>api_key|authorization|bearer|client_key|password|"
    r"private_key|secret|token)[\"']?\s*:\s*(?P<value>[^,\n}]+)"
)
_CONFIG_VERSION = re.compile(
    r"(?im)(?:^\s*|[{,]\s*)[\"']?config_version[\"']?\s*:\s*1(?:\s*[,}]|\s*$|\s*#)"
)
_MAX_PROFILE_BYTES = 256 * 1024
_DOCTOR_TIMEOUT_SECONDS = 15

ProfileInput: TypeAlias = Mapping[str, object] | str


class TunnelProfileError(ValueError):
    """A profile could not be safely rendered, validated, or installed."""


@dataclass(frozen=True, slots=True)
class TunnelProfileResult:
    path: Path
    profile_name: str


def _normalize_mcp_extra_headers(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise TunnelProfileError("Tunnel MCP extra_headers must be a mapping of safe strings")
    normalized: dict[str, str] = {}
    for header_name, header_value in value.items():
        if (
            not isinstance(header_name, str)
            or _HTTP_FIELD_NAME.fullmatch(header_name) is None
            or not isinstance(header_value, str)
            or _INVALID_HEADER_VALUE.search(header_value) is not None
        ):
            raise TunnelProfileError(
                "Tunnel MCP extra_headers must map valid HTTP header names to safe strings"
            )
        lowered_name = header_name.casefold()
        credential_header = (
            lowered_name in _CREDENTIAL_HEADER_NAMES
            or lowered_name in _SECRET_KEYS
            or any(term in lowered_name for term in _CREDENTIAL_HEADER_TERMS)
        )
        if credential_header and (
            _ENV_REFERENCE.fullmatch(header_value) is None
            and _FILE_REFERENCE.fullmatch(header_value) is None
        ):
            raise TunnelProfileError(
                "Tunnel MCP credential headers must use env: or file: references"
            )
        normalized[header_name] = header_value
    return normalized


def _normalize_mapping(value: object, path: tuple[str, ...] = ()) -> object:
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TunnelProfileError("Tunnel profile mapping keys must be strings")
            if path == ("mcp",) and key == "extra_headers":
                normalized[key] = _normalize_mcp_extra_headers(item)
                continue
            if key.casefold() in _SECRET_KEYS:
                _validate_secret_reference(item)
            normalized[key] = _normalize_mapping(item, (*path, key))
        return normalized
    if isinstance(value, (list, tuple)):
        return [_normalize_mapping(item, (*path, "[]")) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise TunnelProfileError("Tunnel profile contains an unsupported value type")


def _validate_secret_reference(value: object) -> None:
    if not isinstance(value, str):
        raise TunnelProfileError("Tunnel secret fields must use env: or file: references")
    if _ENV_REFERENCE.fullmatch(value) is None and _FILE_REFERENCE.fullmatch(value) is None:
        raise TunnelProfileError("Tunnel secret fields must use env: or file: references")


def _unquote_scalar(value: str) -> str:
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in "\"'":
        return stripped[1:-1]
    if " #" in stripped:
        return stripped.split(" #", maxsplit=1)[0].rstrip()
    return stripped


def _validate_rendered_yaml(text: str) -> None:
    if not text.strip() or "\x00" in text:
        raise TunnelProfileError("Tunnel profile YAML is empty or invalid")
    if _CONFIG_VERSION.search(text) is None:
        raise TunnelProfileError("Tunnel profile must use config_version 1")

    found_api_key = False
    for match in _SECRET_ASSIGNMENT.finditer(text):
        key = match.group("key").casefold()
        value = _unquote_scalar(match.group("value"))
        if key == "api_key":
            found_api_key = True
        _validate_secret_reference(value)
    if not found_api_key:
        raise TunnelProfileError("Tunnel profile must provide an API key reference")


def _render_profile(profile: ProfileInput) -> str:
    if isinstance(profile, str):
        rendered = profile
        _validate_rendered_yaml(rendered)
    elif isinstance(profile, Mapping):
        normalized = _normalize_mapping(profile)
        if not isinstance(normalized, dict) or normalized.get("config_version") != 1:
            raise TunnelProfileError("Tunnel profile must use config_version 1")
        control_plane = normalized.get("control_plane")
        if not isinstance(control_plane, dict) or "api_key" not in control_plane:
            raise TunnelProfileError("Tunnel profile must provide an API key reference")
        _validate_secret_reference(control_plane["api_key"])
        try:
            rendered = (
                json.dumps(
                    normalized,
                    ensure_ascii=False,
                    allow_nan=False,
                    indent=2,
                )
                + "\n"
            )
        except (TypeError, ValueError) as exc:
            raise TunnelProfileError("Tunnel profile mapping cannot be rendered") from exc
    else:
        raise TunnelProfileError("Tunnel profile must be a mapping or rendered YAML string")

    if len(rendered.encode("utf-8")) > _MAX_PROFILE_BYTES:
        raise TunnelProfileError("Tunnel profile exceeds the maximum supported size")
    return rendered


def generate_tunnel_profile(
    profile: ProfileInput,
    *,
    profile_dir: str | os.PathLike[str],
    profile_name: str,
    client_executable: str | os.PathLike[str],
) -> TunnelProfileResult:
    """Validate and atomically install a v1 YAML profile with the Tunnel client.

    Mapping inputs are rendered as JSON, which is a valid YAML 1.2 document.
    The caller supplies secret references; this function never resolves them.
    """
    if _PROFILE_NAME.fullmatch(profile_name) is None or profile_name in {".", ".."}:
        raise TunnelProfileError("Tunnel profile name is invalid")
    executable = os.fspath(client_executable)
    if not executable:
        raise TunnelProfileError("Tunnel client executable is required")

    rendered = _render_profile(profile)
    directory = Path(profile_dir).absolute()
    target = directory / f"{profile_name}.yaml"
    temp_path: Path | None = None

    try:
        directory.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{profile_name}.",
            suffix=".yaml.tmp",
            dir=directory,
        )
        temp_path = Path(temp_name)
        try:
            os.chmod(temp_path, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(rendered)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise

        try:
            completed = subprocess.run(
                [
                    executable,
                    "doctor",
                    "--profile-file",
                    str(temp_path),
                    "--health.listen-addr",
                    "127.0.0.1:0",
                ],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_DOCTOR_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TunnelProfileError("Tunnel client could not validate the staged profile") from exc
        if completed.returncode != 0:
            raise TunnelProfileError(
                f"Tunnel client rejected the staged profile (exit code {completed.returncode})"
            )

        try:
            os.replace(temp_path, target)
        except OSError as exc:
            raise TunnelProfileError("Tunnel profile could not be atomically installed") from exc
        temp_path = None
        return TunnelProfileResult(path=target, profile_name=profile_name)
    except TunnelProfileError:
        raise
    except OSError as exc:
        raise TunnelProfileError("Tunnel profile could not be written") from exc
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
