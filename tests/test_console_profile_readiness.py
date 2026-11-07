from __future__ import annotations

import json
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from PySide6.QtCore import QThread, QTimer

from codex_bridge.console import main_window as main_window_module
from codex_bridge.console.bridge_runtime_observer import advance_bridge_generation
from codex_bridge.console.main_window import MainWindow
from codex_bridge.console.tunnel_profile_job import TunnelProfileJob
from codex_bridge.console.tunnel_resolver import TunnelCandidate
from tests.console_main_window_test_support import (
    FakeClient,
    FakeCodexProbe,
    FakeCodexUpdateProbe,
    StableTunnel,
    _application,
)
from tests.test_console_main_window_runtime import _managed_tunnel_config, _report_bridge_ready


def _wait_for(predicate: Callable[[], bool]) -> None:
    app = _application()
    deadline = time.monotonic() + 3
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    assert predicate()


def test_slow_profile_doctor_keeps_gui_responsive_and_tunnel_waiting(tmp_path, monkeypatch):
    app = _application()
    entered = threading.Event()
    release = threading.Event()
    worker_threads = []
    readiness = []

    def doctor(args, **_kwargs):
        worker_threads.append(QThread.currentThread())
        entered.set()
        release.wait(0.7)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr("codex_bridge.tunnel_profile.subprocess.run", doctor)
    monkeypatch.setattr(
        main_window_module,
        "enumerate_tunnel_candidates",
        lambda **_kwargs: (TunnelCandidate("fake-tunnel.exe", "explicit"),),
    )
    tunnel = StableTunnel()
    tunnel.set_bridge_ready = readiness.append
    monkeypatch.setattr(main_window_module, "TunnelSupervisor", lambda **_kwargs: tunnel)
    config, profile_path, _ = _managed_tunnel_config(tmp_path)
    client = FakeClient()
    window = MainWindow(
        config,
        api_client=client,
        codex_probe=FakeCodexProbe(),
        codex_update_probe=FakeCodexUpdateProbe(),
        tray_available=False,
    )
    heartbeat = []
    try:
        started = time.monotonic()
        _report_bridge_ready(client)
        elapsed = time.monotonic() - started
        assert elapsed < 0.2, f"GUI readiness callback blocked for {elapsed:.3f}s"
        assert entered.wait(1)
        assert len(worker_threads) == 1
        assert worker_threads[0] != app.thread()
        QTimer.singleShot(0, lambda: heartbeat.append(True))
        _wait_for(lambda: bool(heartbeat))
        assert window._tunnel_profile_status == "running"
        assert not profile_path.exists()
        assert not any(readiness)
        _report_bridge_ready(client)
        assert len(worker_threads) == 1
        release.set()
        _wait_for(lambda: window._tunnel_profile_status == "passed")
        assert profile_path.is_file()
        assert readiness[-1] is True
        assert client.json_posts == []
        assert client.control_requests == []
    finally:
        release.set()
        window.close()


def test_stale_profile_job_cannot_replace_new_generation_profile(tmp_path, monkeypatch):
    _application()
    entered_a = threading.Event()
    release_a = threading.Event()
    readiness = []

    def doctor(args, **_kwargs):
        staged_path = Path(args[3])
        staged = json.loads(staged_path.read_text(encoding="utf-8"))
        generation = staged["control_plane"]["tunnel_id"]
        if generation == "generation-A":
            entered_a.set()
            assert release_a.wait(2)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr("codex_bridge.tunnel_profile.subprocess.run", doctor)
    monkeypatch.setattr(
        main_window_module,
        "enumerate_tunnel_candidates",
        lambda **_kwargs: (TunnelCandidate("fake-tunnel.exe", "explicit"),),
    )
    tunnel = StableTunnel()
    tunnel.set_bridge_ready = readiness.append
    monkeypatch.setattr(main_window_module, "TunnelSupervisor", lambda **_kwargs: tunnel)
    config, profile_path, profile_a = _managed_tunnel_config(tmp_path)
    profile_a["control_plane"]["tunnel_id"] = "generation-A"
    client = FakeClient()
    window = MainWindow(
        config,
        api_client=client,
        codex_probe=FakeCodexProbe(),
        codex_update_probe=FakeCodexUpdateProbe(),
        tray_available=False,
    )
    try:
        _report_bridge_ready(client)
        assert entered_a.wait(1)

        advance_bridge_generation(window)
        profile_b = {
            **profile_a,
            "control_plane": {**profile_a["control_plane"], "tunnel_id": "generation-B"},
        }
        window._managed_tunnel_profile = (
            profile_b,
            *window._managed_tunnel_profile[1:],
        )
        _report_bridge_ready(client)
        _wait_for(lambda: window._tunnel_profile_status == "passed")

        assert (
            json.loads(profile_path.read_text(encoding="utf-8"))["control_plane"]["tunnel_id"]
            == "generation-B"
        )
        assert readiness[-1] is True
        readiness_count = len(readiness)

        release_a.set()
        _wait_for(lambda: not window.findChildren(TunnelProfileJob))

        assert (
            json.loads(profile_path.read_text(encoding="utf-8"))["control_plane"]["tunnel_id"]
            == "generation-B"
        )
        assert window._tunnel_profile_status == "passed"
        assert len(readiness) == readiness_count
        assert list(profile_path.parent.glob(f"{profile_path.stem}.*.yaml")) == []
    finally:
        release_a.set()
        window.close()


