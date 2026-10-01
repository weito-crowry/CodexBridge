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


def test_internal_runtime_dispatches_before_gui_and_uses_bridge_main(monkeypatch) -> None:
    entry = importlib.import_module("codex_bridge.console_entry")
    bridge_main = importlib.import_module("codex_bridge.__main__")
    calls: list[object] = []
    monkeypatch.setattr(entry, "_parser", lambda: (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr(entry, "_load_gui", lambda: (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr(
        entry,
        "_prepare_internal_runtime_stdio",
        lambda: calls.append("stdio"),
    )
    monkeypatch.setattr(bridge_main, "main", lambda args: calls.append(("bridge", args)) or 23)
    monkeypatch.setattr(
        entry,
        "_set_windows_app_user_model_id",
        lambda: (_ for _ in ()).throw(AssertionError()),
    )

    assert entry.main(["--codexbridge-runtime"]) == 23
    assert calls == ["stdio", ("bridge", [])]


def test_internal_runtime_stdio_fallback_opens_utf8_append_logs(tmp_path, monkeypatch) -> None:
    entry = importlib.import_module("codex_bridge.console_entry")
    stdout_path = tmp_path / "bridge-runtime-stdout.log"
    stderr_path = tmp_path / "bridge-runtime-stderr.log"
    stdout_path.write_text("prior stdout\n", encoding="utf-8")
    stderr_path.write_text("prior stderr\n", encoding="utf-8")
    monkeypatch.setattr(
        entry,
        "_runtime_stdio_log_paths",
        lambda: (stdout_path, stderr_path),
        raising=False,
    )
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    entry._prepare_internal_runtime_stdio()

    assert sys.stdout is not None
    assert sys.stderr is not None
    assert sys.stdout.encoding.lower().replace("-", "") == "utf8"
    assert sys.stderr.encoding.lower().replace("-", "") == "utf8"
    assert sys.stdout.line_buffering
    assert sys.stderr.line_buffering
    sys.stdout.write("new stdout\n")
    sys.stderr.write("new stderr\n")
    sys.stdout.flush()
    sys.stderr.flush()
    sys.stdout.close()
    sys.stderr.close()
    assert stdout_path.read_text(encoding="utf-8") == "prior stdout\nnew stdout\n"
    assert stderr_path.read_text(encoding="utf-8") == "prior stderr\nnew stderr\n"


def test_internal_runtime_stdio_preserves_existing_streams(monkeypatch) -> None:
    entry = importlib.import_module("codex_bridge.console_entry")
    stdout = object()
    stderr = object()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    entry._prepare_internal_runtime_stdio()

    assert sys.stdout is stdout
    assert sys.stderr is stderr


def test_internal_runtime_stdio_log_failure_does_not_raise_secondary_error(monkeypatch) -> None:
    entry = importlib.import_module("codex_bridge.console_entry")

    def broken_paths():
        raise OSError("logs unavailable")

    monkeypatch.setattr(entry, "_runtime_stdio_log_paths", broken_paths, raising=False)
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    entry._prepare_internal_runtime_stdio()

    assert sys.stdout is not None
    assert sys.stderr is not None
    sys.stdout.write("stdout remains safe")
    sys.stderr.write("stderr remains safe")
    sys.stdout.close()
    sys.stderr.close()


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
