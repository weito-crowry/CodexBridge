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
    monkeypatch.setenv("CODEX_BRIDGE_WAIT_DEFAULT_SECONDS", "20")
    monkeypatch.setenv("CODEX_BRIDGE_WAIT_MAX_SECONDS", "29")

    config = BridgeConfig.from_env()

    assert config.allowed_roots == (r"C:\work", r"D:\repo")
    assert config.allowed_hosts == ("bridge.example.com", "bridge.example.com:*")
    assert config.allowed_origins == ("https://chat.example.com",)
    assert config.wait_max_seconds == 29.0


def test_wait_defaults_match_operational_long_poll_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CODEX_BRIDGE_WAIT_DEFAULT_SECONDS", raising=False)
    monkeypatch.delenv("CODEX_BRIDGE_WAIT_MAX_SECONDS", raising=False)

    config = BridgeConfig.from_env()

    assert config.wait_default_seconds == 50.0
    assert config.wait_max_seconds == 55.0


def test_default_wait_cannot_exceed_maximum(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_BRIDGE_WAIT_DEFAULT_SECONDS", "31")
    monkeypatch.setenv("CODEX_BRIDGE_WAIT_MAX_SECONDS", "30")

    with pytest.raises(ConfigurationError, match="default wait"):
        BridgeConfig.from_env()


def test_wait_maximum_cannot_exceed_hard_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_BRIDGE_WAIT_MAX_SECONDS", "56")

    with pytest.raises(ConfigurationError, match="55 seconds"):
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


def test_user_config_file_supplies_non_secret_github_mcp_values(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[github_mcp]\n"
        "enabled = false\n"
        'url = "https://example.test/mcp"\n'
        'prefix = "gh_"\n'
        'include = ["get_*"]\n'
        'exclude = ["*_secret"]\n'
        'toolsets = [" context ", "repos "]\n'
        "max_tools = 25\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_BRIDGE_CONFIG", str(config_path))

    config = BridgeConfig.from_env()

    assert config.github_mcp.enabled is False
    assert config.github_mcp.url == "https://example.test/mcp"
    assert config.github_mcp.prefix == "gh_"
    assert config.github_mcp.include == ("get_*",)
    assert config.github_mcp.exclude == ("*_secret",)
    assert config.github_mcp.toolsets == ("context", "repos")
    assert config.github_mcp.max_tools == 25


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


def test_github_mcp_is_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "CODEX_BRIDGE_GITHUB_MCP_ENABLED",
        "CODEX_BRIDGE_GITHUB_MCP_URL",
        "CODEX_BRIDGE_GITHUB_MCP_PREFIX",
        "CODEX_BRIDGE_GITHUB_MCP_INCLUDE",
        "CODEX_BRIDGE_GITHUB_MCP_EXCLUDE",
        "CODEX_BRIDGE_GITHUB_MCP_TOOLSETS",
        "CODEX_BRIDGE_GITHUB_MCP_MAX_TOOLS",
        "CODEX_BRIDGE_GITHUB_PAT",
    ):
        monkeypatch.delenv(name, raising=False)

    config = BridgeConfig.from_env()

    assert config.github_mcp.enabled is False
    assert config.github_mcp.url == "https://api.githubcopilot.com/mcp/x/all"
    assert config.github_mcp.prefix == "github_"
    assert config.github_mcp.include == ("*",)
    assert config.github_mcp.exclude == ()
    assert config.github_mcp.toolsets == ()
    assert config.github_mcp.max_tools == 0


def test_github_mcp_enabled_requires_dedicated_pat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_BRIDGE_GITHUB_MCP_ENABLED", "true")
    monkeypatch.delenv("CODEX_BRIDGE_GITHUB_PAT", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-be-used")

    with pytest.raises(ConfigurationError, match="CODEX_BRIDGE_GITHUB_PAT"):
        BridgeConfig.from_env()


@pytest.mark.parametrize("value", ["-1", "not-an-integer"])
def test_github_mcp_rejects_invalid_max_tools(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("CODEX_BRIDGE_GITHUB_MCP_MAX_TOOLS", value)

    with pytest.raises(ConfigurationError, match="MAX_TOOLS"):
        BridgeConfig.from_env()


def test_github_mcp_reads_all_supported_environment_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_BRIDGE_GITHUB_MCP_ENABLED", "on")
    monkeypatch.setenv("CODEX_BRIDGE_GITHUB_MCP_URL", "https://example.test/mcp")
    monkeypatch.setenv("CODEX_BRIDGE_GITHUB_MCP_PREFIX", "gh_")
    monkeypatch.setenv("CODEX_BRIDGE_GITHUB_MCP_INCLUDE", "get_*, list_*")
    monkeypatch.setenv("CODEX_BRIDGE_GITHUB_MCP_EXCLUDE", "*_secret")
    monkeypatch.setenv(
        "CODEX_BRIDGE_GITHUB_MCP_TOOLSETS",
        "context,repos,issues,pull_requests,actions",
    )
    monkeypatch.setenv("CODEX_BRIDGE_GITHUB_MCP_MAX_TOOLS", "25")
    monkeypatch.setenv("CODEX_BRIDGE_GITHUB_PAT", "do-not-echo")

    config = BridgeConfig.from_env()

    assert config.github_mcp.enabled is True
    assert config.github_mcp.url == "https://example.test/mcp"
    assert config.github_mcp.prefix == "gh_"
    assert config.github_mcp.include == ("get_*", "list_*")
    assert config.github_mcp.exclude == ("*_secret",)
    assert config.github_mcp.toolsets == (
        "context",
        "repos",
        "issues",
        "pull_requests",
        "actions",
    )
    assert config.github_mcp.max_tools == 25
    assert "do-not-echo" not in repr(config)


def test_github_mcp_trims_toolsets_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_BRIDGE_GITHUB_MCP_TOOLSETS", "context, repos , issues")

    config = BridgeConfig.from_env()

    assert config.github_mcp.toolsets == ("context", "repos", "issues")


def test_github_mcp_empty_toolsets_environment_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_BRIDGE_GITHUB_MCP_TOOLSETS", "")

    config = BridgeConfig.from_env()

    assert config.github_mcp.toolsets == ()


def test_github_mcp_environment_toolsets_override_config_file(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[github_mcp]\ntoolsets = ["context", "repos"]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_BRIDGE_CONFIG", str(config_path))
    monkeypatch.setenv("CODEX_BRIDGE_GITHUB_MCP_TOOLSETS", "issues, pull_requests")

    config = BridgeConfig.from_env()

    assert config.github_mcp.toolsets == ("issues", "pull_requests")


@pytest.mark.parametrize("value", ["context,,repos", "context, ,repos"])
def test_github_mcp_rejects_empty_toolset_names(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("CODEX_BRIDGE_GITHUB_MCP_TOOLSETS", value)

    with pytest.raises(ConfigurationError, match="toolset"):
        BridgeConfig.from_env()


@pytest.mark.parametrize("value", ["get_[", "get_]", "get_*,,list_*"])
def test_github_mcp_rejects_malformed_glob_patterns(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("CODEX_BRIDGE_GITHUB_MCP_INCLUDE", value)

    with pytest.raises(ConfigurationError, match="pattern"):
        BridgeConfig.from_env()


@pytest.mark.parametrize("url", ["http://[invalid", "https://:443/mcp", "https://host:bad/mcp"])
def test_github_mcp_rejects_malformed_url(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setenv("CODEX_BRIDGE_GITHUB_MCP_URL", url)

    with pytest.raises(ConfigurationError, match=r"HTTP\(S\) URL"):
        BridgeConfig.from_env()
