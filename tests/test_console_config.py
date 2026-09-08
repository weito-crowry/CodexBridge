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


def test_cli_codex_executable_has_priority_over_environment() -> None:
    config = ConsoleConfig.from_sources(
        explicit_codex_executable="cli-codex",
        environ={"CODEX_BRIDGE_CODEX_EXECUTABLE": "env-codex"},
    )

    assert config.codex_executable == "cli-codex"
    assert config.codex_executable_source == "explicit"


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


def test_console_reads_user_config_and_reports_effective_roots(tmp_path, monkeypatch) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[bridge]\n"
        f'allowed_roots = ["{root.as_posix()}"]\n'
        'codex_executable = "configured-codex"\n'
        "[console]\nui_port = 8125\n"
        '[tunnel]\nexecutable = "configured-tunnel"\nprofile = "configured"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_BRIDGE_CONFIG", str(config_path))

    config = ConsoleConfig.from_sources(environ={"CODEX_BRIDGE_CONFIG": str(config_path)})

    assert config.port == 8125
    assert config.allowed_roots == (root.as_posix(),)
    assert config.codex_executable == "configured-codex"
    assert config.tunnel_executable == "configured-tunnel"
    assert config.tunnel_profile == "configured"
    assert config.roots_ready
    assert config.roots_count == 1


def test_console_environment_overrides_config_and_cli_overrides_environment(tmp_path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[bridge]\nallowed_roots = ["{root.as_posix()}"]\n[console]\nui_port = 8125\n',
        encoding="utf-8",
    )
    values = {
        "CODEX_BRIDGE_CONFIG": str(config_path),
        "CODEX_BRIDGE_UI_PORT": "8126",
        "CODEX_BRIDGE_ALLOWED_ROOTS": "env-root",
        "CODEX_BRIDGE_TUNNEL_PROFILE": "env-profile",
    }

    config = ConsoleConfig.from_sources(explicit_port=8127, environ=values)

    assert config.port == 8127
    assert config.allowed_roots == ("env-root",)
    assert config.tunnel_profile == "env-profile"


@pytest.mark.parametrize("value", ["", "bad profile", "a/b", "x" * 65, None, True])
def test_invalid_tunnel_profiles_are_rejected(value) -> None:
    with pytest.raises(ConsoleConfigurationError):
        parse_tunnel_profile(value)


@pytest.mark.parametrize("value", ["a", "codex-bridge", "a_b.c-1", "x" * 64])
def test_bounded_tunnel_profiles_are_accepted(value: str) -> None:
    assert parse_tunnel_profile(value) == value
