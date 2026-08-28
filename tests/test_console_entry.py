from __future__ import annotations

import importlib
import sys


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

        def exec(self) -> int:
            calls.append("exec")
            return 0

    class FakeWindow:
        def __init__(self, config) -> None:
            calls.append(("window", config.port))

        def show(self) -> None:
            calls.append("show")

    monkeypatch.setattr(entry, "_load_gui", lambda: (FakeApplication, FakeWindow))

    assert entry.main(["--ui-port", "8123"]) == 0
    assert calls[1:] == [("window", 8123), "show", "exec"]
