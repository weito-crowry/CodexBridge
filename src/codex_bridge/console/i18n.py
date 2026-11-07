"""Console-owned UI translation. Language changes apply on the next startup.

Only explicit tr() calls are translated. Never pass user/model text or raw output
as a template. Named arguments are interpolated after catalog lookup.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PySide6.QtCore import QCoreApplication, QLibraryInfo, QLocale, QObject, QSettings, QTranslator
from shiboken6 import isValid

_CATALOG_PATH = Path(__file__).with_name("locales") / "ja-JP.json"
_CATALOG: dict[str, str] = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
_LANGUAGE = "en"
_QT_TRANSLATOR: QTranslator | None = None


def resolve_language(setting: object, system_locale: str) -> str:
    if setting == "system":
        return "ja-JP" if system_locale.replace("_", "-").lower().split("-")[0] == "ja" else "en"
    return setting if setting in ("ja-JP", "en") else "en"


def language() -> str:
    return _LANGUAGE


def tr(template: str, **arguments: Any) -> str:
    translated = _CATALOG.get(template, template) if _LANGUAGE == "ja-JP" else template
    return translated.format(**arguments) if arguments else translated


class _StandardButtonTranslator(QTranslator):
    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self._qt_base = QTranslator(self)
        translations = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
        self._qt_base.load(str(Path(translations) / "qtbase_ja.qm"))

    def translate(
        self, context: str, sourceText: str, disambiguation: str | None = None, n: int = -1
    ) -> str:
        # Qt-owned standard button/menu labels only; custom data is never translated.
        if (
            context
            in {
                "QPlatformTheme",
                "QDialogButtonBox",
                "QMessageBox",
                "QFileDialog",
                "QLineEdit",
                "QWidgetTextControl",
            }
            and _LANGUAGE == "ja-JP"
        ):
            if sourceText in _CATALOG:
                return _CATALOG[sourceText]
        if _LANGUAGE == "ja-JP":
            translated = self._qt_base.translate(context, sourceText, disambiguation, n)
            if translated:
                return translated
        return sourceText

    def isEmpty(self) -> bool:
        return False


def initialize_language(
    settings: QSettings | None = None, *, system_locale: str | None = None
) -> str:
    global _LANGUAGE, _QT_TRANSLATOR
    store = settings if settings is not None else QSettings("CodexBridge", "Console")
    _LANGUAGE = resolve_language(
        store.value("language", "system"), system_locale or QLocale.system().name()
    )
    QLocale.setDefault(QLocale("ja_JP" if _LANGUAGE == "ja-JP" else "en_US"))
    application = QCoreApplication.instance()
    if application is not None and (_QT_TRANSLATOR is None or not isValid(_QT_TRANSLATOR)):
        _QT_TRANSLATOR = _StandardButtonTranslator(application)
        application.installTranslator(_QT_TRANSLATOR)
    return _LANGUAGE


def save_language(settings: QSettings, selected: str) -> None:
    settings.setValue("language", selected if selected in ("system", "ja-JP", "en") else "en")
    settings.sync()


def install_language_menu(menu: Any, settings: QSettings) -> None:
    from PySide6.QtGui import QActionGroup

    language_menu = menu.addMenu(tr("Language (restart required)"))
    group = QActionGroup(language_menu)
    group.setExclusive(True)
    stored = settings.value("language", "system")
    selected = stored if stored in ("system", "ja-JP", "en") else "en"
    for key, label in (("system", tr("Follow system")), ("ja-JP", "日本語"), ("en", "English")):
        action = language_menu.addAction(label)
        action.setData(key)
        action.setCheckable(True)
        action.setChecked(key == selected)
        group.addAction(action)
        action.triggered.connect(
            lambda checked=False, value=key: save_language(settings, value) if checked else None
        )
