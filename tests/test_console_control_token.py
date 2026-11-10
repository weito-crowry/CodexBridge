from __future__ import annotations

import logging
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QAction
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel, QLineEdit, QMenu, QWidget

from codex_bridge.console.main_window import MainWindow
from tests.console_main_window_test_support import (
    FakeClient,
    FakeCodexProbe,
    FakeCodexUpdateProbe,
    StableTunnel,
    _application,
    _config,
)

_DUMMY_TOKEN = "dummy-control-token-for-tests"
_ROTATED_DUMMY_TOKEN = "rotated-dummy-control-token-for-tests"


@pytest.fixture
def clipboard():
    app = _application()
    target = app.clipboard()
    original = target.text()
    try:
        yield target
    finally:
        if original:
            target.setText(original)
        else:
            target.clear()


def _window(tmp_path: Path, language: str = "en") -> MainWindow:
    _application()
    settings = QSettings(str(tmp_path / "console.ini"), QSettings.Format.IniFormat)
    settings.setValue("language", language)
    window = MainWindow(
        _config(),
        api_client=FakeClient(),
        codex_probe=FakeCodexProbe(),
        codex_update_probe=FakeCodexUpdateProbe(),
        tunnel_supervisor=StableTunnel(),
        tray_available=False,
        appearance_settings=settings,
    )
    _set_ready(window)
    return window


def _set_ready(window: MainWindow) -> None:
    window._runtime_state = "console_started"
    window._bridge_ready = True
    window._app_server_ready = True
    window._bridge_transition = False
    window._detached_launch_started = True
    window._closing = False
    window._control_token = _DUMMY_TOKEN


def _developer_menu(window: MainWindow) -> QMenu:
    action = next(
        (
            item
            for item in window.settings_menu.actions()
            if item.text() in {"Developer", "開発者向け"} and item.menu() is not None
        ),
        None,
    )
    assert action is not None, "Developer submenu should be installed in Settings"
    menu = action.menu()
    assert menu is not None
    return menu


def _token_action(window: MainWindow) -> QAction:
    action = next(
        (
            item
            for item in _developer_menu(window).actions()
            if item.text() in {"Control API token…", "操作APIトークン…"}
        ),
        None,
    )
    assert action is not None, "Control API token action should be installed"
    return action


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("_runtime_state", "unavailable"),
        ("_runtime_state", "external"),
        ("_runtime_state", "stopping"),
        ("_bridge_ready", False),
        ("_app_server_ready", False),
        ("_bridge_transition", True),
        ("_control_token", ""),
        ("_control_token", None),
        ("_detached_launch_started", False),
        ("_closing", True),
    ],
)
def test_action_and_slot_reject_unavailable_or_external_bridge(
    tmp_path: Path, field: str, value: object
) -> None:
    window = _window(tmp_path)
    try:
        setattr(window, field, value)
        action = _token_action(window)
        action.setEnabled(True)
        _developer_menu(window).aboutToShow.emit()

        assert not action.isEnabled()
        show_dialog = getattr(window, "_show_control_token_dialog", None)
        assert callable(show_dialog), "the dialog slot must guard its own entry point"
        show_dialog()
        assert getattr(window, "_control_token_dialog", None) is None
    finally:
        window._exit_finished = True
        window.close()


@pytest.mark.parametrize(
    ("language", "developer_label", "action_label"),
    [
        ("en", "Developer", "Control API token…"),
        ("ja-JP", "開発者向け", "操作APIトークン…"),
    ],
)
def test_ready_console_enables_localized_menu_action(
    tmp_path: Path, language: str, developer_label: str, action_label: str
) -> None:
    window = _window(tmp_path, language)
    try:
        developer = next(
            item
            for item in window.settings_menu.actions()
            if item.text() in {"Developer", "開発者向け"} and item.menu() is not None
        )
        assert developer.text() == developer_label
        action = _token_action(window)
        assert action.text() == action_label

        action.setEnabled(False)
        _developer_menu(window).aboutToShow.emit()

        assert action.isEnabled()
    finally:
        window._exit_finished = True
        window.close()


