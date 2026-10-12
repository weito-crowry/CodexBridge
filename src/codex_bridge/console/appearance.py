from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from PySide6.QtCore import QObject, QSettings, Qt
from PySide6.QtGui import QAction, QActionGroup, QColor, QPalette
from PySide6.QtWidgets import QApplication, QMenu, QToolButton

if TYPE_CHECKING:
    from .main_window import MainWindow

AppearanceMode = Literal["System", "Light", "Dark"]
AppearanceScheme = Literal["light", "dark"]


@dataclass(frozen=True)
class AppearanceTokens:
    window: QColor
    window_text: QColor
    surface: QColor
    alternate_surface: QColor
    text: QColor
    muted_text: QColor
    disabled_text: QColor
    border: QColor
    strong_border: QColor
    button: QColor
    button_hover: QColor
    selection: QColor
    selection_text: QColor
    link: QColor
    warning: QColor
    error: QColor
    series_primary: QColor
    series_secondary: QColor
    reset_marker: QColor
    chart_background: QColor
    chart_plot: QColor
    chart_grid: QColor


@dataclass(frozen=True)
class AppearanceTheme:
    scheme: AppearanceScheme
    tokens: AppearanceTokens

    def make_palette(self) -> QPalette:
        tokens = self.tokens
        palette = QPalette()
        values = {
            QPalette.ColorRole.Window: tokens.window,
            QPalette.ColorRole.WindowText: tokens.window_text,
            QPalette.ColorRole.Base: tokens.surface,
            QPalette.ColorRole.AlternateBase: tokens.alternate_surface,
            QPalette.ColorRole.ToolTipBase: tokens.surface,
            QPalette.ColorRole.ToolTipText: tokens.window_text,
            QPalette.ColorRole.Text: tokens.text,
            QPalette.ColorRole.Button: tokens.button,
            QPalette.ColorRole.ButtonText: tokens.text,
            QPalette.ColorRole.BrightText: tokens.error,
            QPalette.ColorRole.Light: tokens.surface,
            QPalette.ColorRole.Midlight: tokens.alternate_surface,
            QPalette.ColorRole.Dark: tokens.window,
            QPalette.ColorRole.Mid: tokens.border,
            QPalette.ColorRole.Shadow: tokens.window,
            QPalette.ColorRole.Highlight: tokens.selection,
            QPalette.ColorRole.HighlightedText: tokens.selection_text,
            QPalette.ColorRole.Link: tokens.link,
            QPalette.ColorRole.LinkVisited: tokens.series_secondary,
            QPalette.ColorRole.PlaceholderText: tokens.muted_text,
        }
        for group in (
            QPalette.ColorGroup.Active,
            QPalette.ColorGroup.Inactive,
            QPalette.ColorGroup.Disabled,
        ):
            for role, color in values.items():
                palette.setColor(group, role, color)
        for role in (
            QPalette.ColorRole.WindowText,
            QPalette.ColorRole.Text,
            QPalette.ColorRole.ButtonText,
            QPalette.ColorRole.PlaceholderText,
        ):
            palette.setColor(QPalette.ColorGroup.Disabled, role, tokens.disabled_text)
        palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Highlight, tokens.border)
        palette.setColor(
            QPalette.ColorGroup.Disabled, QPalette.ColorRole.HighlightedText, tokens.disabled_text
        )
        return palette


_LIGHT = AppearanceTheme(
    "light",
    AppearanceTokens(
        window=QColor("#f6f7f9"),
        window_text=QColor("#202124"),
        surface=QColor("#ffffff"),
        alternate_surface=QColor("#eef1f5"),
        text=QColor("#202124"),
        muted_text=QColor("#5f6670"),
        disabled_text=QColor("#777e88"),
        border=QColor("#c6cbd2"),
        strong_border=QColor("#a8afb8"),
        button=QColor("#edf0f4"),
        button_hover=QColor("#dfe4eb"),
        selection=QColor("#356ac3"),
        selection_text=QColor("#ffffff"),
        link=QColor("#185abc"),
        warning=QColor("#965900"),
        error=QColor("#b3261e"),
        series_primary=QColor("#1769aa"),
        series_secondary=QColor("#7541a5"),
        reset_marker=QColor("#c43d35"),
        chart_background=QColor("#f6f7f9"),
        chart_plot=QColor("#ffffff"),
        chart_grid=QColor("#aeb5be"),
    ),
)

