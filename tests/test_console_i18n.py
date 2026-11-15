from pathlib import Path

import pytest
from PySide6.QtCore import QSettings


def test_language_resolution_and_invalid_fallback():
    from codex_bridge.console.i18n import resolve_language

    assert resolve_language("system", "ja_JP") == "ja-JP"
    assert resolve_language("system", "ja") == "ja-JP"
    assert resolve_language("system", "fr_FR") == "en"
    assert resolve_language("ja-JP", "en_US") == "ja-JP"
    assert resolve_language("en", "ja_JP") == "en"
    for invalid in ("fr", "", None, 9):
        assert resolve_language(invalid, "ja_JP") == "en"


def test_language_persistence_is_restart_only(tmp_path: Path):
    from codex_bridge.console.i18n import initialize_language, language, save_language, tr

    settings = QSettings(str(tmp_path / "console.ini"), QSettings.Format.IniFormat)
    initialize_language(settings, system_locale="ja_JP")
    assert language() == "ja-JP"
    assert not settings.contains("language")
    assert tr("Settings") == "設定"
    save_language(settings, "en")
    assert language() == "ja-JP"
    restored = QSettings(str(tmp_path / "console.ini"), QSettings.Format.IniFormat)
    initialize_language(restored, system_locale="ja_JP")
    assert language() == "en"
    assert tr("Settings") == "Settings"


def test_translation_interpolates_data_after_lookup(tmp_path: Path):
    from codex_bridge.console.i18n import initialize_language, tr

    settings = QSettings(str(tmp_path / "console.ini"), QSettings.Format.IniFormat)
    settings.setValue("language", "ja-JP")
    initialize_language(settings)
    data = "Settings {p0} 日本語 C:/User/Send"
    assert tr("Thread ID: {p0}", p0=data) == "スレッドID: " + data
    assert tr("Uncatalogued {value}", value=data) == "Uncatalogued " + data


@pytest.mark.parametrize("selected", ["ja-JP", "en"])
@pytest.mark.parametrize("theme", ["Light", "Dark"])
def test_major_qt_surfaces_and_raw_data(tmp_path: Path, selected: str, theme: str):
    from PySide6.QtWidgets import QApplication, QMessageBox

    from codex_bridge.console.i18n import initialize_language, tr
    from codex_bridge.console.main_window import MainWindow
    from codex_bridge.console.notification_controller import NotificationSettingsDialog
    from codex_bridge.console.usage_history_window import UsageHistoryWindow
    from tests.console_main_window_test_support import (
        FakeClient,
        FakeCodexProbe,
        FakeCodexUpdateProbe,
        StableTunnel,
        _application,
        _config,
    )

    _application()
    settings = QSettings(str(tmp_path / "console.ini"), QSettings.Format.IniFormat)
    settings.setValue("language", selected)
    settings.setValue("appearance/mode", theme)
    initialize_language(settings)
    window = MainWindow(
        _config(),
        api_client=FakeClient(),
        codex_probe=FakeCodexProbe(),
        codex_update_probe=FakeCodexUpdateProbe(),
        tunnel_supervisor=StableTunnel(),
        tray_available=False,
        appearance_settings=settings,
    )
    history = UsageHistoryWindow(tmp_path / "usage.sqlite3", settings=settings)
    notifications = NotificationSettingsDialog(settings)
    try:
        assert [action.text() for action in window.menuBar().actions()] == (
            ["ファイル", "表示", "設定"] if selected == "ja-JP" else ["File", "View", "Settings"]
        )
        assert window.status_button.text() == ("状態" if selected == "ja-JP" else "Status")
        assert window._settings_actions["Open settings file"].text() == tr("Open settings file")
        assert window.composer_widget.send_button.text() == (
            "送信" if selected == "ja-JP" else "Send"
        )
        assert window.history_pane.messages_mode_button.text() == tr("Messages")
        assert window.thread_pane.refresh_button.text() == tr("Refresh")
        assert history.windowTitle() == tr("Usage History · CodexBridge Console")
        assert notifications.windowTitle() == tr("Notification settings")
        language_menu = next(
            a.menu() for a in window.settings_menu.actions() if a.menu() and "Language" in a.text()
        )
        assert language_menu is not None
        assert "system" in [a.data() for a in language_menu.actions()]
        language_menu.actions()[-1].trigger()
        assert window.status_button.text() == tr("Status")  # no mixed live UI
        assert settings.value("language") == "en"
        raw = "Settings / Send / English / 日本語 / {p0}"
        window.composer_widget.input.setPlainText(raw)
        assert window.composer_widget.input.toPlainText() == raw
        window.history_pane.set_mode("Messages")
        assert window.history_pane.mode == "Messages"
        window.resize(760, 600)
        window.composer_widget.resize(350, 250)
        QApplication.processEvents()
        assert window.composer_widget.minimumWidth() == 0
        box = QMessageBox(
            QMessageBox.Icon.Question,
            tr("Settings"),
            raw,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        assert box.text() == raw
        assert box.button(QMessageBox.StandardButton.Yes).text() == (
            "はい(&Y)" if selected == "ja-JP" else "&Yes"
        )
    finally:
        notifications.close()
        history.close()
        window._exit_finished = True
        window.close()


def test_catalog_placeholders_and_explicit_call_coverage():
    import ast
    import json
    from string import Formatter

    import codex_bridge.console.i18n as i18n

    root = Path(i18n.__file__).parent
    catalog = json.loads((root / "locales" / "ja-JP.json").read_text(encoding="utf-8"))

    def fields(text):
        return sorted(
            (name, spec, conversion)
            for _, name, spec, conversion in Formatter().parse(text)
            if name is not None
        )

    for source, translation in catalog.items():
        assert fields(source) == fields(translation), source
        assert translation, source
    missing = []
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "tr"
            ):
                assert not isinstance(node.args[0], ast.JoinedStr), (path.name, node.lineno)
                if isinstance(node.args[0], ast.Constant) and node.args[0].value not in catalog:
                    missing.append((path.name, node.lineno, node.args[0].value))
    assert missing == []


