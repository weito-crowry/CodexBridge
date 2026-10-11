from __future__ import annotations

from typing import Any

import pytest
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QMainWindow, QSystemTrayIcon

from codex_bridge.console.tray_controller import TrayController

_APPLICATION: QApplication | None = None


class FakeIcon:
    def __init__(self, *, null: bool = False) -> None:
        self._null = null

    def isNull(self) -> bool:
        return self._null


class Signal:
    def __init__(self) -> None:
        self._slots: list[Any] = []

    def connect(self, slot: Any) -> None:
        self._slots.append(slot)

    def emit(self, *args: Any) -> None:
        for slot in tuple(self._slots):
            slot(*args)


class FakeTray:
    def __init__(
        self,
        *,
        fail_at: str | None = None,
        set_icon_callable: bool = True,
        show_message_callable: bool = True,
    ) -> None:
        self.activated = Signal()
        self.fail_at = fail_at
        self.icon: object | None = None
        self.menu: object | None = None
        self.tooltip: str | None = None
        self.shown = 0
        self.usable_during_show: bool | None = None
        self.messages: list[tuple[str, str]] = []
        self.setIcon: Any = self._set_icon if set_icon_callable else None
        self.showMessage: Any = self._show_message if show_message_callable else None

    def _set_icon(self, icon: object) -> None:
        if self.fail_at == "setIcon":
            raise RuntimeError("setIcon failed")
        self.icon = icon

    def setToolTip(self, tooltip: str) -> None:
        self.tooltip = tooltip

    def setContextMenu(self, menu: object) -> None:
        if self.fail_at == "setContextMenu":
            raise RuntimeError("setContextMenu failed")
        self.menu = menu

    def show(self) -> None:
        self.shown += 1
        if self.window is not None:
            self.usable_during_show = self.window._tray_usable
        if self.fail_at == "show":
            raise RuntimeError("show failed")

    def _show_message(self, title: str, message: str) -> None:
        self.messages.append((title, message))
        if self.fail_at == "showMessage":
            raise RuntimeError("showMessage failed")

    window: Any | None = None


class FakeWindow(QMainWindow):
    def __init__(
        self,
        *,
        tray_available: bool = True,
        icon_null: bool = False,
        tray_factory: Any,
    ) -> None:
        super().__init__()
        self._tray_available = tray_available
        self._window_icon = FakeIcon(null=icon_null)
        self._tray_factory = tray_factory
        self.tray_icon: Any | None = None
        self.tray_menu: Any | None = None
        self._tray_actions: dict[str, QAction] = {}
        self._tray_usable = False
        self._tray_notified = False
        self._closing = False
        self.calls: list[str] = []
        self.controller: TrayController | None = None

    def show(self) -> None:
        self.calls.append("show")

    def raise_(self) -> None:
        self.calls.append("raise_")

    def activateWindow(self) -> None:
        self.calls.append("activateWindow")

    def hide(self) -> None:
        self.calls.append("hide")

    def _show_console(self) -> None:
        assert self.controller is not None
        self.controller.show_console()

    def _hide_console(self) -> None:
        assert self.controller is not None
        self.controller.hide_console()

    def _on_tray_activated(self, reason: object) -> None:
        assert self.controller is not None
        self.controller.on_activated(reason)

    def _start_bridge(self) -> None:
        self.calls.append("_start_bridge")

    def _stop_bridge(self) -> None:
        self.calls.append("_stop_bridge")

    def _restart_bridge(self) -> None:
        self.calls.append("_restart_bridge")

    def _start_tunnel(self) -> None:
        self.calls.append("_start_tunnel")

    def _stop_tunnel(self) -> None:
        self.calls.append("_stop_tunnel")

    def _restart_tunnel(self) -> None:
        self.calls.append("_restart_tunnel")

    def _begin_exit(self) -> None:
        self.calls.append("_begin_exit")


class FakeCloseEvent:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def accept(self) -> None:
        self.calls.append("accept")

    def ignore(self) -> None:
        self.calls.append("ignore")


def _application() -> QApplication:
    global _APPLICATION
    if _APPLICATION is None:
        instance = QApplication.instance()
        _APPLICATION = instance if isinstance(instance, QApplication) else QApplication([])
    return _APPLICATION


def _controller(
    *,
    tray_available: bool = True,
    icon_null: bool = False,
    tray: FakeTray | None = None,
) -> tuple[FakeWindow, TrayController, FakeTray, list[object]]:
    _application()
    fake_tray = tray if tray is not None else FakeTray()
    factory_calls: list[object] = []

    def tray_factory(window: object) -> FakeTray:
        factory_calls.append(window)
        fake_tray.window = window  # type: ignore[assignment]
        return fake_tray

    window = FakeWindow(
        tray_available=tray_available,
        icon_null=icon_null,
        tray_factory=tray_factory,
    )
    controller = TrayController(window)
    window.controller = controller
    return window, controller, fake_tray, factory_calls


@pytest.mark.parametrize(("tray_available", "icon_null"), ((False, False), (True, True)))
def test_build_skips_factory_when_tray_unavailable_or_icon_is_null(
    tray_available: bool,
    icon_null: bool,
) -> None:
    window, controller, _tray, factory_calls = _controller(
        tray_available=tray_available,
        icon_null=icon_null,
    )

    controller.build()

    assert factory_calls == []
    assert window.tray_icon is None
    assert not window._tray_usable


