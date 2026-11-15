from __future__ import annotations

from PySide6.QtWidgets import QMenuBar

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
        assert isinstance(window.menuBar(), QMenuBar)
        assert [action.text() for action in window.menuBar().actions()] == [
            "File",
            "View",
            "Settings",
        ]
        assert window.workflow_management_action in window.view_menu.actions()
        assert window.workflow_management_action.text() in {"Workflows…", "ワークフロー…"}
        window.workflow_management_action.trigger()
        workflow_dialog = window._workflow_management_controller.dialog
        assert workflow_dialog.isVisible()
        assert workflow_dialog.bridge_state_label.text() in {
            "Bridge unavailable",
            "Bridgeを利用できません",
        }
        window._show_workflow_management_dialog()
        assert window._workflow_management_controller.dialog is workflow_dialog
        assert window.splitter.count() == 3
        assert window.advanced_controls.isHidden()
    finally:
        window._workflow_management_controller.shutdown()
        window.close()
