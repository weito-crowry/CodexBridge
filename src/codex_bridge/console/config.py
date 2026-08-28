from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


class ConsoleConfigurationError(ValueError):
    """Raised when the console port cannot be used safely."""


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


@dataclass(frozen=True, slots=True)
class ConsoleConfig:
    """Small console-only configuration; the host is intentionally fixed."""

    host: str = "127.0.0.1"
    port: int = 8001

    def __post_init__(self) -> None:
        if self.host != "127.0.0.1":
            raise ConsoleConfigurationError("console host is fixed to 127.0.0.1")
        parse_ui_port(self.port)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @classmethod
    def from_sources(
        cls,
        explicit_port: int | None = None,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> ConsoleConfig:
        values = os.environ if environ is None else environ
        if explicit_port is not None:
            port = parse_ui_port(explicit_port, source="--ui-port")
        else:
            raw_port = values.get("CODEX_BRIDGE_UI_PORT")
            port = (
                8001 if raw_port is None else parse_ui_port(raw_port, source="CODEX_BRIDGE_UI_PORT")
            )
        return cls(port=port)
