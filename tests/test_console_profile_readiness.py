from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Callable

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