_DARK = AppearanceTheme(
    "dark",
    AppearanceTokens(
        window=QColor("#202124"),
        window_text=QColor("#e8eaed"),
        surface=QColor("#292a2d"),
        alternate_surface=QColor("#303134"),
        text=QColor("#f1f3f4"),
        muted_text=QColor("#aeb4bd"),
        disabled_text=QColor("#858b93"),
        border=QColor("#42464a"),
        strong_border=QColor("#5f6368"),
        button=QColor("#303134"),
        button_hover=QColor("#3c4043"),
        selection=QColor("#496b9d"),
        selection_text=QColor("#ffffff"),
        link=QColor("#8ab4f8"),
        warning=QColor("#fbbc04"),
        error=QColor("#f28b82"),
        series_primary=QColor("#8ab4f8"),
        series_secondary=QColor("#c58af9"),
        reset_marker=QColor("#f28b82"),
        chart_background=QColor("#202124"),
        chart_plot=QColor("#292a2d"),
        chart_grid=QColor("#777e88"),
    ),
)


def theme_for_scheme(scheme: AppearanceScheme) -> AppearanceTheme:
    return _DARK if scheme == "dark" else _LIGHT


def system_appearance_theme() -> AppearanceTheme:
    application = QApplication.instance()
    if isinstance(application, QApplication):
        scheme = application.styleHints().colorScheme()
        if scheme == Qt.ColorScheme.Dark:
            return _DARK
    return _LIGHT


def _css(color: QColor) -> str:
    return color.name(QColor.NameFormat.HexRgb)


def stylesheet_for_theme(theme: AppearanceTheme) -> str:
    colors = theme.tokens
    window = _css(colors.window)
    text = _css(colors.text)
    surface = _css(colors.surface)
    alternate = _css(colors.alternate_surface)
    muted = _css(colors.muted_text)
    disabled = _css(colors.disabled_text)
    border = _css(colors.border)
    strong_border = _css(colors.strong_border)
    button = _css(colors.button)
    hover = _css(colors.button_hover)
    selection = _css(colors.selection)
    selection_text = _css(colors.selection_text)
    link = _css(colors.link)
    error = _css(colors.error)
    return f"""
        QMainWindow, QDialog {{ background-color: {window}; }}
        QGroupBox {{ border: 1px solid {border}; margin-top: 8px; }}
        QFrame#historyCard {{
            background-color: {surface}; border: 1px solid {border}; border-radius: 4px;
        }}
        QLabel#topStatus {{ padding: 3px 8px; border: 1px solid {border}; border-radius: 3px; }}
        QLabel#topStatus[usageShortcut="true"]:hover {{ background-color: {alternate}; }}
        QLabel#topStatus[usageShortcut="true"]:focus {{ border-color: {link}; }}
        QLabel#bottomStatus {{ padding: 4px 6px; border-top: 1px solid {border}; }}
        QLabel#turnSeparator {{ color: {muted}; padding: 4px; }}
        QPushButton#showUsageHistory {{
            color: {link}; text-decoration: underline; border: 0; padding: 2px 0;
            text-align: left;
        }}
        QPushButton#showUsageHistory:hover {{ color: {selection}; }}
        QToolButton#appearanceButton {{ padding: 3px 7px; }}
        QLabel#usageHistoryMetricCaption {{ color: {muted}; }}
        QLabel#usageHistoryMetricValue {{ color: {text}; }}
        QLabel#usageHistoryUpdated, QLabel#usageHistoryState {{ color: {muted}; }}
        QLabel#usageHistoryRangeError {{ color: {error}; }}
        QFrame#usageHistoryMetricCard {{
            background-color: {surface}; border: 1px solid {border}; border-radius: 6px;
        }}
        QFrame#usageHistoryMetricCard QLabel {{ background-color: transparent; border: none; }}
        QLineEdit, QTreeWidget, QTextBrowser, QPlainTextEdit, QListWidget,
        QComboBox, QDateTimeEdit {{
            background-color: {surface}; color: {text}; border: 1px solid {border};
        }}
        QTreeWidget::item:selected, QListWidget::item:selected,
        QCalendarWidget QAbstractItemView::item:selected {{
            background-color: {selection}; color: {selection_text};
        }}
        QPushButton, QToolButton {{
            background-color: {button}; color: {text}; border: 1px solid {strong_border};
            padding: 4px 10px;
        }}
        QPushButton:hover, QToolButton:hover {{ background-color: {hover}; }}
        QPushButton:checked, QToolButton:checked {{
            background-color: {selection}; color: {selection_text};
        }}
        QPushButton:disabled, QToolButton:disabled {{
            background-color: {alternate}; color: {disabled}; border-color: {border};
        }}
        QToolButton#usagePreset24Hours {{ border-radius: 5px 0 0 5px; }}
        QToolButton#usagePreset7Days, QToolButton#usagePreset1Month,
        QToolButton#usagePreset1Year, QToolButton#usagePresetCustom {{ border-left: 0; }}
        QToolButton#usagePresetCustom {{ border-radius: 0 5px 5px 0; }}
        QComboBox::drop-down, QDateTimeEdit::drop-down {{
            border-left: 1px solid {border}; width: 20px;
        }}
        QMenu {{ background-color: {surface}; color: {text}; border: 1px solid {border}; }}
        QMenu::item {{ padding: 5px 24px 5px 8px; }}
        QMenu::item:selected {{ background-color: {selection}; color: {selection_text}; }}
        QToolTip {{
            background-color: {surface}; color: {text}; border: 1px solid {border}; padding: 3px;
        }}
        QScrollBar:vertical, QScrollBar:horizontal {{ background-color: {alternate}; border: 0; }}
        QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
            background-color: {strong_border}; min-width: 18px; min-height: 18px;
            border-radius: 4px;
        }}
        QScrollBar::handle:hover {{ background-color: {muted}; }}
        QScrollBar::add-line, QScrollBar::sub-line {{ background: transparent; border: 0; }}
        QSplitter::handle {{ background-color: {border}; }}
    """


