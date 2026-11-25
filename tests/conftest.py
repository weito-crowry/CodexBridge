from __future__ import annotations

import sys
from functools import wraps
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))


class FakeClipboard:
    def __init__(self) -> None:
        self._text = ""

    def setText(self, text: str) -> None:
        self._text = text

    def text(self) -> str:
        return self._text

    def clear(self) -> None:
        self._text = ""


@pytest.fixture(autouse=True)
def set_test_application_style_on_initialization(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply Fusion once when a test creates the shared QApplication."""

    original_init = QApplication.__init__

    def initialize_with_fusion(self: QApplication, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.setStyle("Fusion")

    monkeypatch.setattr(QApplication, "__init__", initialize_with_fusion)


@pytest.fixture
def fake_clipboard(monkeypatch: pytest.MonkeyPatch) -> FakeClipboard:
    clipboard = FakeClipboard()
    monkeypatch.setattr(QApplication, "clipboard", staticmethod(lambda: clipboard))
    return clipboard


@pytest.fixture(autouse=True)
def isolated_usage_history_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    from codex_bridge.console import usage_history

    database_path = tmp_path / "usage-history.sqlite3"
    default_path_resolver = usage_history.default_usage_history_path

    def isolated_default_path_resolver(*args: Any, **kwargs: Any) -> Path:
        if not args and not kwargs:
            return database_path
        return default_path_resolver(*args, **kwargs)

    monkeypatch.setattr(
        usage_history,
        "default_usage_history_path",
        isolated_default_path_resolver,
    )
    main_window_module = sys.modules.get("codex_bridge.console.main_window")
    if main_window_module is not None:
        monkeypatch.setattr(
            main_window_module,
            "default_usage_history_path",
            isolated_default_path_resolver,
        )
    return database_path


@pytest.fixture(autouse=True)
def isolated_qsettings_store(tmp_path: Path):
    previous_format = QSettings.defaultFormat()
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(
        QSettings.Format.IniFormat,
        QSettings.Scope.UserScope,
        str(tmp_path / "qsettings"),
    )
    QSettings.setPath(
        QSettings.Format.IniFormat,
        QSettings.Scope.SystemScope,
        str(tmp_path / "qsettings-system"),
    )
    yield
    QSettings.setDefaultFormat(previous_format)


@pytest.fixture(autouse=True)
def console_english_baseline(request, isolated_qsettings_store):
    """Keep legacy UI assertions independent of the host language; i18n tests own theirs."""
    if (
        not request.node.path.name.startswith("test_console_")
        or request.node.path.name == "test_console_i18n.py"
    ):
        yield
        return
    from codex_bridge.console.i18n import initialize_language

    settings = QSettings("CodexBridge", "Console")
    settings.setValue("language", "en")
    initialize_language(settings)
    yield
    settings.setValue("language", "en")
    initialize_language(settings)


@pytest.fixture(autouse=True)
def isolate_fake_client_main_window_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep FakeClient MainWindow tests from starting host executables by default."""

    from codex_bridge.console import main_window as main_window_module
    from tests.console_main_window_test_support import (
        FakeClient,
        FakeCodexProbe,
        FakeCodexUpdateProbe,
        FakeLauncher,
        StableTunnel,
    )

    original_init = main_window_module.MainWindow.__init__
    real_tunnel_supervisor = main_window_module.TunnelSupervisor
    real_tunnel_enumerator = main_window_module.enumerate_tunnel_candidates

    @wraps(original_init)
    def isolated_init(self: Any, config: Any, parent: Any = None, *args: Any, **kwargs: Any):
        if isinstance(kwargs.get("api_client"), FakeClient):
            kwargs.setdefault("codex_probe", FakeCodexProbe())
            kwargs.setdefault("codex_update_probe", FakeCodexUpdateProbe())
            kwargs.setdefault("runtime_launcher", FakeLauncher())
            if (
                main_window_module.TunnelSupervisor is real_tunnel_supervisor
                and main_window_module.enumerate_tunnel_candidates is real_tunnel_enumerator
            ):
                kwargs.setdefault("tunnel_supervisor", StableTunnel())
        return original_init(self, config, parent, *args, **kwargs)

    monkeypatch.setattr(main_window_module.MainWindow, "__init__", isolated_init)
