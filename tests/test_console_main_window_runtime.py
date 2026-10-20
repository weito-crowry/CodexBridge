from __future__ import annotations

from codex_bridge.console import main_window as main_window_module
from codex_bridge.console.codex_resolver import CodexResolution
from codex_bridge.console.config import ConsoleConfig
from codex_bridge.console.main_window import MainWindow
from codex_bridge.console.tunnel_resolver import TunnelCandidate
from codex_bridge.tunnel_profile import TunnelProfileError, TunnelProfileResult
from tests.console_main_window_test_support import (
    FakeClient,
    FakeCodexProbe,
    FakeCodexUpdateProbe,
    FakeLauncher,
    LifecycleTunnel,
    ManagedTunnel,
    RecoveringLifecycleTunnel,
    StableTunnel,
    _application,
    _config,
)


def test_close_stops_timers_and_aborts_only_client_replies() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)

    window.close()

    assert client.aborted_all
    assert not window.health_timer.isActive()
    assert not window.thread_timer.isActive()
    assert not window.selected_status_timer.isActive()
    assert not window._history_refresh_timer.isActive()


def test_existing_ready_bridge_is_external_and_start_is_disabled() -> None:
    _application()
    client = FakeClient()
    probe = FakeCodexProbe()
    launcher = FakeLauncher()
    window = MainWindow(
        _config(),
        api_client=client,
        codex_probe=probe,
        runtime_launcher=launcher,
        tray_available=False,
    )

    probe.result(CodexResolution("C:/Codex/codex.exe", "1.2.3", "codex_app"))
    client.result("health", {"status": "ok"})
    client.result("bridge-status", {"bridge": "ready", "app_server": "ready"})

    assert probe.starts == 1
    assert window.codex_status_label.text() == "Codex: 1.2.3 · codex_app"
    assert window.runtime_state == "external"
    assert window.runtime_status_label.text() == "Runtime: external"
    assert not window.start_bridge_button.isEnabled()
    assert launcher.calls == []
    window.close()


def test_valid_detected_codex_enables_start_when_bridge_is_unavailable() -> None:
    _application()
    client = FakeClient()
    probe = FakeCodexProbe()
    launcher = FakeLauncher()
    window = MainWindow(
        _config(),
        api_client=client,
        codex_probe=probe,
        runtime_launcher=launcher,
        tray_available=False,
    )

    probe.result(CodexResolution("C:/Codex/codex.exe", "1.2.3", "path"))
    client.failure("health", "Bridge unavailable")
    client.failure("bridge-status", "Bridge unavailable")

    assert window.codex_status_label.text() == "Codex: 1.2.3 · path"
    assert window.runtime_state == "launching"
    assert launcher.calls == [("C:/Codex/codex.exe", 8001)]
    assert not window.start_bridge_button.isEnabled()
    window.close()


def test_managed_config_path_is_passed_to_bridge_launcher() -> None:
    _application()
    config = ConsoleConfig(
        allowed_roots=_config().allowed_roots,
        config_path="C:/repo/config.toml",
        config_mode="managed",
    )
    client = FakeClient()
    probe = FakeCodexProbe()
    launcher = FakeLauncher()
    window = MainWindow(
        config,
        api_client=client,
        codex_probe=probe,
        runtime_launcher=launcher,
        tray_available=False,
    )

    probe.result(CodexResolution("C:/Codex/codex.exe", "1.2.3", "path"))
    client.failure("health", "Bridge unavailable")
    client.failure("bridge-status", "Bridge unavailable")

    assert launcher.managed_config_paths == ["C:/repo/config.toml"]
    window.close()