def test_configuration_error_guidance_preserves_source_name(tmp_path: Path):
    from codex_bridge.console.configuration_error_presentation import format_configuration_error
    from codex_bridge.console.i18n import initialize_language

    settings = QSettings(str(tmp_path / "console.ini"), QSettings.Format.IniFormat)
    settings.setValue("language", "ja-JP")
    initialize_language(settings)
    assert (
        format_configuration_error("console.port must be an integer between 1 and 65535")
        == "console.portは1～65535の整数にしてください"
    )
    assert format_configuration_error("raw {p0} file Error") == "raw {p0} file Error"
    assert (
        format_configuration_error("allowed roots must be existing directories")
        == "許可フォルダーには存在するフォルダーを指定してください"
    )


def test_ui_sinks_use_explicit_translation_for_static_text():
    import ast
    import re

    import codex_bridge.console.i18n as i18n

    constructors = {"QLabel", "QPushButton", "QCheckBox", "QGroupBox", "QAction"}
    setters = {
        "setText",
        "setWindowTitle",
        "setToolTip",
        "setAccessibleName",
        "setPlaceholderText",
        "addMenu",
        "addAction",
        "addItem",
    }
    brand_names = {"CodexBridge Console", "CodexBridge", "Console"}
    missing = []
    for path in Path(i18n.__file__).parent.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            ui_call = (isinstance(node.func, ast.Name) and node.func.id in constructors) or (
                isinstance(node.func, ast.Attribute) and node.func.attr in setters
            )
            if ui_call and isinstance(node.args[0], ast.Constant):
                value = node.args[0].value
                if (
                    isinstance(value, str)
                    and re.search(r"[A-Za-zぁ-龠]", value)
                    and value not in brand_names
                ):
                    missing.append((path.name, node.lineno, value))
    assert missing == []


