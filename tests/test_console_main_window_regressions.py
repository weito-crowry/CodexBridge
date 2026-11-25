from __future__ import annotations

from PySide6.QtCore import QTimer

from codex_bridge.console import main_window_layout as main_window_layout_module
from codex_bridge.console.config import ConsoleConfig
from codex_bridge.console.main_window import MainWindow
from tests.console_main_window_test_support import (
    FakeClient,
    FakeCodexProbe,
    FakeCodexUpdateProbe,
    FakeLauncher,
    StableTunnel,
    _application,
    _config,
)


def _window(
    config: ConsoleConfig | None = None,
    *,
    client: FakeClient | None = None,
) -> MainWindow:
    return MainWindow(config or _config(), api_client=client or FakeClient(), tray_available=False)


def test_repeated_main_window_construction_does_not_reset_application_style(monkeypatch) -> None:
    _application()
    style_calls: list[str] = []

    class ApplicationSpy:
        @classmethod
        def instance(cls):
            return cls.application

        def setStyle(self, style: str) -> None:
            style_calls.append(style)

    ApplicationSpy.application = ApplicationSpy()
    monkeypatch.setattr(main_window_layout_module, "QApplication", ApplicationSpy, raising=False)

    first = _window()
    second = _window()
    try:
        assert style_calls == []
    finally:
        first.close()
        second.close()


def test_fake_client_supports_control_json_request_and_response_contract() -> None:
    client = FakeClient()
    successes: list[tuple[str, object]] = []
    failures: list[tuple[str, str]] = []
    client.composer_json_succeeded.connect(lambda key, payload: successes.append((key, payload)))
    client.composer_json_failed.connect(lambda key, message: failures.append((key, message)))

    assert client.post_control_json(
        "/ui-api/control/account/auth/refresh", {}, "test-token", key="auth:refresh"
    )
    assert client.control_json_posts == [
        ("/ui-api/control/account/auth/refresh", {}, "test-token", "auth:refresh")
    ]
    client.control_json_result("auth:refresh", {"status": "authenticated"})
    assert successes == [("auth:refresh", {"status": "authenticated"})]

    assert client.post_control_json(
        "/ui-api/control/account/auth/login/cancel",
        {"login_revision": 1},
        "test-token",
        key="auth:cancel",
    )
    client.control_json_failure("auth:cancel", "request failed")
    assert failures == [("auth:cancel", "request failed")]


def test_fake_client_window_defaults_to_inert_process_helpers() -> None:
    application = _application()
    assert application.style().objectName().casefold() == "fusion"
    window = _window()
    probe = window._codex_probe
    tunnel = window._tunnel
    launcher = window._launcher
    update_probe = window._codex_update_probe

    assert isinstance(probe, FakeCodexProbe)
    assert probe.starts == 1
    assert isinstance(update_probe, FakeCodexUpdateProbe)
    assert isinstance(tunnel, StableTunnel)
    assert isinstance(launcher, FakeLauncher)

    window.close()

    assert probe.aborts == 1
    assert tunnel.closed == 1
    assert launcher.closes == 1
    assert not window.health_timer.isActive()


def test_main_window_shutdown_stops_timers_aborts_replies_and_update_probe() -> None:
    _application()
    client = FakeClient()
    window = _window(client=client)
    update_probe = window._codex_update_probe
    update_probe.busy = True
    timers = [
        timer for timer in window.findChildren(QTimer) if isinstance(timer.parent(), MainWindow)
    ]
    for index, timer in enumerate(timers):
        timer.setObjectName(f"test-timer-{index}")
        timer.start(60_000)

    window.close()

    assert client.aborted_all
    assert update_probe.aborts == 1
    active_timers = [timer for timer in timers if timer.isActive()]
    active_names = [(timer.objectName(), type(timer.parent()).__name__) for timer in active_timers]
    assert not active_timers, active_names
