from __future__ import annotations

from codex_bridge.console.main_window import MainWindow
from tests.test_console_main_window import FakeClient, FakeCodexProbe, _application, _config


def test_main_window_starts_in_conversation_mode() -> None:
    _application()
    window = MainWindow(
        _config(),
        api_client=FakeClient(),
        codex_probe=FakeCodexProbe(),
        tray_available=False,
    )
    try:
        assert window.history_pane.mode == "Conversation"
    finally:
        window.close()


def test_activity_toggle_preserves_pending_approval_and_restores_splitter_widths() -> None:
    application = _application()
    window = MainWindow(
        _config(),
        api_client=FakeClient(),
        codex_probe=FakeCodexProbe(),
        tray_available=False,
    )
    window.resize(1_440, 900)
    window.show()
    application.processEvents()
    activity = window.activity_pane
    history = window.history_pane
    history.set_mode("Details")
    activity.set_snapshot(
        {
            "state": "needs_approval",
            "pending_request": {
                "method": "item/commandExecution/requestApproval",
                "request_id": 31,
                "summary": "Run a test command",
            },
        }
    )
    application.processEvents()
    original_widths = window.splitter.sizes()
    center_width = original_widths[1]

    toggle = history.activity_toggle_button
    toggle.click()
    application.processEvents()

    assert window.activity_pane is activity
    assert activity.isHidden()
    assert activity.approval_request_id == 31
    assert toggle.text() == "Show Activity"
    assert "Approval required" in history.pending_notice_label.text()
    assert window.splitter.sizes()[1] > center_width
    assert history.mode == "Details"

    toggle.click()
    application.processEvents()

    assert not activity.isHidden()
    assert activity.approval_request_id == 31
    assert activity.approval_buttons_widget.isHidden() is False
    assert history.mode == "Details"
    restored_widths = window.splitter.sizes()
    assert abs(restored_widths[2] - original_widths[2]) <= 5, (
        f"original={original_widths!r} restored={restored_widths!r}"
    )
    activity.set_snapshot({"state": "ready"})
    toggle.click()
    application.processEvents()
    assert history.pending_notice_label.text() == ""
    assert toggle.accessibleName() == "Show Activity"
    window.close()