def test_generated_tunnel_profile_is_passed_to_supervisor(tmp_path, monkeypatch) -> None:
    _application()
    config_path = tmp_path / "config.toml"
    profile_path = tmp_path / ".runtime" / "tunnel-profile.yaml"
    profile_settings = {
        "config_version": 1,
        "control_plane": {
            "base_url": "https://control.example.test",
            "tunnel_id": "example-id",
            "api_key": "env:TUNNEL_API_KEY",
        },
    }
    config = ConsoleConfig(
        allowed_roots=_config().allowed_roots,
        config_path=str(config_path),
        config_mode="explicit",
        tunnel_executable="C:/tools/tunnel-client.exe",
        tunnel_executable_source="explicit",
        tunnel_profile_file=str(profile_path),
        tunnel_profile_settings=profile_settings,
    )
    generator_calls = []
    supervisor_options = []
    monkeypatch.setattr(
        main_window_module,
        "enumerate_tunnel_candidates",
        lambda **_kwargs: (TunnelCandidate("C:/tools/tunnel-client.exe", "explicit"),),
    )

    def generate(profile, **kwargs):
        generator_calls.append((profile, kwargs))
        return TunnelProfileResult(profile_path, "tunnel-profile")

    def create_supervisor(**kwargs):
        supervisor_options.append(kwargs)
        return StableTunnel()

    monkeypatch.setattr(main_window_module, "generate_tunnel_profile", generate)
    monkeypatch.setattr(main_window_module, "TunnelSupervisor", create_supervisor)
    window = MainWindow(
        config,
        api_client=FakeClient(),
        codex_probe=FakeCodexProbe(),
        codex_update_probe=FakeCodexUpdateProbe(),
        runtime_launcher=FakeLauncher(),
        tray_available=False,
    )

    assert generator_calls == [
        (
            profile_settings,
            {
                "profile_dir": profile_path.parent,
                "profile_name": "tunnel-profile",
                "client_executable": "C:/tools/tunnel-client.exe",
            },
        )
    ]
    assert supervisor_options[0]["profile_file"] == str(profile_path)
    assert supervisor_options[0]["executable"] == "C:/tools/tunnel-client.exe"
    window.close()


def test_failed_tunnel_profile_generation_disables_tunnel_without_fallback(
    tmp_path, monkeypatch
) -> None:
    _application()
    profile_settings = {
        "config_version": 1,
        "control_plane": {
            "base_url": "https://control.example.test",
            "tunnel_id": "example-id",
            "api_key": "env:TUNNEL_API_KEY",
        },
    }
    config = ConsoleConfig(
        allowed_roots=_config().allowed_roots,
        config_path=str(tmp_path / "config.toml"),
        config_mode="explicit",
        tunnel_executable="C:/tools/tunnel-client.exe",
        tunnel_executable_source="explicit",
        tunnel_profile_file=str(tmp_path / ".runtime" / "tunnel-profile.yaml"),
        tunnel_profile_settings=profile_settings,
    )
    supervisor_options = []
    monkeypatch.setattr(
        main_window_module,
        "enumerate_tunnel_candidates",
        lambda **_kwargs: (TunnelCandidate("C:/tools/tunnel-client.exe", "explicit"),),
    )

    def reject_profile(*_args, **_kwargs):
        raise TunnelProfileError("Tunnel client rejected the staged profile")

    def create_supervisor(**kwargs):
        supervisor_options.append(kwargs)
        return StableTunnel()

    monkeypatch.setattr(main_window_module, "generate_tunnel_profile", reject_profile)
    monkeypatch.setattr(main_window_module, "TunnelSupervisor", create_supervisor)
    window = MainWindow(
        config,
        api_client=FakeClient(),
        codex_probe=FakeCodexProbe(),
        codex_update_probe=FakeCodexUpdateProbe(),
        runtime_launcher=FakeLauncher(),
        tray_available=False,
    )

    assert supervisor_options[0]["executable"] is None
    assert supervisor_options[0]["profile_file"] is None
    assert window._tunnel_resolution_error == "Tunnel client rejected the staged profile"
    window.close()


def test_empty_allowed_roots_keep_start_disabled_even_with_codex_resolution() -> None:
    _application()
    client = FakeClient()
    probe = FakeCodexProbe()
    window = MainWindow(ConsoleConfig(), api_client=client, codex_probe=probe, tray_available=False)
    probe.result(CodexResolution("C:/Codex/codex.exe", "1.2.3", "path"))
    client.failure("health", "Bridge unavailable")
    client.failure("bridge-status", "Bridge unavailable")

    assert not window.start_bridge_button.isEnabled()
    assert "roots" in window.config_status_label.text().casefold()
    window.close()