def test_profile_install_succeeds_again_after_generation_advance(tmp_path, monkeypatch):
    _application()
    monkeypatch.setattr(
        "codex_bridge.tunnel_profile.subprocess.run",
        lambda args, **_kwargs: subprocess.CompletedProcess(args, 0),
    )
    monkeypatch.setattr(
        main_window_module,
        "enumerate_tunnel_candidates",
        lambda **_kwargs: (TunnelCandidate("fake-tunnel.exe", "explicit"),),
    )
    tunnel = StableTunnel()
    monkeypatch.setattr(main_window_module, "TunnelSupervisor", lambda **_kwargs: tunnel)
    config, profile_path, profile_a = _managed_tunnel_config(tmp_path)
    profile_a["control_plane"]["tunnel_id"] = "generation-A"
    client = FakeClient()
    window = MainWindow(
        config,
        api_client=client,
        codex_probe=FakeCodexProbe(),
        codex_update_probe=FakeCodexUpdateProbe(),
        tray_available=False,
    )
    try:
        _report_bridge_ready(client)
        _wait_for(lambda: window._tunnel_profile_status == "passed")
        assert (
            json.loads(profile_path.read_text(encoding="utf-8"))["control_plane"]["tunnel_id"]
            == "generation-A"
        )

        advance_bridge_generation(window)
        profile_b = {
            **profile_a,
            "control_plane": {**profile_a["control_plane"], "tunnel_id": "generation-B"},
        }
        window._managed_tunnel_profile = (
            profile_b,
            *window._managed_tunnel_profile[1:],
        )
        _report_bridge_ready(client)
        _wait_for(lambda: window._tunnel_profile_status == "passed")

        assert (
            json.loads(profile_path.read_text(encoding="utf-8"))["control_plane"]["tunnel_id"]
            == "generation-B"
        )
        _wait_for(lambda: not window.findChildren(TunnelProfileJob))
    finally:
        window.close()


def test_stale_profile_job_cannot_replace_profile_after_new_generation_fails(tmp_path, monkeypatch):
    _application()
    entered_a = threading.Event()
    release_a = threading.Event()

    def doctor(args, **_kwargs):
        profile = json.loads(Path(args[3]).read_text(encoding="utf-8"))
        if profile["control_plane"]["tunnel_id"] == "generation-A":
            entered_a.set()
            assert release_a.wait(2)
            return subprocess.CompletedProcess(args, 0)
        return subprocess.CompletedProcess(args, 3)

    monkeypatch.setattr("codex_bridge.tunnel_profile.subprocess.run", doctor)
    monkeypatch.setattr(
        main_window_module,
        "enumerate_tunnel_candidates",
        lambda **_kwargs: (TunnelCandidate("fake-tunnel.exe", "explicit"),),
    )
    tunnel = StableTunnel()
    readiness = []
    tunnel.set_bridge_ready = readiness.append
    monkeypatch.setattr(main_window_module, "TunnelSupervisor", lambda **_kwargs: tunnel)
    config, profile_path, profile_a = _managed_tunnel_config(tmp_path)
    profile_a["control_plane"]["tunnel_id"] = "generation-A"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text("previous-valid-profile\n", encoding="utf-8")
    client = FakeClient()
    window = MainWindow(
        config,
        api_client=client,
        codex_probe=FakeCodexProbe(),
        codex_update_probe=FakeCodexUpdateProbe(),
        tray_available=False,
    )
    try:
        _report_bridge_ready(client)
        assert entered_a.wait(1)

        advance_bridge_generation(window)
        profile_b = {
            **profile_a,
            "control_plane": {**profile_a["control_plane"], "tunnel_id": "generation-B"},
        }
        window._managed_tunnel_profile = (
            profile_b,
            *window._managed_tunnel_profile[1:],
        )
        _report_bridge_ready(client)
        _wait_for(lambda: window._tunnel_profile_status == "failed")
        assert profile_path.read_text(encoding="utf-8") == "previous-valid-profile\n"
        assert not any(readiness)
        error_b = window._tunnel_resolution_error
        assert error_b is not None

        release_a.set()
        _wait_for(lambda: not window.findChildren(TunnelProfileJob))

        assert profile_path.read_text(encoding="utf-8") == "previous-valid-profile\n"
        assert window._tunnel_profile_status == "failed"
        assert window._tunnel_resolution_error == error_b
        assert not any(readiness)
        assert list(profile_path.parent.glob(f"{profile_path.stem}.*.yaml")) == []
    finally:
        release_a.set()
        window.close()


