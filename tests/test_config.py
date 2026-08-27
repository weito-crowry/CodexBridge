from __future__ import annotations

import pytest

from codex_bridge.config import BridgeConfig, ConfigurationError


def test_empty_allowed_roots_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODEX_BRIDGE_ALLOWED_ROOTS", raising=False)

    config = BridgeConfig.from_env()

    assert config.allowed_roots == ()


def test_config_parses_host_origin_and_bounded_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_BRIDGE_ALLOWED_ROOTS", r"C:\work;D:\repo")
    monkeypatch.setenv(
        "CODEX_BRIDGE_ALLOWED_HOSTS", "bridge.example.com, bridge.example.com:*"
    )
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