def test_start_bridge_is_detached_once_and_readiness_marks_console_started() -> None:
    _application()
    client = FakeClient()
    probe = FakeCodexProbe()
    launcher = FakeLauncher()
    window = MainWindow(
        _config(),
        api_client=client,
        codex_probe=probe,
        runtime_launcher=launcher,
        tray_available=False,
    )
    probe.result(CodexResolution("C:/Codex/codex.exe", "1.2.3", "explicit"))
    client.failure("health", "Bridge unavailable")
    client.failure("bridge-status", "Bridge unavailable")

    window.start_bridge_button.click()

    assert window.runtime_state == "launching"
    assert window.runtime_status_label.text() == "Runtime: launching"
    assert not window.start_bridge_button.isEnabled()
    assert not window.retry_now_button.isEnabled()
    assert launcher.calls == [("C:/Codex/codex.exe", 8001)]
    assert [key for key, _, _ in client.requests if key.startswith("launch:")] == [
        "launch:health",
        "launch:status",
    ]

    client.result("launch:health", {"status": "ok"})
    client.result("launch:status", {"bridge": "connected", "app_server": "ready"})

    assert window.runtime_state == "console_started"
    assert window.runtime_status_label.text() == "Runtime: started by Console"
    assert not window.start_bridge_button.isEnabled()
    window.close()


def test_managed_launch_readiness_starts_usage_sequence() -> None:
    window, client, _launcher = _owned_window()

    assert window._usage_ready
    assert window.usage_initial_timer.isActive()
    assert window.usage_poll_timer.isActive()

    window._on_usage_initial_timeout()

    assert sum(key == "usage" for key, _, _ in client.requests) == 1
    window.close()


def test_successful_detached_launch_never_reenables_after_temporary_unavailable() -> None:
    _application()
    client = FakeClient()
    probe = FakeCodexProbe()
    launcher = FakeLauncher()
    window = MainWindow(
        _config(),
        api_client=client,
        codex_probe=probe,
        runtime_launcher=launcher,
        tray_available=False,
    )
    probe.result(CodexResolution("C:/Codex/codex.exe", "1.2.3", "path"))
    client.failure("health", "Bridge unavailable")
    client.failure("bridge-status", "Bridge unavailable")
    window.start_bridge_button.click()

    client.failure("health", "Bridge unavailable")
    client.failure("bridge-status", "Bridge unavailable")

    assert window.runtime_state == "launching"
    assert not window.start_bridge_button.isEnabled()
    window.start_bridge_button.click()
    assert launcher.calls == [("C:/Codex/codex.exe", 8001)]
    window.close()


def test_managed_launch_readiness_timeout_schedules_retry() -> None:
    _application()
    client = FakeClient()
    probe = FakeCodexProbe()
    launcher = FakeLauncher()
    window = MainWindow(
        _config(),
        api_client=client,
        codex_probe=probe,
        runtime_launcher=launcher,
        tray_available=False,
    )
    probe.result(CodexResolution("C:/Codex/codex.exe", "1.2.3", "path"))
    client.failure("health", "Bridge unavailable")
    client.failure("bridge-status", "Bridge unavailable")
    window._readiness_deadline = 0.0
    window._on_readiness_tick()

    assert window.runtime_state == "launch_failed"
    assert window.runtime_status_label.text() == "Runtime: launch failed"
    assert not window.start_bridge_button.isEnabled()
    assert len(launcher.calls) == 1
    assert window.bridge_start_retry_timer.isActive()
    assert window.bridge_start_retry_timer.interval() == 2_000
    window.close()


def test_close_aborts_probe_and_readiness_only_without_stopping_bridge() -> None:
    _application()
    client = FakeClient()
    probe = FakeCodexProbe()
    launcher = FakeLauncher()
    window = MainWindow(
        _config(),
        api_client=client,
        codex_probe=probe,
        runtime_launcher=launcher,
        tray_available=False,
    )
    probe.result(CodexResolution("C:/Codex/codex.exe", "1.2.3", "path"))
    client.failure("health", "Bridge unavailable")
    client.failure("bridge-status", "Bridge unavailable")
    window.start_bridge_button.click()

    window.close()

    assert client.control_requests
    assert not client.aborted_all
    client.control_success("control:shutdown")
    client.failure("health", "Bridge unavailable")
    client.failure("bridge-status", "Bridge unavailable")
    assert client.aborted_all
    assert probe.aborts == 1
    assert launcher.closes == 1
    assert not window.readiness_timer.isActive()