def test_console_close_invalidates_inflight_profile_install(tmp_path, monkeypatch):
    app = _application()
    entered = threading.Event()
    release = threading.Event()

    def doctor(args, **_kwargs):
        entered.set()
        assert release.wait(2)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr("codex_bridge.tunnel_profile.subprocess.run", doctor)
    monkeypatch.setattr(
        main_window_module,
        "enumerate_tunnel_candidates",
        lambda **_kwargs: (TunnelCandidate("fake-tunnel.exe", "explicit"),),
    )
    monkeypatch.setattr(main_window_module, "TunnelSupervisor", lambda **_kwargs: StableTunnel())
    config, profile_path, _settings = _managed_tunnel_config(tmp_path)
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text("previous-valid-profile\n", encoding="utf-8")
    client = FakeClient()
    window = MainWindow(
        config,
        api_client=client,
        codex_probe=FakeCodexProbe(),
        codex_update_probe=FakeCodexUpdateProbe(),
        tray_available=False,
    )
    try:
        _report_bridge_ready(client)
        assert entered.wait(1)
        window.close()
        assert window._closing
        release.set()
        deadline = time.monotonic() + 3
        while (
            list(profile_path.parent.glob(f"{profile_path.stem}.*.yaml"))
            and time.monotonic() < deadline
        ):
            app.processEvents()
            time.sleep(0.005)

        assert profile_path.read_text(encoding="utf-8") == "previous-valid-profile\n"
        assert list(profile_path.parent.glob(f"{profile_path.stem}.*.yaml")) == []
    finally:
        release.set()
        window.close()


@pytest.mark.parametrize("change", ["generation", "unreachable", "transition", "close"])
def test_late_profile_result_does_not_start_tunnel_after_runtime_changes(
    tmp_path, monkeypatch, change
):
    _application()
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    readiness = []

    def generate(*_args, **_kwargs):
        entered.set()
        release.wait(2)
        finished.set()

    monkeypatch.setattr(main_window_module, "generate_tunnel_profile", generate)
    monkeypatch.setattr(
        main_window_module,
        "enumerate_tunnel_candidates",
        lambda **_kwargs: (TunnelCandidate("fake-tunnel.exe", "explicit"),),
    )
    tunnel = StableTunnel()
    tunnel.set_bridge_ready = readiness.append
    monkeypatch.setattr(main_window_module, "TunnelSupervisor", lambda **_kwargs: tunnel)
    config, _, _ = _managed_tunnel_config(tmp_path)
    client = FakeClient()
    window = MainWindow(
        config,
        api_client=client,
        codex_probe=FakeCodexProbe(),
        codex_update_probe=FakeCodexUpdateProbe(),
        tray_available=False,
    )
    try:
        _report_bridge_ready(client)
        assert entered.wait(1)
        if change == "generation":
            advance_bridge_generation(window)
        elif change == "unreachable":
            window._health_ok = False
        elif change == "transition":
            window._bridge_transition = True
        else:
            window.close()
        release.set()
        assert finished.wait(1)
        # Wait for the queued completion, including suppressed stale results.
        _wait_for(lambda: not window.findChildren(TunnelProfileJob))
        assert not any(readiness)
        assert client.json_posts == []
        assert client.control_requests == []
        if change == "generation":
            assert window._tunnel_profile_status == "not_run"
        elif change == "close":
            assert window._tunnel_profile_status == "running"
    finally:
        release.set()
        window.close()
