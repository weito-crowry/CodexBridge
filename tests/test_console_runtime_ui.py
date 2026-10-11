from __future__ import annotations

from types import SimpleNamespace

import pytest

from codex_bridge.console.runtime_ui import RuntimeUiPresenter


class FakeWidget:
    def __init__(self) -> None:
        self.value = ""
        self.tooltip = ""
        self.enabled = False

    def setText(self, value: str) -> None:
        self.value = value

    def text(self) -> str:
        return self.value

    def setToolTip(self, value: str) -> None:
        self.tooltip = value

    def toolTip(self) -> str:
        return self.tooltip

    def setEnabled(self, value: bool) -> None:
        self.enabled = value

    def isEnabled(self) -> bool:
        return self.enabled


class FakePane:
    def __init__(self) -> None:
        self.empty_state = ""
        self.error = ""

    def set_empty_state(self, value: str) -> None:
        self.empty_state = value

    def set_error(self, value: str) -> None:
        self.error = value


class FakeWindow:
    def __init__(self) -> None:
        for name in (
            "tunnel_client_status_label",
            "start_tunnel_button",
            "stop_tunnel_button",
            "restart_tunnel_button",
            "stop_bridge_button",
            "restart_bridge_button",
            "restart_codexbridge_button",
            "start_bridge_button",
            "retry_now_button",
            "overall_status_label",
            "overall_detail_label",
        ):
            setattr(self, name, FakeWidget())
        self._tray_actions = {
            name: FakeWidget()
            for name in (
                "Start Bridge",
                "Stop Bridge",
                "Restart Bridge",
                "Start Tunnel",
                "Stop Tunnel",
                "Restart Tunnel",
            )
        }
        self._tunnel = SimpleNamespace(
            client_version=None,
            resolution_source="unavailable",
            executable=None,
            state="unavailable",
            action_state=SimpleNamespace(
                start_enabled=False,
                stop_enabled=False,
                restart_enabled=False,
            ),
        )
        self._config = SimpleNamespace(host="127.0.0.1", port=43121)
        self.history_pane = FakePane()
        self.activity_pane = FakePane()
        self.thread_pane = SimpleNamespace(thread_count=0)
        self._selected_thread_id: str | None = None
        self._tunnel_resolution_error: str | None = None
        self._closing = False
        self._runtime_state = "unavailable"
        self._bridge_ready = False
        self._app_server_ready = False
        self._control_token: str | None = None
        self._bridge_transition = False
        self._health_ok = False
        self._health_observed = False
        self._status_observed = False
        self._launch_in_progress = False
        self._managed_start_active = False
        self._bridge_start_retry_exhausted = False
        self._tunnel_state = "unavailable"
        self.can_start_bridge = False
        self._runtime_ui = RuntimeUiPresenter(self)

    def _update_tunnel_client_status(self) -> None:
        self._runtime_ui._update_tunnel_client_status()

    def _apply_tunnel_controls(self, start: bool, stop: bool, restart: bool) -> None:
        self._runtime_ui._apply_tunnel_controls(start, stop, restart)

    def _update_bridge_controls(self) -> None:
        self._runtime_ui._update_bridge_controls()

    def _sync_bridge_controls(self) -> None:
        self._runtime_ui._sync_bridge_controls()

    def _sync_tunnel_controls(self) -> None:
        self._runtime_ui._sync_tunnel_controls()

    def _sync_empty_state(self) -> None:
        self._runtime_ui._sync_empty_state()

    def _sync_overall_status(self) -> None:
        self._runtime_ui._sync_overall_status()

    def _update_start_button(self) -> None:
        self._runtime_ui._update_start_button()

    def _update_lifecycle_controls(self) -> None:
        self._runtime_ui._update_lifecycle_controls()

    def _can_start_bridge(self) -> bool:
        return self.can_start_bridge

    def _tunnel_is_transitioning(self) -> bool:
        return self._tunnel.state in {"checking", "starting", "stopping"}


@pytest.mark.parametrize(
    ("version", "source", "executable", "error", "expected_text", "expected_tooltip"),
    (
        (None, "unavailable", None, None, "Tunnel Client: unavailable", ""),
        (
            "1.2.3",
            "managed",
            "C:/tools/tunnel.exe",
            None,
            "Tunnel Client: 1.2.3 · managed",
            "C:/tools/tunnel.exe",
        ),
        (
            None,
            "unavailable",
            None,
            "Tunnel Client could not be resolved",
            "Tunnel Client: unavailable",
            "Tunnel Client could not be resolved",
        ),
        (
            "1.2.3",
            "path",
            "C:/tools/tunnel.exe",
            "Resolution failed: access denied",
            "Tunnel Client: 1.2.3 · path",
            "Resolution failed: access denied",
        ),
    ),
)
def test_tunnel_client_status_preserves_resolution_text_and_tooltip_precedence(
    version: str | None,
    source: str,
    executable: str | None,
    error: str | None,
    expected_text: str,
    expected_tooltip: str,
) -> None:
    window = FakeWindow()
    window._tunnel.client_version = version
    window._tunnel.resolution_source = source
    window._tunnel.executable = executable
    window._tunnel_resolution_error = error

    window._update_tunnel_client_status()

    assert window.tunnel_client_status_label.text() == expected_text
    assert window.tunnel_client_status_label.toolTip() == expected_tooltip


