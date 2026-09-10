from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace


def test_console_entry_import_does_not_import_pyside6() -> None:
    sys.modules.pop("codex_bridge.console_entry", None)
    sys.modules.pop("PySide6", None)

    importlib.import_module("codex_bridge.console_entry")

    assert "PySide6" not in sys.modules


def test_missing_console_extra_has_bounded_error(monkeypatch, capsys) -> None:
    entry = importlib.import_module("codex_bridge.console_entry")

    def missing_extra():
        raise entry.ConsoleDependencyError(entry._MISSING_EXTRA_MESSAGE)

    monkeypatch.setattr(entry, "_load_gui", missing_extra)

    result = entry.main(["--ui-port", "8001"])

    captured = capsys.readouterr()
    assert result != 0
    assert "CodexBridge Console requires the 'console' extra." in captured.err
    assert "uv sync --extra console" in captured.err
    assert "Traceback" not in captured.err


def test_entrypoint_constructs_and_runs_gui_after_configuration(monkeypatch) -> None:
    entry = importlib.import_module("codex_bridge.console_entry")
    calls: list[object] = []

    class FakeApplication:
        def __init__(self, args) -> None:
            calls.append(("application", args))

        def setWindowIcon(self, value) -> None:
            calls.append(("icon", value))

        def exec(self) -> int:
            calls.append("exec")
            return 0

    class FakeWindow:
        def __init__(self, config) -> None:
            calls.append(("window", config.port))

        def show(self) -> None:
            calls.append("show")

    monkeypatch.setattr(entry, "_load_gui", lambda: (FakeApplication, FakeWindow))
    monkeypatch.setattr(entry, "_load_application_icon", lambda: object())

    assert entry.main(["--ui-port", "8123"]) == 0
    assert calls[1][0] == "icon"
    assert calls[2:] == [("window", 8123), "show", "exec"]


def test_entrypoint_sets_application_icon_before_creating_window(monkeypatch) -> None:
    entry = importlib.import_module("codex_bridge.console_entry")
    icon = object()
    calls: list[object] = []

    class FakeApplication:
        def __init__(self, args) -> None:
            calls.append(("application", args))

        def setWindowIcon(self, value) -> None:
            calls.append(("icon", value))

        def exec(self) -> int:
            calls.append("exec")
            return 0

    class FakeWindow:
        def __init__(self, config) -> None:
            calls.append(("window", config.port))

        def show(self) -> None:
            calls.append("show")

    monkeypatch.setattr(entry, "_load_gui", lambda: (FakeApplication, FakeWindow))
    monkeypatch.setattr(entry, "_load_application_icon", lambda: icon, raising=False)
    monkeypatch.setattr(
        entry,
        "_set_windows_app_user_model_id",
        lambda: calls.append("app-id"),
        raising=False,
    )

    assert entry.main(["--ui-port", "8123"]) == 0
    assert calls[0] == "app-id"
    assert calls[1][0] == "application"
    assert calls[2:] == [("icon", icon), ("window", 8123), "show", "exec"]


def test_windows_app_user_model_id_uses_shell32(monkeypatch) -> None:
    entry = importlib.import_module("codex_bridge.console_entry")
    helper = getattr(entry, "_set_windows_app_user_model_id", None)
    assert callable(helper)
    calls: list[str] = []

    class Shell32:
        def SetCurrentProcessExplicitAppUserModelID(self, app_id: str) -> None:
            calls.append(app_id)

    monkeypatch.setattr(entry.sys, "platform", "win32")
    monkeypatch.setattr(
        entry,
        "ctypes",
        SimpleNamespace(windll=SimpleNamespace(shell32=Shell32())),
        raising=False,
    )

    helper()

    assert calls == ["CodexBridge.Console"]


def test_non_windows_app_user_model_id_does_not_access_windows_api(monkeypatch) -> None:
    entry = importlib.import_module("codex_bridge.console_entry")
    helper = getattr(entry, "_set_windows_app_user_model_id", None)
    assert callable(helper)

    class UnexpectedWindowsApi:
        @property
        def windll(self):
            raise AssertionError("Windows API must not be accessed")

    monkeypatch.setattr(entry.sys, "platform", "linux")
    monkeypatch.setattr(entry, "ctypes", UnexpectedWindowsApi(), raising=False)

    helper()