def test_start_stays_disabled_until_both_bridge_observations_confirm_unavailable() -> None:
    _application()
    client = FakeClient()
    probe = FakeCodexProbe()
    launcher = FakeLauncher()
    window = MainWindow(
        _config(),
        api_client=client,
        codex_probe=probe,
        runtime_launcher=launcher,
        tray_available=False,
    )
    probe.result(CodexResolution("C:/Codex/codex.exe", "1.2.3", "path"))

    assert not window.start_bridge_button.isEnabled()
    client.failure("health", "Bridge unavailable")
    assert not window.start_bridge_button.isEnabled()
    client.failure("bridge-status", "Bridge unavailable")
    assert launcher.calls == [("C:/Codex/codex.exe", 8001)]
    assert not window.start_bridge_button.isEnabled()
    window.close()


def test_unavailable_bridge_with_valid_config_auto_starts_once() -> None:
    _application()
    client = FakeClient()
    probe = FakeCodexProbe()
    launcher = FakeLauncher()
    window = MainWindow(
        _config(),
        api_client=client,
        codex_probe=probe,
        runtime_launcher=launcher,
        tray_available=False,
    )

    probe.result(CodexResolution("C:/Codex/codex.exe", "1.2.3", "path"))
    client.failure("health", "Bridge unavailable")
    client.failure("bridge-status", "Bridge unavailable")

    assert launcher.calls == [("C:/Codex/codex.exe", 8001)]
    assert window.runtime_state == "launching"
    window.close()


def test_managed_bridge_launch_failures_use_bounded_retry_schedule() -> None:
    _application()
    client = FakeClient()
    probe = FakeCodexProbe()
    launcher = FakeLauncher(started=False)
    window = MainWindow(
        _config(),
        api_client=client,
        codex_probe=probe,
        runtime_launcher=launcher,
        tray_available=False,
    )

    probe.result(CodexResolution("C:/Codex/codex.exe", "1.2.3", "path"))
    client.failure("health", "Bridge unavailable")
    client.failure("bridge-status", "Bridge unavailable")

    assert len(launcher.calls) == 1
    assert window.bridge_start_retry_timer.isActive()
    assert window.bridge_start_retry_timer.interval() == 2_000

    window._on_bridge_start_retry_timeout()
    assert len(launcher.calls) == 2
    assert window.bridge_start_retry_timer.interval() == 5_000

    window._on_bridge_start_retry_timeout()
    assert len(launcher.calls) == 3
    assert window.bridge_start_retry_timer.interval() == 10_000

    window._on_bridge_start_retry_timeout()
    assert len(launcher.calls) == 4
    assert not window.bridge_start_retry_timer.isActive()
    assert window.runtime_state == "launch_failed"
    window.close()


def test_retry_now_cancels_managed_bridge_wait_and_attempts_immediately() -> None:
    _application()
    client = FakeClient()
    probe = FakeCodexProbe()
    launcher = FakeLauncher(started=False)
    window = MainWindow(
        _config(),
        api_client=client,
        codex_probe=probe,
        runtime_launcher=launcher,
        tray_available=False,
    )

    probe.result(CodexResolution("C:/Codex/codex.exe", "1.2.3", "path"))
    client.failure("health", "Bridge unavailable")
    client.failure("bridge-status", "Bridge unavailable")
    assert window.bridge_start_retry_timer.isActive()

    launcher.started = True
    window._retry_now()

    assert len(launcher.calls) == 2
    assert not window.bridge_start_retry_timer.isActive()
    assert window.runtime_state == "launching"
    window.close()


def _set_ready_components(window: MainWindow, tunnel_state: str = "ready") -> None:
    window._health_ok = True
    window._health_observed = True
    window._status_observed = True
    window._bridge_ready = True
    window._app_server_ready = True
    window._runtime_state = "external"
    window._tunnel_state = tunnel_state


def test_overall_is_starting_during_managed_start_or_tunnel_recovery() -> None:
    _application()
    tunnel = LifecycleTunnel("starting")
    window = MainWindow(
        _config(),
        api_client=FakeClient(),
        tunnel_supervisor=tunnel,
        tray_available=False,
    )

    _set_ready_components(window, tunnel_state="starting")
    window._sync_overall_status()
    assert window.overall_status_label.text() == "● Starting"

    window._runtime_state = "launching"
    window._health_ok = False
    window._sync_overall_status()
    assert window.overall_status_label.text() == "● Starting"
    window.close()