@pytest.mark.parametrize(
    ("runtime", "bridge_ready", "app_ready", "token", "transition", "tunnel", "enabled"),
    (
        ("console_started", True, True, "owned-token", False, "ready", True),
        ("console_started", True, True, None, False, "ready", False),
        ("external", True, True, "token", False, "ready", False),
        ("console_started", True, True, "token", True, "ready", False),
        ("console_started", True, True, "token", False, "starting", False),
    ),
)
def test_bridge_controls_follow_runtime_ownership_and_transitions(
    runtime: str,
    bridge_ready: bool,
    app_ready: bool,
    token: str | None,
    transition: bool,
    tunnel: str,
    enabled: bool,
) -> None:
    window = FakeWindow()
    window._runtime_state = runtime
    window._bridge_ready = bridge_ready
    window._app_server_ready = app_ready
    window._control_token = token
    window._bridge_transition = transition
    window._tunnel.state = tunnel

    window._sync_bridge_controls()

    for name in ("stop_bridge_button", "restart_bridge_button", "restart_codexbridge_button"):
        assert getattr(window, name).isEnabled() is enabled
    assert window._tray_actions["Stop Bridge"].isEnabled() is enabled
    assert window._tray_actions["Restart Bridge"].isEnabled() is enabled
    assert window._tray_actions["Start Bridge"].isEnabled() is False
    expected_tooltip = (
        "Disabled while the Bridge is managed externally."
        if runtime == "external" and bridge_ready and app_ready
        else "Restart the Console-managed Bridge."
    )
    assert window.restart_codexbridge_button.toolTip() == expected_tooltip


def test_presenter_chains_existing_main_window_wrappers(monkeypatch: pytest.MonkeyPatch) -> None:
    window = FakeWindow()
    bridge_calls: list[str] = []
    monkeypatch.setattr(window, "_update_start_button", lambda: bridge_calls.append("start"))
    monkeypatch.setattr(window, "_update_bridge_controls", lambda: bridge_calls.append("bridge"))

    window._sync_bridge_controls()

    assert bridge_calls == ["start", "bridge"]

    applied: list[tuple[bool, bool, bool]] = []
    monkeypatch.setattr(window, "_apply_tunnel_controls", lambda *values: applied.append(values))
    window._tunnel.action_state = SimpleNamespace(
        start_enabled=True,
        stop_enabled=False,
        restart_enabled=True,
    )
    window._sync_tunnel_controls()
    assert applied == [(True, False, True)]

    monkeypatch.undo()
    lifecycle_calls: list[str] = []
    monkeypatch.setattr(window, "_can_start_bridge", lambda: True)
    monkeypatch.setattr(
        window, "_update_lifecycle_controls", lambda: lifecycle_calls.append("lifecycle")
    )
    window._update_start_button()
    assert window.start_bridge_button.isEnabled()
    assert window._tray_actions["Start Bridge"].isEnabled()
    assert lifecycle_calls == ["lifecycle"]


def test_tunnel_controls_use_action_state_and_disable_during_bridge_transition() -> None:
    window = FakeWindow()
    window._tunnel.action_state = SimpleNamespace(
        start_enabled=True,
        stop_enabled=False,
        restart_enabled=True,
    )

    window._sync_tunnel_controls()

    assert window.start_tunnel_button.isEnabled()
    assert not window.stop_tunnel_button.isEnabled()
    assert window.restart_tunnel_button.isEnabled()
    assert window._tray_actions["Start Tunnel"].isEnabled()
    assert not window._tray_actions["Stop Tunnel"].isEnabled()
    assert window._tray_actions["Restart Tunnel"].isEnabled()

    window._bridge_transition = True
    window._tunnel.action_state = SimpleNamespace(
        start_enabled=True,
        stop_enabled=True,
        restart_enabled=True,
    )
    window._sync_tunnel_controls()

    for name in ("start_tunnel_button", "stop_tunnel_button", "restart_tunnel_button"):
        assert not getattr(window, name).isEnabled()
    for name in ("Start Tunnel", "Stop Tunnel", "Restart Tunnel"):
        assert not window._tray_actions[name].isEnabled()


