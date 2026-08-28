from __future__ import annotations

from typing import Any

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from codex_bridge.console.config import ConsoleConfig
from codex_bridge.console.main_window import MainWindow
from codex_bridge.console.tunnel_supervisor import TunnelActionState


class Signal:
    def __init__(self) -> None:
        self._slots: list[Any] = []

    def connect(self, slot: Any) -> None:
        self._slots.append(slot)

    def emit(self, *args: Any) -> None:
        for slot in tuple(self._slots):
            slot(*args)


class FakeClient:
    def __init__(self, events: list[str] | None = None) -> None:
        self.json_succeeded = Signal()
        self.json_failed = Signal()
        self.activity_received = Signal()
        self.stream_state_changed = Signal()
        self.control_succeeded = Signal()
        self.control_failed = Signal()
        self.aborted = 0
        self.control_requests: list[tuple[str, str]] = []
        self.events = events if events is not None else []

    def get_json(self, _path: str, *, key: str, query: object = None) -> bool:
        del key, query
        return True

    def abort_json_group(self, _prefix: str) -> None:
        pass

    def start_stream(self, _thread_id: str, _generation: int) -> None:
        pass

    def stop_stream(self) -> None:
        pass

    def abort_all(self) -> None:
        self.aborted += 1

    def post_control_shutdown(self, token: str, *, key: str) -> bool:
        self.control_requests.append((token, key))
        self.events.append("control.post")
        return True


class FakeProbe:
    def __init__(self) -> None:
        self.resolved = Signal()
        self.failed = Signal()
        self.aborted = 0

    def start(self) -> bool:
        return True

    def abort(self) -> None:
        self.aborted += 1


class FakeLauncher:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class FakeTunnelSupervisor:
    def __init__(
        self,
        *,
        delayed_close: bool = False,
        delayed_stop: bool = False,
        events: list[str] | None = None,
    ) -> None:
        self.state_changed = Signal()
        self.message_changed = Signal()
        self.controls_changed = Signal()
        self._state = "unavailable"
        self.action_state = TunnelActionState(False, False, False)
        self.bridge_ready_calls: list[bool] = []
        self.start_calls = 0
        self.stop_calls = 0
        self.restart_calls = 0
        self.close_calls = 0
        self._delayed_close = delayed_close
        self._delayed_stop = delayed_stop
        self._close_callback: Any | None = None
        self._stop_callback: Any | None = None
        self.events = events if events is not None else []

    @property
    def state(self) -> str:
        return self._state

    def set_bridge_ready(self, ready: bool) -> None:
        self.bridge_ready_calls.append(ready)

    def start(self) -> bool:
        self.start_calls += 1
        return True

    def stop(self, *, on_finished=None) -> bool:
        self.stop_calls += 1
        self.events.append("tunnel.stop")
        if on_finished is not None:
            if self._delayed_stop:
                self._stop_callback = on_finished
            else:
                on_finished()
        return True

    def restart(self) -> bool:
        self.restart_calls += 1
        return True

    def close(self, *, on_finished=None) -> None:
        self.close_calls += 1
        if on_finished is not None:
            if self._delayed_close:
                self._close_callback = on_finished
            else:
                on_finished()

    def complete_close(self) -> None:
        callback, self._close_callback = self._close_callback, None
        if callback is not None:
            callback()

    def complete_stop(self) -> None:
        callback, self._stop_callback = self._stop_callback, None
        if callback is not None:
            callback()

    def emit_state(self, state: str) -> None:
        self._state = state
        self.state_changed.emit(state)

    def controls(self, actions: TunnelActionState) -> None:
        self.action_state = actions
        self.controls_changed.emit(
            actions.start_enabled,
            actions.stop_enabled,
            actions.restart_enabled,
        )


class FakeTray:
    def __init__(self) -> None:
        self.activated = Signal()
        self.menu = None
        self.shown = 0
        self.hidden = 0

    def setContextMenu(self, menu: object) -> None:
        self.menu = menu

    def setToolTip(self, _text: str) -> None:
        pass

    def show(self) -> None:
        self.shown += 1

    def hide(self) -> None:
        self.hidden += 1


def _application() -> QApplication:
    application = QCoreApplication.instance()
    return application if isinstance(application, QApplication) else QApplication([])