def test_overall_is_ready_only_when_bridge_app_server_and_tunnel_are_usable() -> None:
    _application()
    window = MainWindow(
        _config(),
        api_client=FakeClient(),
        tunnel_supervisor=LifecycleTunnel("ready"),
        tray_available=False,
    )

    _set_ready_components(window)
    window._sync_overall_status()
    assert window.overall_status_label.text() == "● Ready"

    window._app_server_ready = False
    window._sync_overall_status()
    assert window.overall_status_label.text() != "● Ready"
    window.close()


def test_tunnel_failure_with_local_bridge_ready_is_degraded() -> None:
    _application()
    tunnel = RecoveringLifecycleTunnel("failed")
    tunnel.recovery_timer.start(60_000)
    window = MainWindow(
        _config(),
        api_client=FakeClient(),
        tunnel_supervisor=tunnel,
        tray_available=False,
    )

    _set_ready_components(window, tunnel_state="failed")
    window._runtime_state = "console_started"
    window._sync_overall_status()

    assert window.overall_status_label.text() == "● Degraded"
    window.close()


def test_usage_failure_does_not_change_ready_to_degraded() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(
        _config(),
        api_client=client,
        tunnel_supervisor=LifecycleTunnel("ready"),
        tray_available=False,
    )
    _set_ready_components(window)
    window._sync_overall_status()

    client.failure("usage", "Usage unavailable")

    assert window.overall_status_label.text() == "● Ready"
    window.close()


def test_update_check_failure_does_not_change_ready_to_degraded() -> None:
    _application()
    updates = FakeCodexUpdateProbe()
    window = MainWindow(
        _config(),
        api_client=FakeClient(),
        codex_update_probe=updates,
        tunnel_supervisor=LifecycleTunnel("ready"),
        tray_available=False,
    )
    _set_ready_components(window)
    window._sync_overall_status()

    updates.failure()

    assert window.overall_status_label.text() == "● Ready"
    window.close()


def test_retry_now_routes_to_bridge_when_bridge_unavailable() -> None:
    _application()
    client = FakeClient()
    probe = FakeCodexProbe()
    launcher = FakeLauncher(started=False)
    window = MainWindow(
        _config(),
        api_client=client,
        codex_probe=probe,
        runtime_launcher=launcher,
        tunnel_supervisor=LifecycleTunnel("unavailable"),
        tray_available=False,
    )
    probe.result(CodexResolution("C:/Codex/codex.exe", "1.2.3", "path"))
    client.failure("health", "Bridge unavailable")
    client.failure("bridge-status", "Bridge unavailable")
    assert len(launcher.calls) == 1

    window._retry_now()

    assert len(launcher.calls) == 2
    window.close()


def test_retry_now_routes_to_tunnel_when_bridge_ready_but_tunnel_unavailable() -> None:
    _application()
    tunnel = LifecycleTunnel("failed")
    window = MainWindow(
        _config(),
        api_client=FakeClient(),
        tunnel_supervisor=tunnel,
        tray_available=False,
    )
    _set_ready_components(window, tunnel_state="failed")
    window._runtime_state = "external"
    window._sync_overall_status()

    window._retry_now()

    assert tunnel.retry_calls == 1
    window.close()


def test_restart_codexbridge_uses_existing_managed_restart_path() -> None:
    window, client, launcher = _owned_window()

    assert window.restart_codexbridge_button.isEnabled()
    window.restart_codexbridge_button.click()

    assert window.runtime_state == "restarting"
    assert not window.retry_now_button.isEnabled()
    assert len(client.control_requests) == 1
    assert len(launcher.calls) == 1
    window.close()


def test_restart_codexbridge_is_disabled_for_ready_external_bridge() -> None:
    _application()
    client = FakeClient()
    probe = FakeCodexProbe()
    window = MainWindow(
        _config(),
        api_client=client,
        codex_probe=probe,
        tunnel_supervisor=LifecycleTunnel("ready"),
        tray_available=False,
    )
    probe.result(CodexResolution("C:/Codex/codex.exe", "1.2.3", "path"))
    client.result("health", {"status": "ok"})
    client.result("bridge-status", {"bridge": "ready", "app_server": "ready"})

    assert not window.restart_codexbridge_button.isEnabled()
    assert "external" in window.restart_codexbridge_button.toolTip().casefold()
    window.close()