@pytest.mark.parametrize(
    (
        "health",
        "bridge",
        "app",
        "tunnel",
        "runtime",
        "launching",
        "transition",
        "active",
        "exhausted",
        "health_seen",
        "status_seen",
        "expected",
    ),
    (
        (True, True, True, "ready", "external", False, False, False, False, True, True, "● Ready"),
        (
            True,
            True,
            True,
            "checking",
            "external",
            False,
            False,
            False,
            False,
            True,
            True,
            "● Starting",
        ),
        (
            True,
            True,
            True,
            "failed",
            "console_started",
            False,
            False,
            False,
            False,
            True,
            True,
            "● Degraded",
        ),
        (
            False,
            False,
            False,
            "unavailable",
            "launching",
            False,
            False,
            False,
            True,
            True,
            True,
            "● Starting",
        ),
        (
            False,
            False,
            False,
            "unavailable",
            "unavailable",
            False,
            False,
            False,
            True,
            True,
            True,
            "● Error",
        ),
        (
            False,
            False,
            False,
            "unavailable",
            "launch_failed",
            False,
            False,
            False,
            False,
            True,
            True,
            "● Error",
        ),
        (
            False,
            False,
            False,
            "unavailable",
            "unavailable",
            False,
            False,
            False,
            False,
            False,
            False,
            "● Starting",
        ),
        (
            False,
            False,
            False,
            "unavailable",
            "stopped",
            False,
            False,
            False,
            False,
            True,
            False,
            "● Error",
        ),
    ),
)
def test_overall_status_preserves_state_priority(
    health: bool,
    bridge: bool,
    app: bool,
    tunnel: str,
    runtime: str,
    launching: bool,
    transition: bool,
    active: bool,
    exhausted: bool,
    health_seen: bool,
    status_seen: bool,
    expected: str,
) -> None:
    window = FakeWindow()
    window._health_ok = health
    window._bridge_ready = bridge
    window._app_server_ready = app
    window._tunnel_state = tunnel
    window._runtime_state = runtime
    window._launch_in_progress = launching
    window._bridge_transition = transition
    window._managed_start_active = active
    window._bridge_start_retry_exhausted = exhausted
    window._health_observed = health_seen
    window._status_observed = status_seen

    window._sync_overall_status()

    assert window.overall_status_label.text() == expected
    assert window.overall_detail_label.text() == expected


@pytest.mark.parametrize(
    (
        "can_start",
        "health",
        "bridge",
        "app",
        "tunnel",
        "closing",
        "launching",
        "transition",
        "start_expected",
        "retry_expected",
    ),
    (
        (True, False, False, False, "unavailable", False, False, False, True, True),
        (False, False, False, False, "unavailable", False, False, False, False, True),
        (True, True, True, True, "failed", False, False, False, True, True),
        (True, True, True, True, "ready", False, False, False, True, False),
        (True, False, False, False, "ready", True, False, False, True, False),
        (True, False, False, False, "ready", False, True, False, True, False),
        (True, False, False, False, "ready", False, False, True, True, False),
    ),
)
def test_start_and_retry_buttons_keep_their_existing_conditions(
    can_start: bool,
    health: bool,
    bridge: bool,
    app: bool,
    tunnel: str,
    closing: bool,
    launching: bool,
    transition: bool,
    start_expected: bool,
    retry_expected: bool,
) -> None:
    window = FakeWindow()
    window.can_start_bridge = can_start
    window._health_ok = health
    window._bridge_ready = bridge
    window._app_server_ready = app
    window._tunnel_state = tunnel
    window._closing = closing
    window._launch_in_progress = launching
    window._bridge_transition = transition

    window._update_start_button()

    assert window.start_bridge_button.isEnabled() is start_expected
    assert window._tray_actions["Start Bridge"].isEnabled() is start_expected
    assert window.retry_now_button.isEnabled() is retry_expected


def test_empty_state_keeps_unavailable_threads_and_selection_messages() -> None:
    window = FakeWindow()

    window._sync_empty_state()
    assert window.history_pane.empty_state == (
        "CodexBridge is not available on 127.0.0.1:43121\nStart codex-bridge and retry."
    )
    assert window.activity_pane.error == "Bridge unavailable"

    window._bridge_ready = True
    window._sync_empty_state()
    assert window.history_pane.empty_state == "No threads found."
    assert window.activity_pane.empty_state == "No thread selected."

    window.thread_pane.thread_count = 1
    window._sync_empty_state()
    assert window.history_pane.empty_state == "Select a thread to view history."
    assert window.activity_pane.empty_state == "Select a thread to view activity."