@pytest.mark.parametrize(
    ("language", "warning"),
    [
        (
            "en",
            "This token grants Control API access. Do not share it with others. "
            "It may remain in clipboard history.",
        ),
        (
            "ja-JP",
            "このトークンは操作APIへの権限を持ちます。第三者に共有しないでください。"
            "クリップボード履歴に残ることがあります。",
        ),
    ],
)
def test_dialog_masks_token_and_toggles_visibility(
    tmp_path: Path, language: str, warning: str
) -> None:
    window = _window(tmp_path, language)
    try:
        action = _token_action(window)
        _developer_menu(window).aboutToShow.emit()
        action.trigger()

        dialog = getattr(window, "_control_token_dialog", None)
        assert dialog is not None and dialog.isVisible()
        assert dialog.windowTitle() != _DUMMY_TOKEN
        assert dialog.warning_label.text() == warning
        assert isinstance(dialog.token_edit, QLineEdit)
        assert dialog.token_edit.isReadOnly()
        assert dialog.token_edit.echoMode() == QLineEdit.EchoMode.Password
        assert dialog.token_edit.contextMenuPolicy() == Qt.ContextMenuPolicy.NoContextMenu
        assert dialog.token_edit.text() == _DUMMY_TOKEN
        assert all(_DUMMY_TOKEN not in label.text() for label in dialog.findChildren(QLabel))
        assert all(_DUMMY_TOKEN not in widget.toolTip() for widget in dialog.findChildren(QWidget))

        dialog.show_button.click()
        assert dialog.token_edit.echoMode() == QLineEdit.EchoMode.Normal
        assert dialog.show_button.text() in {"Hide", "隠す"}
        dialog.show_button.click()
        assert dialog.token_edit.echoMode() == QLineEdit.EchoMode.Password
        assert dialog.show_button.text() in {"Show", "表示"}
    finally:
        window._exit_finished = True
        window.close()


def test_copy_uses_only_user_action_and_close_clears_without_logging_or_saving(
    tmp_path: Path, clipboard, caplog: pytest.LogCaptureFixture
) -> None:
    window = _window(tmp_path)
    settings = window._appearance_settings
    settings.sync()
    try:
        clipboard.setText("preexisting clipboard text")
        action = _token_action(window)
        _developer_menu(window).aboutToShow.emit()
        action.trigger()
        dialog = window._control_token_dialog
        assert dialog is not None
        assert clipboard.text() == "preexisting clipboard text"

        with caplog.at_level(logging.DEBUG):
            dialog.copy_button.click()

        assert clipboard.text() == _DUMMY_TOKEN
        assert dialog.copy_button.text() == "Copied"
        assert _DUMMY_TOKEN not in caplog.text
        settings.sync()
        assert all("token" not in key.casefold() for key in settings.allKeys())
        assert all(settings.value(key) != _DUMMY_TOKEN for key in settings.allKeys())
        assert _DUMMY_TOKEN not in dialog.windowTitle()

        dialog.close_button.click()
        assert dialog.token_edit.text() == ""
        assert not dialog.copy_button.isEnabled()
        assert not dialog.show_button.isEnabled()
    finally:
        window._exit_finished = True
        window.close()


@pytest.mark.parametrize("invalidate_by", ["rotation", "transition"])
def test_open_dialog_cannot_copy_after_token_is_replaced_or_bridge_stops(
    tmp_path: Path, clipboard, invalidate_by: str
) -> None:
    window = _window(tmp_path)
    try:
        clipboard.setText("preexisting clipboard text")
        action = _token_action(window)
        _developer_menu(window).aboutToShow.emit()
        action.trigger()
        dialog = window._control_token_dialog
        assert dialog is not None

        if invalidate_by == "rotation":
            window._control_token = _ROTATED_DUMMY_TOKEN
        else:
            window._bridge_transition = True
            window._runtime_state = "restarting"

        dialog.copy_button.click()

        assert clipboard.text() == "preexisting clipboard text"
        assert dialog.token_edit.text() == ""
        assert not dialog.copy_button.isEnabled()
        QTest.qWait(300)
        assert dialog.token_edit.text() == ""
    finally:
        window._exit_finished = True
        window.close()


def test_open_dialog_timer_clears_token_after_bridge_becomes_unavailable(
    tmp_path: Path, clipboard
) -> None:
    window = _window(tmp_path)
    try:
        clipboard.setText("preexisting clipboard text")
        action = _token_action(window)
        _developer_menu(window).aboutToShow.emit()
        action.trigger()
        dialog = window._control_token_dialog
        assert dialog is not None

        window._runtime_state = "stopping"
        window._bridge_transition = True
        QTest.qWait(300)

        assert dialog.token_edit.text() == ""
        assert not dialog.copy_button.isEnabled()
        assert not dialog.show_button.isEnabled()
        assert clipboard.text() == "preexisting clipboard text"
        dialog._copy_token()
        assert clipboard.text() == "preexisting clipboard text"
    finally:
        window._exit_finished = True
        window.close()


def test_keyboard_copy_rechecks_token_before_copying(tmp_path: Path, clipboard) -> None:
    window = _window(tmp_path)
    try:
        clipboard.setText("preexisting clipboard text")
        action = _token_action(window)
        _developer_menu(window).aboutToShow.emit()
        action.trigger()
        dialog = window._control_token_dialog
        assert dialog is not None
        dialog.show_button.click()
        dialog.token_edit.selectAll()
        dialog._validation_timer.stop()
        window._control_token = _ROTATED_DUMMY_TOKEN

        QTest.keyClick(
            dialog.token_edit,
            Qt.Key.Key_C,
            Qt.KeyboardModifier.ControlModifier,
        )

        assert clipboard.text() == "preexisting clipboard text"
        assert dialog.token_edit.text() == ""
    finally:
        window._exit_finished = True
        window.close()