@pytest.mark.parametrize(
    "item_type, kind, title",
    [("userMessage", "User", "ユーザー"), ("agentMessage", "Agent", "エージェント")],
)
def test_japanese_thread_history_process_and_notification_data(
    tmp_path: Path, item_type, kind, title
):
    from PySide6.QtCore import Qt

    from codex_bridge.console.i18n import initialize_language, tr
    from codex_bridge.console.notification_controller import NotificationController
    from codex_bridge.console.process_monitor import ProcessMonitorWidget
    from codex_bridge.console.widgets import ThreadListPane, timeline_entries
    from tests.console_main_window_test_support import _application

    _application()
    settings = QSettings(str(tmp_path / "ja.ini"), QSettings.Format.IniFormat)
    settings.setValue("language", "ja-JP")
    initialize_language(settings)
    raw = "Settings Send 日本語 {p0}"
    pane = ThreadListPane()
    pane.set_threads(
        [{"id": "raw-id", "name": raw, "cwd": str(tmp_path)}],
        thread_statuses={"raw-id": "in_progress"},
    )
    running = pane.list_widget.topLevelItem(0)
    assert running.text(0) == "現在処理中"
    assert running.child(0).child(0).data(0, Qt.ItemDataRole.UserRole) == "raw-id"
    assert raw in running.child(0).child(0).text(0)
    entries = timeline_entries(
        {"items": [{"turn_id": "t", "item": {"id": "u", "type": item_type, "text": raw}}]}
    )
    assert entries[0].kind == kind
    assert entries[0].title == title
    assert entries[0].body == raw
    monitor = ProcessMonitorWidget(platform_name="linux")
    assert monitor.toggle_button.text() == "プロセス（読み取り専用）"
    assert monitor.process_text.toPlainText() == "Windowsのみ対応"
    messages = []
    controller = NotificationController(
        settings,
        show_message=lambda title, body: messages.append((title, body)),
        is_window_active=lambda: False,
        activate_console=lambda: None,
        play_sound=lambda: None,
    )
    assert controller.handle_activity(
        {
            "thread_id": "t",
            "turn_id": "turn",
            "type": "turn_completed",
            "status": "completed",
            "details": {},
            "summary": raw,
        }
    )
    assert messages == [(tr("Turn completed"), raw)]
    monitor.shutdown()
    monitor.close()
    pane.close()


def test_dictionary_machine_keys_remain_untranslated():
    import ast

    import codex_bridge.console.i18n as i18n

    translated_keys = []
    for path in Path(i18n.__file__).parent.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Dict):
                translated_keys.extend(
                    (path.name, key.lineno)
                    for key in node.keys
                    if isinstance(key, ast.Call)
                    and isinstance(key.func, ast.Name)
                    and key.func.id == "tr"
                )
    assert translated_keys == []


@pytest.mark.parametrize("selected", ["ja-JP", "en"])
@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("width", [240, 360, 720])
def test_translated_composer_controls_fit_narrow_layout(tmp_path: Path, selected, scheme, width):
    from PySide6.QtCore import QPoint

    from codex_bridge.console.appearance import stylesheet_for_theme, theme_for_scheme
    from codex_bridge.console.composer_widget import ComposerWidget
    from codex_bridge.console.i18n import initialize_language
    from tests.console_main_window_test_support import _application

    app = _application()
    settings = QSettings(str(tmp_path / "layout.ini"), QSettings.Format.IniFormat)
    settings.setValue("language", selected)
    initialize_language(settings)
    widget = ComposerWidget()
    widget.setStyleSheet(stylesheet_for_theme(theme_for_scheme(scheme)))
    widget.resize(width, 480)
    widget.set_attachments(["D:/raw/Settings 日本語 document.txt"])
    widget.show()
    try:
        for _ in range(3):
            app.processEvents()
        for control in (
            widget.send_button,
            widget.access_combo,
            widget.model_combo,
            widget.effort_combo,
        ):
            position = control.mapTo(widget, QPoint(0, 0))
            assert position.x() >= 0
            assert position.x() + control.width() <= widget.width()
        assert (
            widget.attachment_bar._chips[0]
            .name_label.toolTip()
            .endswith("Settings 日本語 document.txt")
        )
        assert widget.attachment_bar.scroll_area.horizontalScrollBar().maximum() == 0
    finally:
        widget.close()


@pytest.mark.parametrize("selected, expected", [("ja-JP", "ファイル名"), ("en", "File name")])
def test_qt_file_dialog_labels_follow_console_language(tmp_path, selected, expected):
    from PySide6.QtWidgets import QFileDialog, QLabel

    from codex_bridge.console.i18n import initialize_language
    from tests.console_main_window_test_support import _application

    _application()
    settings = QSettings(str(tmp_path / "dialog.ini"), QSettings.Format.IniFormat)
    settings.setValue("language", selected)
    initialize_language(settings)
    dialog = QFileDialog(options=QFileDialog.Option.DontUseNativeDialog)
    try:
        assert any(
            expected in label.text().replace("&", "") for label in dialog.findChildren(QLabel)
        )
    finally:
        dialog.close()


