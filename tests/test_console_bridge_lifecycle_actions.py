from __future__ import annotations

from codex_bridge.console import main_window as main_window_module
from codex_bridge.console.codex_resolver import CodexResolution
from codex_bridge.console.main_window import MainWindow
from tests.console_main_window_test_support import (
    FakeClient,
    FakeCodexProbe,
    FakeLauncher,
    _application,
    _config,
)


def test_bridge_lifecycle_token_generator_is_late_bound_and_lazy(monkeypatch) -> None:
    _application()
    generated_lengths: list[int] = []
    launcher = FakeLauncher()
    window = MainWindow(
        _config(),
        api_client=FakeClient(),
        codex_probe=FakeCodexProbe(),
        runtime_launcher=launcher,
        tray_available=False,
    )
    assert generated_lengths == []

    monkeypatch.setattr(
        main_window_module,
        "token_urlsafe",
        lambda length: generated_lengths.append(length) or "patched-control-token",
    )
    window._codex_resolution = CodexResolution("C:/Codex/codex.exe", "1.2.3", "path")
    window._health_observed = True
    window._status_observed = True
    window._runtime_state = "unavailable"

    window._start_bridge()

    assert generated_lengths == [32]
    assert launcher.control_tokens == ["patched-control-token"]
    assert window._bridge_lifecycle_controller is not None
    window.close()
