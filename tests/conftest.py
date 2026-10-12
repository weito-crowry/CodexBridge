from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QSettings

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))


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