class AppearanceController(QObject):
    settings_key = "appearance/mode"
    modes: tuple[AppearanceMode, ...] = ("System", "Light", "Dark")

    def __init__(
        self,
        window: MainWindow,
        settings: QSettings,
        *,
        system_scheme_getter: Callable[[], object] | None = None,
    ) -> None:
        super().__init__(window)
        self._window = window
        self._settings = settings
        self._system_scheme_getter = system_scheme_getter or self._read_system_scheme
        stored_mode = settings.value(self.settings_key, "System")
        self.mode: AppearanceMode = stored_mode if stored_mode in self.modes else "System"
        self.theme = self._resolve_theme()
        self._build_menu()
        application = QApplication.instance()
        if isinstance(application, QApplication):
            hints = application.styleHints()
            signal = getattr(hints, "colorSchemeChanged", None)
            if signal is not None:
                signal.connect(self._on_system_scheme_changed)

    def _read_system_scheme(self) -> object:
        application = QApplication.instance()
        if isinstance(application, QApplication):
            return application.styleHints().colorScheme()
        return Qt.ColorScheme.Unknown

    def _resolve_theme(self) -> AppearanceTheme:
        if self.mode == "Dark":
            return _DARK
        if self.mode == "Light":
            return _LIGHT
        try:
            scheme = self._system_scheme_getter()
        except Exception:
            scheme = Qt.ColorScheme.Unknown
        return _DARK if scheme == Qt.ColorScheme.Dark else _LIGHT

    def _build_menu(self) -> None:
        button = self._window.appearance_button
        menu = QMenu(button)
        group = QActionGroup(self)
        group.setExclusive(True)
        actions: dict[str, QAction] = {}
        for mode in self.modes:
            action = QAction(mode, menu)
            action.setCheckable(True)
            group.addAction(action)
            menu.addAction(action)
            action.triggered.connect(
                lambda checked=False, selected=mode: self.set_mode(selected) if checked else None
            )
            actions[mode] = action
        button.setMenu(menu)
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._window._appearance_actions = actions
        self._update_menu_checks()

    def _update_menu_checks(self) -> None:
        for mode, action in self._window._appearance_actions.items():
            action.setChecked(mode == self.mode)

    def _on_system_scheme_changed(self, *_args: object) -> None:
        self.refresh_system_scheme()

    def refresh_system_scheme(self) -> None:
        if self.mode != "System":
            self._update_menu_checks()
            return
        theme = self._resolve_theme()
        self._update_menu_checks()
        if theme.scheme == self.theme.scheme:
            return
        self.theme = theme
        self._apply_theme()

    def set_mode(self, mode: AppearanceMode) -> None:
        if mode not in self.modes:
            mode = "System"
        self.mode = mode
        self._settings.setValue(self.settings_key, mode)
        self._settings.sync()
        theme = self._resolve_theme()
        self._update_menu_checks()
        if theme.scheme != self.theme.scheme:
            self.theme = theme
            self._apply_theme()

    def apply(self) -> None:
        self._update_menu_checks()
        self._apply_theme()

    def _apply_theme(self) -> None:
        window = self._window
        application = QApplication.instance()
        palette = self.theme.make_palette()
        if isinstance(application, QApplication):
            application.setPalette(palette)
        window.setPalette(palette)
        window.setStyleSheet(stylesheet_for_theme(self.theme))
        window._usage_presentation.render_usage_status()
        if window._usage_history_window is not None:
            window._usage_history_window.apply_appearance(self.theme)


def install_appearance(
    window: MainWindow,
    settings: QSettings | None,
    system_scheme_getter: Callable[[], object] | None,
) -> None:
    appearance_settings = settings if settings is not None else QSettings("CodexBridge", "Console")
    window._appearance_settings = appearance_settings
    window._appearance_controller = AppearanceController(
        window,
        appearance_settings,
        system_scheme_getter=system_scheme_getter,
    )
    window._appearance_controller.apply()