def test_display_maps_and_configuration_templates_have_catalog_entries():
    import ast
    import json

    import codex_bridge.console.i18n as i18n
    from codex_bridge.console.api_error_presentation import _LOCAL_API_MESSAGES
    from codex_bridge.console.configuration_error_presentation import _FIXED, _SUFFIXES

    root = Path(i18n.__file__).parent
    catalog = json.loads((root / "locales" / "ja-JP.json").read_text(encoding="utf-8"))
    for filename, name in [
        ("runtime_ui.py", "_RUNTIME_LABELS"),
        ("tunnel_supervisor.py", "_STATE_LABELS"),
        ("conversation_widgets.py", "_STATUS_LABELS"),
        ("usage_forecast_presentation.py", "_REASON_TEXT"),
    ]:
        tree = ast.parse((root / filename).read_text(encoding="utf-8"))
        mapping = next(
            node.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == name for target in node.targets)
        )
        for value in mapping.values:
            assert value.value in catalog, (filename, value.value)
    assert all("{p0}" + suffix in catalog for suffix in _SUFFIXES)
    assert all(message in catalog for message in _FIXED)
    assert all(message in catalog for message in _LOCAL_API_MESSAGES)


def test_api_error_contract_stays_raw_in_japanese(tmp_path):
    from codex_bridge.console.api_client import ApiClientError, parse_json_reply
    from codex_bridge.console.i18n import initialize_language

    settings = QSettings(str(tmp_path / "api.ini"), QSettings.Format.IniFormat)
    settings.setValue("language", "ja-JP")
    initialize_language(settings)
    with pytest.raises(ApiClientError, match="Thread not found"):
        parse_json_reply(404, b"{}")


def test_api_error_presentation_preserves_unknown_details(tmp_path):
    from codex_bridge.console.api_error_presentation import format_api_error
    from codex_bridge.console.i18n import initialize_language

    settings = QSettings(str(tmp_path / "api.ini"), QSettings.Format.IniFormat)
    settings.setValue("language", "ja-JP")
    initialize_language(settings)
    assert format_api_error("Thread not found") == "スレッドが見つかりません"
    assert format_api_error("Approval already resolved") == "承認は処理済みです"
    assert format_api_error("Settings Send user output {p0}") == "Settings Send user output {p0}"


def test_japanese_already_resolved_approval_uses_existing_control_branch(tmp_path):
    from codex_bridge.console.api_client import ApiClient
    from codex_bridge.console.i18n import tr
    from codex_bridge.console.main_window import MainWindow
    from tests.console_main_window_test_support import (
        FakeClient,
        FakeCodexProbe,
        FakeCodexUpdateProbe,
        StableTunnel,
        _application,
        _config,
    )
    from tests.test_console_api_client import FakeManager, FakeReply

    _application()
    settings = QSettings(str(tmp_path / "approval.ini"), QSettings.Format.IniFormat)
    settings.setValue("language", "ja-JP")
    window = MainWindow(
        _config(),
        api_client=FakeClient(),
        codex_probe=FakeCodexProbe(),
        codex_update_probe=FakeCodexUpdateProbe(),
        tunnel_supervisor=StableTunnel(),
        tray_available=False,
        appearance_settings=settings,
    )
    reply = FakeReply(b'{"error":"already_resolved"}', status=409)
    client = ApiClient("http://127.0.0.1:8001", manager=FakeManager([reply]))
    client.control_failed.connect(window._apply_control_failure)
    window._approval_control_pending = True
    try:
        assert client.post_control_approval(
            "A" * 32, request_id="raw-id", decision="accept", key="control:approval"
        )
        reply.finished.emit()
        assert window.activity_pane._approval_resolved
        assert not window._approval_control_pending
        assert window.activity_pane._approval_feedback.text() == tr(
            "Approval already resolved. Refreshing status…"
        )
    finally:
        window._exit_finished = True
        window.close()