@pytest.mark.parametrize("fail_at", ("factory", "setIcon"))
def test_build_keeps_tray_unusable_when_factory_or_set_icon_fails(fail_at: str) -> None:
    _application()
    tray = FakeTray(fail_at=fail_at)
    window = FakeWindow(
        tray_factory=lambda _window: (
            (_ for _ in ()).throw(RuntimeError("factory failed")) if fail_at == "factory" else tray
        ),
    )
    controller = TrayController(window)
    window.controller = controller

    controller.build()

    assert window.tray_icon is None
    assert window.tray_menu is None
    assert window._tray_actions == {}
    assert not window._tray_usable


def test_build_keeps_tray_unusable_when_set_icon_is_not_callable() -> None:
    tray = FakeTray(set_icon_callable=False)
    window, controller, _tray, _factory_calls = _controller(tray=tray)

    controller.build()

    assert window.tray_icon is None
    assert window.tray_menu is None
    assert not window._tray_usable


@pytest.mark.parametrize("fail_at", ("setContextMenu", "show"))
def test_build_clears_tray_state_when_context_menu_or_show_fails(fail_at: str) -> None:
    window, controller, _tray, _factory_calls = _controller(tray=FakeTray(fail_at=fail_at))
    window._tray_actions["stale"] = QAction("stale", window)

    controller.build()

    assert window.tray_icon is None
    assert window.tray_menu is None
    assert window._tray_actions == {}
    assert not window._tray_usable


def test_build_preserves_menu_callbacks_icon_tooltip_and_activation_behavior() -> None:
    window, controller, tray, _factory_calls = _controller()

    controller.build()

    assert tray.icon is window._window_icon
    assert tray.tooltip == "CodexBridge Console"
    assert tray.menu is window.tray_menu
    assert tray.shown == 1
    assert tray.usable_during_show is False
    assert window._tray_usable
    assert [
        "|" if action.isSeparator() else action.text() for action in window.tray_menu.actions()
    ] == [
        "Show Console",
        "Hide Console",
        "|",
        "Start Bridge",
        "Stop Bridge",
        "Restart Bridge",
        "|",
        "Start Tunnel",
        "Stop Tunnel",
        "Restart Tunnel",
        "|",
        "Exit",
    ]

    tray.activated.emit(QSystemTrayIcon.ActivationReason.Trigger)
    assert window.calls == []
    tray.activated.emit(QSystemTrayIcon.ActivationReason.DoubleClick)
    assert window.calls == ["show", "raise_", "activateWindow"]

    for action in window.tray_menu.actions():
        if not action.isSeparator():
            action.trigger()
    assert window.calls[3:] == [
        "show",
        "raise_",
        "activateWindow",
        "hide",
        "_start_bridge",
        "_stop_bridge",
        "_restart_bridge",
        "_start_tunnel",
        "_stop_tunnel",
        "_restart_tunnel",
        "_begin_exit",
    ]


def test_show_and_hide_console_keep_window_call_order() -> None:
    window, controller, _tray, _factory_calls = _controller()

    controller.show_console()
    controller.hide_console()

    assert window.calls == ["show", "raise_", "activateWindow", "hide"]


def test_close_while_closing_accepts_without_other_actions() -> None:
    window, controller, _tray, _factory_calls = _controller()
    window._closing = True
    event = FakeCloseEvent()

    controller.handle_close_event(event)

    assert event.calls == ["accept"]
    assert window.calls == []


def test_close_with_usable_tray_hides_and_notifies_only_once() -> None:
    window, controller, tray, _factory_calls = _controller()
    window.tray_icon = tray
    window._tray_usable = True
    first_event = FakeCloseEvent()
    second_event = FakeCloseEvent()

    controller.handle_close_event(first_event)
    controller.handle_close_event(second_event)

    assert window.calls == ["hide", "hide"]
    assert first_event.calls == ["ignore"]
    assert second_event.calls == ["ignore"]
    assert tray.messages == [
        ("CodexBridge Console", "Console is still running in the system tray.")
    ]
    assert window._tray_notified


def test_close_marks_notification_attempted_even_when_show_message_raises() -> None:
    window, controller, tray, _factory_calls = _controller(tray=FakeTray(fail_at="showMessage"))
    window.tray_icon = tray
    window._tray_usable = True

    controller.handle_close_event(FakeCloseEvent())
    controller.handle_close_event(FakeCloseEvent())

    assert tray.messages == [
        ("CodexBridge Console", "Console is still running in the system tray.")
    ]
    assert window._tray_notified
    assert window.calls == ["hide", "hide"]


def test_close_marks_notification_attempted_when_show_message_is_not_callable() -> None:
    tray = FakeTray(show_message_callable=False)
    window, controller, _tray, _factory_calls = _controller(tray=tray)
    window.tray_icon = tray
    window._tray_usable = True

    controller.handle_close_event(FakeCloseEvent())

    assert tray.messages == []
    assert window._tray_notified
    assert window.calls == ["hide"]


def test_close_without_usable_tray_starts_exit_then_hides_and_ignores() -> None:
    window, controller, _tray, _factory_calls = _controller()
    event = FakeCloseEvent()

    controller.handle_close_event(event)

    assert window.calls == ["_begin_exit", "hide"]
    assert event.calls == ["ignore"]
