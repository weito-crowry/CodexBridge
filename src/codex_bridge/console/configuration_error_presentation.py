"""Localize known Console configuration validation without changing config contracts."""

from __future__ import annotations

from .i18n import tr

_SUFFIXES = (
    " must be an integer between 1 and 65535",
    " must contain 1-64 ASCII letters, digits, '.', '_' or '-'",
    " must be an absolute path",
    " must not contain parent traversal",
    " cannot be resolved",
    " must be inside bridge.allowed_roots",
    " must not be empty",
    " must be a non-empty path",
)
_FIXED = {
    "CodexBridge configuration sections are malformed",
    "console host is fixed to 127.0.0.1",
    "CODEX_BRIDGE_ALLOWED_ROOTS must contain at least one allowed root",
    "allowed roots must be absolute directories",
    "allowed roots must be existing directories",
}


def format_configuration_error(message: str) -> str:
    if message in _FIXED:
        return tr(message)
    for suffix in _SUFFIXES:
        if message.endswith(suffix):
            return tr("{p0}" + suffix, p0=message[: -len(suffix)])
    marker = " conflicts with managed TOML configuration ("
    if marker in message and message.endswith(")"):
        source, location = message[:-1].split(marker, 1)
        return tr("{p0} conflicts with managed TOML configuration ({p1})", p0=source, p1=location)
    return message
