from __future__ import annotations

import pytest

from codex_bridge.config import BridgeConfig, ConfigurationError


def test_empty_allowed_roots_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODEX_BRIDGE_ALLOWED_ROOTS", raising=False)

    config = BridgeConfig.from_env()

    assert config.allowed_roots == ()


def test_config_parses_host_origin_and_bounded_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_BRIDGE_ALLOWED_ROOTS", r"C:\work;D:\repo")
    monkeypatch.setenv("CODEX_BRIDGE_ALLOWED_HOSTS", "bridge.example.com, bridge.example.com:*")
    monkeypatch.setenv("CODEX_BRIDGE_ALLOWED_ORIGINS", "https://chat.example.com")
    monkeypatch.setenv("CODEX_BRIDGE_WAIT_MAX_SECONDS", "29")

    config = BridgeConfig.from_env()

    assert config.allowed_roots == (r"C:\work", r"D:\repo")
    assert config.allowed_hosts == ("bridge.example.com", "bridge.example.com:*")
    assert config.allowed_origins == ("https://chat.example.com",)
    assert config.wait_max_seconds == 29.0


def test_default_wait_cannot_exceed_maximum(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_BRIDGE_WAIT_DEFAULT_SECONDS", "31")
    monkeypatch.setenv("CODEX_BRIDGE_WAIT_MAX_SECONDS", "30")

    with pytest.raises(ConfigurationError, match="default wait"):
        BridgeConfig.from_env()


def test_ui_port_defaults_to_8001(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODEX_BRIDGE_UI_PORT", raising=False)

    config = BridgeConfig.from_env()

    assert config.ui_port == 8001


def test_control_token_is_unset_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODEX_BRIDGE_CONTROL_TOKEN", raising=False)

    config = BridgeConfig.from_env()

    assert config.control_token is None


def test_control_token_accepts_bounded_url_safe_value(monkeypatch: pytest.MonkeyPatch) -> None:
    token = "A_b-9" * 7
    monkeypatch.setenv("CODEX_BRIDGE_CONTROL_TOKEN", token)

    config = BridgeConfig.from_env()

    assert config.control_token == token


@pytest.mark.parametrize(
    "token",
    ["too-short", "A" * 257, "A" * 31 + "."],
)
def test_control_token_rejects_invalid_values_without_echoing_secret(
    monkeypatch: pytest.MonkeyPatch, token: str
) -> None:
    monkeypatch.setenv("CODEX_BRIDGE_CONTROL_TOKEN", token)

    with pytest.raises(ConfigurationError) as exc_info:
        BridgeConfig.from_env()

    assert token not in str(exc_info.value)


def test_ui_port_is_parsed_and_must_differ_from_mcp_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_BRIDGE_PORT", "8100")
    monkeypatch.setenv("CODEX_BRIDGE_UI_PORT", "8101")

    config = BridgeConfig.from_env()

    assert config.ui_port == 8101

    monkeypatch.setenv("CODEX_BRIDGE_UI_PORT", "8100")
    with pytest.raises(ConfigurationError, match="must differ"):
        BridgeConfig.from_env()


@pytest.mark.parametrize("value", ["0", "65536", "not-a-port"])
def test_ui_port_rejects_invalid_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("CODEX_BRIDGE_UI_PORT", value)

    with pytest.raises(ConfigurationError, match="CODEX_BRIDGE_UI_PORT"):
        BridgeConfig.from_env()


def test_user_config_file_supplies_bridge_values(tmp_path, monkeypatch) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[bridge]\n"
        f'allowed_roots = ["{root.as_posix()}"]\n'
        'codex_executable = "configured-codex"\n'
        "[console]\nui_port = 8123\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_BRIDGE_CONFIG", str(config_path))
    monkeypatch.delenv("CODEX_BRIDGE_ALLOWED_ROOTS", raising=False)
    monkeypatch.delenv("CODEX_BRIDGE_CODEX_EXECUTABLE", raising=False)
    monkeypatch.delenv("CODEX_BRIDGE_UI_PORT", raising=False)

    config = BridgeConfig.from_env()

    assert config.allowed_roots == (root.as_posix(),)
    assert config.codex_executable == "configured-codex"
    assert config.ui_port == 8123


def test_config_precedence_is_explicit_then_environment_then_file_then_default(
    tmp_path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[bridge]\nallowed_roots = ["file-root"]\n'
        'codex_executable = "file-codex"\n'
        "[console]\nui_port = 8101\n",
        encoding="utf-8",
    )
    values = {
        "CODEX_BRIDGE_CONFIG": str(config_path),
        "CODEX_BRIDGE_ALLOWED_ROOTS": "env-root",
        "CODEX_BRIDGE_CODEX_EXECUTABLE": "env-codex",
        "CODEX_BRIDGE_UI_PORT": "8102",
    }

    config = BridgeConfig.from_sources(
        explicit_allowed_roots=("cli-root",),
        explicit_codex_executable="cli-codex",
        explicit_ui_port=8103,
        environ=values,
    )

    assert config.allowed_roots == ("cli-root",)
    assert config.codex_executable == "cli-codex"
    assert config.ui_port == 8103


def test_malformed_or_wrong_type_config_fails_fast(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("[bridge\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_BRIDGE_CONFIG", str(config_path))
    with pytest.raises(ConfigurationError, match="TOML"):
        BridgeConfig.from_env()

    config_path.write_text('[bridge]\nallowed_roots = "one-root"\n', encoding="utf-8")
    with pytest.raises(ConfigurationError, match="allowed_roots"):
        BridgeConfig.from_env()


def test_config_does_not_accept_secret_settings(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('[tunnel]\ncontrol_token = "secret"\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_BRIDGE_CONFIG", str(config_path))

    with pytest.raises(ConfigurationError, match="secret"):
        BridgeConfig.from_env()


def test_empty_and_invalid_allowed_roots_fail_preflight(tmp_path) -> None:
    from codex_bridge import config as config_module

    validate_allowed_roots = config_module.validate_allowed_roots
    with pytest.raises(ConfigurationError, match="allowed root"):
        validate_allowed_roots(())
    with pytest.raises(ConfigurationError, match="absolute"):
        validate_allowed_roots(("relative",))
    with pytest.raises(ConfigurationError, match="existing"):
        validate_allowed_roots((str(tmp_path / "missing"),))

    root = tmp_path / "root"
    root.mkdir()
    assert validate_allowed_roots((str(root),)) == (str(root.resolve()).casefold(),)