def test_advanced_controls_are_hidden_by_default_and_preserve_individual_actions() -> None:
    _application()
    window = MainWindow(_config(), api_client=FakeClient(), tray_available=False)

    assert window.advanced_controls.isHidden()
    assert window.start_bridge_button.parentWidget() is window.advanced_controls
    assert window.start_tunnel_button.parentWidget() is window.advanced_controls

    window.advanced_toggle_button.click()

    assert not window.advanced_controls.isHidden()
    assert window.start_bridge_button.text() == "Start Bridge"
    assert window.stop_tunnel_button.text() == "Stop Tunnel"
    window.close()


def _owned_window(
    tunnel: StableTunnel | None = None,
) -> tuple[MainWindow, FakeClient, FakeLauncher]:
    _application()
    client = FakeClient()
    probe = FakeCodexProbe()
    launcher = FakeLauncher()
    tunnel = tunnel or StableTunnel()
    window = MainWindow(
        _config(),
        api_client=client,
        codex_probe=probe,
        runtime_launcher=launcher,
        tunnel_supervisor=tunnel,
        tray_available=False,
    )
    probe.result(CodexResolution("C:/Codex/codex.exe", "1.2.3", "path"))
    client.failure("health", "Bridge unavailable")
    client.failure("bridge-status", "Bridge unavailable")
    window.start_bridge_button.click()
    client.result("launch:health", {"status": "ok"})
    client.result("launch:status", {"bridge": "ready", "app_server": "ready"})
    return window, client, launcher


def test_owned_ready_bridge_enables_stop_and_restart_with_process_local_token() -> None:
    window, client, launcher = _owned_window()

    assert window.runtime_state == "console_started"
    assert window.stop_bridge_button.isEnabled()
    assert window.restart_bridge_button.isEnabled()
    assert len(launcher.control_tokens) == 1
    assert len(launcher.control_tokens[0]) >= 32
    assert client.control_requests == []

    window.stop_bridge_button.click()

    assert window.runtime_state == "stopping"
    assert len(client.control_requests) == 1
    assert not window.stop_bridge_button.isEnabled()
    window.close()


def test_external_bridge_keeps_bridge_controls_disabled() -> None:
    _application()
    client = FakeClient()
    probe = FakeCodexProbe()
    launcher = FakeLauncher()
    window = MainWindow(
        _config(),
        api_client=client,
        codex_probe=probe,
        runtime_launcher=launcher,
        tunnel_supervisor=StableTunnel(),
        tray_available=False,
    )
    probe.result(CodexResolution("C:/Codex/codex.exe", "1.2.3", "path"))
    client.result("health", {"status": "ok"})
    client.result("bridge-status", {"bridge": "ready", "app_server": "ready"})

    assert window.runtime_state == "external"
    assert not window.start_bridge_button.isEnabled()
    assert not window.stop_bridge_button.isEnabled()
    assert not window.restart_bridge_button.isEnabled()
    assert launcher.calls == []
    window.close()


def _fail_bridge_observation(client: FakeClient) -> None:
    client.failure("health", "Bridge unavailable")
    client.failure("bridge-status", "Bridge unavailable")


def test_one_failed_bridge_status_observation_does_not_restart_owned_bridge() -> None:
    window, client, launcher = _owned_window()

    client.failure("health", "Bridge unavailable")
    assert window._bridge_loss_count == 0
    client.failure("bridge-status", "Bridge unavailable")

    assert window._bridge_loss_count == 1
    assert client.control_requests == []
    assert len(launcher.calls) == 1
    window.close()


def test_two_failed_bridge_status_observations_restart_owned_bridge() -> None:
    window, client, launcher = _owned_window()

    _fail_bridge_observation(client)
    _fail_bridge_observation(client)

    assert window._bridge_loss_count == 2
    assert window.runtime_state == "restarting"
    assert len(client.control_requests) == 1
    assert len(launcher.calls) == 1
    window.close()


def test_successful_bridge_status_observation_resets_loss_counter() -> None:
    window, client, _launcher = _owned_window()

    _fail_bridge_observation(client)
    client.result("health", {"status": "ok"})
    client.result("bridge-status", {"bridge": "ready", "app_server": "ready"})
    _fail_bridge_observation(client)

    assert window._bridge_loss_count == 1
    assert client.control_requests == []
    window.close()


