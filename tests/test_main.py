from __future__ import annotations

import pytest

import codex_bridge.__main__ as cli


def _stub_configuration(monkeypatch) -> None:
    monkeypatch.setattr(cli.BridgeConfig, "from_sources", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "configure_logging", lambda: None, raising=False)


def test_main_treats_keyboard_interrupt_as_normal_shutdown(monkeypatch) -> None:
    _stub_configuration(monkeypatch)

    async def interrupt(_config) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run_server", interrupt)

    assert cli.main([]) == 0


def test_main_preserves_unexpected_server_exception(monkeypatch) -> None:
    _stub_configuration(monkeypatch)

    async def fail(_config) -> None:
        raise RuntimeError("unexpected server failure")

    monkeypatch.setattr(cli, "run_server", fail)

    with pytest.raises(RuntimeError, match="unexpected server failure"):
        cli.main([])