def _window(
    *,
    tray_available: bool,
    quit_calls: list[str],
    tray: FakeTray | None = None,
    delayed_close: bool = False,
    delayed_stop: bool = False,
) -> tuple[MainWindow, FakeClient, FakeProbe, FakeLauncher, FakeTunnelSupervisor, FakeTray | None]:
    _application()
    events: list[str] = []
    client = FakeClient(events)
    probe = FakeProbe()
    launcher = FakeLauncher()
    supervisor = FakeTunnelSupervisor(
        delayed_close=delayed_close,
        delayed_stop=delayed_stop,
        events=events,
    )
    window = MainWindow(
        ConsoleConfig(),
        api_client=client,
        codex_probe=probe,
        runtime_launcher=launcher,
        tunnel_supervisor=supervisor,
        tray_available=tray_available,
        tray_factory=(lambda _parent: tray) if tray is not None else None,
        quit_application=lambda: quit_calls.append("quit"),
    )
    return window, client, probe, launcher, supervisor, tray


def test_tunnel_bridge_readiness_is_forwarded_to_supervisor() -> None:
    window, client, _probe, _launcher, supervisor, _tray = _window(
        tray_available=False,
        quit_calls=[],
    )

    client.json_succeeded.emit("health", {"status": "ok"})
    client.json_succeeded.emit("bridge-status", {"bridge": "ready", "app_server": "ready"})

    assert supervisor.bridge_ready_calls[-1] is True
    window.close()


def test_window_tunnel_controls_follow_one_supervisor_action_state() -> None:
    window, _client, _probe, _launcher, supervisor, _tray = _window(
        tray_available=False,
        quit_calls=[],
    )

    supervisor.emit_state("ready_to_start")
    supervisor.controls(TunnelActionState(True, False, False))

    assert window.tunnel_status_label.text() == "Tunnel: ready to start"
    assert window.start_tunnel_button.isEnabled()
    assert not window.stop_tunnel_button.isEnabled()
    assert not window.restart_tunnel_button.isEnabled()
    window.close()


def test_initial_and_runtime_tunnel_controls_are_synced_for_window_and_tray() -> None:
    tray = FakeTray()
    window, _client, _probe, _launcher, supervisor, _tray = _window(
        tray_available=True,
        quit_calls=[],
        tray=tray,
    )
    assert tray.menu is not None
    actions = {action.text(): action for action in tray.menu.actions() if not action.isSeparator()}

    for control in (
        window.start_tunnel_button,
        window.stop_tunnel_button,
        window.restart_tunnel_button,
    ):
        assert not control.isEnabled()
    for name in ("Start Tunnel", "Stop Tunnel", "Restart Tunnel"):
        assert not actions[name].isEnabled()
    for name in ("Start Bridge", "Stop Bridge", "Restart Bridge"):
        assert not actions[name].isEnabled()

    supervisor.emit_state("checking")
    supervisor.controls(TunnelActionState(False, False, False))
    supervisor.emit_state("ready_to_start")
    supervisor.controls(TunnelActionState(True, False, False))
    assert window.start_tunnel_button.isEnabled()
    assert actions["Start Tunnel"].isEnabled()
    assert not window.stop_tunnel_button.isEnabled()
    assert not actions["Stop Tunnel"].isEnabled()

    supervisor.emit_state("ready")
    supervisor.controls(TunnelActionState(False, True, True))
    assert not window.start_tunnel_button.isEnabled()
    assert not actions["Start Tunnel"].isEnabled()
    assert window.stop_tunnel_button.isEnabled()
    assert actions["Stop Tunnel"].isEnabled()
    assert window.restart_tunnel_button.isEnabled()
    assert actions["Restart Tunnel"].isEnabled()
    window._begin_exit()


def test_available_tray_close_hides_without_cleanup_or_tunnel_stop() -> None:
    tray = FakeTray()
    quit_calls: list[str] = []
    window, client, probe, launcher, supervisor, _tray = _window(
        tray_available=True,
        quit_calls=quit_calls,
        tray=tray,
    )
    window.show()

    window.close()

    assert not window.isVisible()
    assert client.aborted == 0
    assert probe.aborted == 0
    assert launcher.closed == 0
    assert supervisor.close_calls == 0
    assert quit_calls == []
    window._begin_exit()


def test_tray_actions_call_same_tunnel_operations_as_window_controls() -> None:
    tray = FakeTray()
    window, _client, _probe, _launcher, supervisor, _tray = _window(
        tray_available=True,
        quit_calls=[],
        tray=tray,
    )
    assert tray.menu is not None
    actions = {action.text(): action for action in tray.menu.actions() if not action.isSeparator()}
    supervisor.controls(TunnelActionState(True, True, True))

    actions["Start Tunnel"].trigger()
    actions["Stop Tunnel"].trigger()
    actions["Restart Tunnel"].trigger()

    assert supervisor.start_calls == 1
    assert supervisor.stop_calls == 1
    assert supervisor.restart_calls == 1
    window._begin_exit()