def test_two_failed_external_bridge_status_observations_take_over_without_shutdown() -> None:
    _application()
    client = FakeClient()
    probe = FakeCodexProbe()
    launcher = FakeLauncher()
    window = MainWindow(
        _config(),
        api_client=client,
        codex_probe=probe,
        runtime_launcher=launcher,
        tunnel_supervisor=StableTunnel(),
        tray_available=False,
    )
    probe.result(CodexResolution("C:/Codex/codex.exe", "1.2.3", "path"))
    client.result("health", {"status": "ok"})
    client.result("bridge-status", {"bridge": "ready", "app_server": "ready"})

    _fail_bridge_observation(client)
    _fail_bridge_observation(client)

    assert window._bridge_loss_count == 2
    assert window.runtime_state == "launching"
    assert len(launcher.calls) == 1
    assert client.control_requests == []
    assert window._detached_launch_started
    window.close()


def test_stop_requires_bridge_disappearance_after_202_and_clears_ownership() -> None:
    window, client, launcher = _owned_window()
    old_token = launcher.control_tokens[0]

    window.stop_bridge_button.click()
    client.control_success("control:shutdown")

    assert window.runtime_state == "stopping"
    assert window._control_token == old_token
    client.failure("health", "Bridge unavailable")
    assert window.runtime_state == "stopping"
    client.failure("bridge-status", "Bridge unavailable")

    assert window.runtime_state == "stopped"
    assert window._control_token is None
    assert window._detached_pid is None
    assert not window._detached_launch_started
    assert window.start_bridge_button.isEnabled()
    window.start_bridge_button.click()

    assert len(launcher.control_tokens) == 2
    assert launcher.control_tokens[1] != old_token
    window.close()


def test_stop_timeout_is_fail_closed_and_does_not_clear_token_or_relaunch() -> None:
    window, client, launcher = _owned_window()

    window.stop_bridge_button.click()
    client.control_success("control:shutdown")
    window._stop_confirmation_deadline = 0.0
    window._on_stop_confirmation_tick()

    assert window.runtime_state == "stop_timed_out"
    assert window._control_token is not None
    assert window._detached_launch_started
    assert len(launcher.calls) == 1
    assert client.control_requests == [(window._control_token, "control:shutdown")]
    window.close()


def test_launch_timeout_recovers_to_owned_ready_on_late_normal_poll() -> None:
    _application()
    client = FakeClient()
    probe = FakeCodexProbe()
    launcher = FakeLauncher()
    window = MainWindow(
        _config(),
        api_client=client,
        codex_probe=probe,
        runtime_launcher=launcher,
        tunnel_supervisor=StableTunnel(),
        tray_available=False,
    )
    probe.result(CodexResolution("C:/Codex/codex.exe", "1.2.3", "path"))
    client.failure("health", "Bridge unavailable")
    client.failure("bridge-status", "Bridge unavailable")
    window.start_bridge_button.click()
    old_token = launcher.control_tokens[0]

    window._readiness_deadline = 0.0
    window._on_readiness_tick()

    assert window.runtime_state == "launch_failed"
    assert not window._bridge_transition
    assert not window.start_bridge_button.isEnabled()
    assert len(launcher.calls) == 1
    assert window.bridge_start_retry_timer.isActive()

    client.result("health", {"status": "ok"})
    client.result("bridge-status", {"bridge": "ready", "app_server": "ready"})

    assert window.runtime_state == "console_started"
    assert window.stop_bridge_button.isEnabled()
    assert window.restart_bridge_button.isEnabled()
    assert len(launcher.calls) == 1
    assert window._control_token == old_token
    window.close()


def test_stop_timeout_recovers_to_owned_ready_without_relaunch() -> None:
    window, client, launcher = _owned_window()
    old_token = launcher.control_tokens[0]

    window.stop_bridge_button.click()
    client.control_success("control:shutdown")
    window._stop_confirmation_deadline = 0.0
    window._on_stop_confirmation_tick()

    assert window.runtime_state == "stop_timed_out"
    client.result("health", {"status": "ok"})
    client.result("bridge-status", {"bridge": "ready", "app_server": "ready"})

    assert window.runtime_state == "console_started"
    assert window._control_token == old_token
    assert len(launcher.calls) == 1
    assert window.stop_bridge_button.isEnabled()
    assert window.restart_bridge_button.isEnabled()
    window.close()


