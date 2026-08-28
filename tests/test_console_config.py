from __future__ import annotations

import pytest

from codex_bridge.console.config import (
    ConsoleConfig,
    ConsoleConfigurationError,
    parse_tunnel_profile,
    parse_ui_port,
)


def test_console_config_defaults_to_fixed_loopback_and_8001(monkeypatch) -> None:
    monkeypatch.delenv("CODEX_BRIDGE_UI_PORT", raising=False)

    config = ConsoleConfig.from_sources(environ={})

    assert config.host == "127.0.0.1"
    assert config.port == 8001
    assert config.base_url == "http://127.0.0.1:8001"


def test_environment_port_is_used_when_cli_port_is_absent() -> None:
    config = ConsoleConfig.from_sources(environ={"CODEX_BRIDGE_UI_PORT": "8123"})

    assert config.port == 8123


def test_cli_port_has_priority_over_environment() -> None:
    config = ConsoleConfig.from_sources(
        explicit_port=8124,
        environ={"CODEX_BRIDGE_UI_PORT": "8123"},
    )

    assert config.port == 8124


@pytest.mark.parametrize("value", [0, -1, 65536, "not-a-port", True, None])
def test_invalid_console_ports_are_rejected(value) -> None:
    with pytest.raises(ConsoleConfigurationError):
        parse_ui_port(value)


@pytest.mark.parametrize("value", [1, 8001, 65535])
def test_boundary_console_ports_are_accepted(value: int) -> None:
    assert parse_ui_port(value) == value


def test_console_config_defaults_to_codex_bridge_tunnel_profile() -> None:
    config = ConsoleConfig.from_sources(environ={})

    assert config.tunnel_profile == "codex-bridge"
    assert config.tunnel_executable is None


def test_tunnel_environment_overrides_are_read_without_secret_fields() -> None:
    config = ConsoleConfig.from_sources(
        environ={
            "CODEX_BRIDGE_TUNNEL_EXECUTABLE": "C:/tools/tunnel-client.exe",
            "CODEX_BRIDGE_TUNNEL_PROFILE": "work.profile-1",
            "CONTROL_PLANE_API_KEY": "must-not-be-read-by-config",
        }
    )

    assert config.tunnel_executable == "C:/tools/tunnel-client.exe"
    assert config.tunnel_profile == "work.profile-1"


@pytest.mark.parametrize("value", ["", "bad profile", "a/b", "x" * 65, None, True])
def test_invalid_tunnel_profiles_are_rejected(value) -> None:
    with pytest.raises(ConsoleConfigurationError):
        parse_tunnel_profile(value)


@pytest.mark.parametrize("value", ["a", "codex-bridge", "a_b.c-1", "x" * 64])
def test_bounded_tunnel_profiles_are_accepted(value: str) -> None:
    assert parse_tunnel_profile(value) == value