def test_tray_bridge_actions_share_owned_state_and_stop_tunnel_before_control_post() -> None:
    tray = FakeTray()
    window, client, _probe, _launcher, supervisor, _tray = _window(
        tray_available=True,
        quit_calls=[],
        tray=tray,
    )
    assert tray.menu is not None
    actions = {action.text(): action for action in tray.menu.actions() if not action.isSeparator()}
    supervisor.emit_state("ready")
    supervisor.controls(TunnelActionState(False, True, True))
    window._runtime_state = "console_started"
    window._bridge_ready = True
    window._app_server_ready = True
    window._control_token = "A" * 32
    window._update_bridge_controls()

    assert actions["Stop Bridge"].isEnabled()
    assert actions["Restart Bridge"].isEnabled()
    actions["Stop Bridge"].trigger()

    assert supervisor.stop_calls == 1
    assert client.control_requests == [("A" * 32, "control:shutdown")]
    assert client.events == ["tunnel.stop", "control.post"]
    window._begin_exit()


def test_tunnel_transition_disables_bridge_actions_from_shared_state() -> None:
    tray = FakeTray()
    window, _client, _probe, _launcher, supervisor, _tray = _window(
        tray_available=True,
        quit_calls=[],
        tray=tray,
    )
    assert tray.menu is not None
    actions = {action.text(): action for action in tray.menu.actions() if not action.isSeparator()}
    window._runtime_state = "console_started"
    window._bridge_ready = True
    window._app_server_ready = True
    window._control_token = "A" * 32
    supervisor.emit_state("starting")
    supervisor.controls(TunnelActionState(False, False, False))
    window._update_bridge_controls()

    assert not window.stop_bridge_button.isEnabled()
    assert not window.restart_bridge_button.isEnabled()
    assert not actions["Stop Bridge"].isEnabled()
    assert not actions["Restart Bridge"].isEnabled()
    window._begin_exit()


def test_console_exit_does_not_post_bridge_control_after_tunnel_stop_callback() -> None:
    tray = FakeTray()
    window, client, _probe, _launcher, supervisor, _tray = _window(
        tray_available=True,
        quit_calls=[],
        tray=tray,
        delayed_stop=True,
    )
    supervisor.emit_state("ready")
    supervisor.controls(TunnelActionState(False, True, True))
    window._runtime_state = "console_started"
    window._bridge_ready = True
    window._app_server_ready = True
    window._control_token = "A" * 32
    window._update_bridge_controls()

    window.stop_bridge_button.click()
    window._begin_exit()
    supervisor.complete_stop()

    assert client.control_requests == []


def test_tray_exit_cleans_up_and_quits_after_tunnel_close() -> None:
    tray = FakeTray()
    quit_calls: list[str] = []
    window, client, probe, launcher, supervisor, _tray = _window(
        tray_available=True,
        quit_calls=quit_calls,
        tray=tray,
    )
    actions = {action.text(): action for action in tray.menu.actions() if not action.isSeparator()}

    actions["Exit"].trigger()

    assert supervisor.close_calls == 1
    assert client.aborted == 1
    assert probe.aborted == 1
    assert launcher.closed == 1
    assert quit_calls == ["quit"]
    window.close()


def test_unavailable_tray_uses_explicit_exit_cleanup_on_window_close() -> None:
    quit_calls: list[str] = []
    window, client, probe, launcher, supervisor, _tray = _window(
        tray_available=False,
        quit_calls=quit_calls,
    )

    window.close()

    assert supervisor.close_calls == 1
    assert client.aborted == 1
    assert probe.aborted == 1
    assert launcher.closed == 1
    assert quit_calls == ["quit"]


def test_unavailable_tray_close_waits_for_async_tunnel_cleanup() -> None:
    quit_calls: list[str] = []
    window, client, probe, launcher, supervisor, _tray = _window(
        tray_available=False,
        quit_calls=quit_calls,
        delayed_close=True,
    )
    window.show()

    assert not window.close()

    assert supervisor.close_calls == 1
    assert client.aborted == 1
    assert probe.aborted == 1
    assert launcher.closed == 1
    assert window._closing
    assert not window.isVisible()
    assert quit_calls == []

    supervisor.complete_close()

    assert quit_calls == ["quit"]
    supervisor.complete_close()
    assert quit_calls == ["quit"]