def test_stop_timeout_late_unavailable_confirms_stopped_and_allows_start() -> None:
    window, client, launcher = _owned_window()

    window.stop_bridge_button.click()
    client.control_success("control:shutdown")
    window._stop_confirmation_deadline = 0.0
    window._on_stop_confirmation_tick()

    client.failure("health", "Bridge unavailable")
    client.failure("bridge-status", "Bridge unavailable")

    assert window.runtime_state == "stopped"
    assert window._control_token is None
    assert window._detached_pid is None
    assert not window._detached_launch_started
    assert window.start_bridge_button.isEnabled()
    assert len(launcher.calls) == 1
    window.close()


def test_restart_timeout_late_stop_does_not_automatically_relaunch() -> None:
    tunnel = ManagedTunnel()
    window, client, launcher = _owned_window(tunnel)

    window.restart_bridge_button.click()
    client.control_success("control:shutdown")
    window._stop_confirmation_deadline = 0.0
    window._on_stop_confirmation_tick()

    client.failure("health", "Bridge unavailable")
    client.failure("bridge-status", "Bridge unavailable")

    assert window.runtime_state == "stopped"
    assert len(launcher.calls) == 1
    assert tunnel.started == 0
    assert window.start_bridge_button.isEnabled()
    window.close()


def test_unexpected_unreachable_owned_bridge_does_not_reenable_start() -> None:
    window, client, _launcher = _owned_window()

    client.failure("health", "Bridge unavailable")
    client.failure("bridge-status", "Bridge unavailable")

    assert window.runtime_state == "console_started_unreachable"
    assert not window.start_bridge_button.isEnabled()
    assert not window.stop_bridge_button.isEnabled()
    assert not window.restart_bridge_button.isEnabled()
    window.close()


def test_control_failure_does_not_trigger_duplicate_launch() -> None:
    window, client, launcher = _owned_window()

    window.stop_bridge_button.click()
    client.control_failure("control:shutdown")

    assert window.runtime_state == "control_failed"
    assert len(launcher.calls) == 1
    assert not window.start_bridge_button.isEnabled()
    window.close()


def test_restart_relaunches_after_confirmed_stop_with_fresh_token() -> None:
    window, client, launcher = _owned_window()
    old_token = launcher.control_tokens[0]

    window.restart_bridge_button.click()
    assert window.runtime_state == "restarting"
    client.control_success("control:shutdown")
    client.failure("health", "Bridge unavailable")
    client.failure("bridge-status", "Bridge unavailable")

    assert len(launcher.calls) == 2
    assert launcher.control_tokens[1] != old_token
    assert window.runtime_state == "launching"
    client.result("launch:health", {"status": "ok"})
    client.result("launch:status", {"bridge": "ready", "app_server": "ready"})

    assert window.runtime_state == "console_started"
    assert window.stop_bridge_button.isEnabled()
    window.close()


def test_restart_restores_managed_tunnel_once_after_new_bridge_ready() -> None:
    tunnel = ManagedTunnel()
    window, client, launcher = _owned_window(tunnel)

    window.restart_bridge_button.click()
    assert tunnel.stop_calls == 1
    client.control_success("control:shutdown")
    client.failure("health", "Bridge unavailable")
    client.failure("bridge-status", "Bridge unavailable")
    client.result("launch:health", {"status": "ok"})
    client.result("launch:status", {"bridge": "ready", "app_server": "ready"})

    assert tunnel.started == 1
    assert len(client.control_requests) == 1
    assert len(launcher.control_tokens) == 2
    window.close()


def test_restart_with_stopped_tunnel_does_not_start_it_after_readiness() -> None:
    window, client, launcher = _owned_window()

    window.restart_bridge_button.click()
    client.control_success("control:shutdown")
    client.failure("health", "Bridge unavailable")
    client.failure("bridge-status", "Bridge unavailable")
    client.result("launch:health", {"status": "ok"})
    client.result("launch:status", {"bridge": "ready", "app_server": "ready"})

    assert window._tunnel.started == 0
    assert len(launcher.control_tokens) == 2
    window.close()


def test_restart_relaunch_failure_does_not_start_managed_tunnel() -> None:
    tunnel = ManagedTunnel()
    window, client, launcher = _owned_window(tunnel)
    launcher.started = False

    window.restart_bridge_button.click()
    client.control_success("control:shutdown")
    client.failure("health", "Bridge unavailable")
    client.failure("bridge-status", "Bridge unavailable")

    assert window.runtime_state == "launch_failed"
    assert tunnel.started == 0
    assert len(launcher.control_tokens) == 2
    window.close()
