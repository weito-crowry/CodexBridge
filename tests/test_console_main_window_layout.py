from __future__ import annotations

from codex_bridge.console import main_window as main_window_module
from codex_bridge.console.main_window import MainWindow
from tests.test_console_main_window import (
    FakeClient,
    FakeCodexProbe,
    _application,
    _config,
)


def test_main_window_delegates_ui_construction_to_layout_builder(monkeypatch) -> None:
    _application()
    build_ui = getattr(main_window_module, "build_main_window_ui", None)
    assert callable(build_ui)
    calls: list[MainWindow] = []

    def track_build(window: MainWindow) -> None:
        calls.append(window)
        build_ui(window)

    monkeypatch.setattr(main_window_module, "build_main_window_ui", track_build)
    window = MainWindow(
        _config(),
        api_client=FakeClient(),
        codex_probe=FakeCodexProbe(),
        tray_available=False,
    )
    try:
        assert calls == [window]
        assert window.status_dialog.parent() is window
        assert window.splitter.count() == 3
        assert window.advanced_controls.isHidden()
    finally:
        window.close()
