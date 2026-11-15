from __future__ import annotations

from codex_bridge.console.codex_resolver import CodexResolution
from codex_bridge.console.main_window import MainWindow
from tests.console_main_window_test_support import FakeClient, FakeCodexProbe, _application, _config


def test_retry_now_reprobes_after_initial_codex_failure(monkeypatch) -> None:
    _application()
    original = FakeCodexProbe()
    retry = FakeCodexProbe()
    window = MainWindow(
        _config(), api_client=FakeClient(), codex_probe=original, tray_available=False
    )
    monkeypatch.setattr(window, "_new_codex_probe", lambda: retry)
    original.failure()
    assert window.codex_status_label.text() == "Codex: not found"

    window._retry_now()
    window._retry_now()
    original.result(CodexResolution("stale.exe", "0", "path"))
    retry.result(CodexResolution("fresh.exe", "1.2.3", "path"))

    assert retry.starts == 1
    assert window.codex_status_label.text() == "Codex: 1.2.3 · path"
    window.close()


def test_retry_now_during_initial_probe_does_not_activate_managed_retry() -> None:
    _application()
    probe = FakeCodexProbe()
    window = MainWindow(_config(), codex_probe=probe, tray_available=False)

    window._retry_now()

    assert not window._bridge_managed_retry_coordinator._managed_active
    window.close()
