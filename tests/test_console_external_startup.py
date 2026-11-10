from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from codex_bridge.console.config import ConsoleConfig
from codex_bridge.console.external_running_threads import ExternalThreadMonitor
from codex_bridge.console.main_window import MainWindow
from tests.console_main_window_test_support import (
    FakeClient,
    FakeCodexProbe,
    FakeCodexUpdateProbe,
    StableTunnel,
    _application,
)

THREAD_ID = "01a16aba-cdff-7413-90e6-f467224b821d"


def _wait_for(app: QApplication, predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 3
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    assert predicate()


def _rows(window: MainWindow) -> list[str]:
    tree = window.thread_pane.list_widget
    rows = []
    pending = [tree.topLevelItem(index) for index in range(tree.topLevelItemCount())]
    while pending:
        item = pending.pop()
        thread_id = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(thread_id, str):
            rows.append(thread_id)
        pending.extend(item.child(index) for index in range(item.childCount()))
    return rows


@pytest.mark.parametrize("http_before_scan", (True, False))
def test_external_running_section_recovers_on_restart_without_creating_a_thread(
    tmp_path: Path, http_before_scan: bool
) -> None:
    app = _application()
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "codex-home"
    session = home / "sessions" / f"{THREAD_ID}.jsonl"
    session.parent.mkdir(parents=True)
    records = [
        {"type": "session_meta", "payload": {"cwd": str(project), "originator": "Codex Desktop"}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-a"}},
    ]
    session.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    server_threads = {
        "threads": [
            {
                "id": THREAD_ID,
                "name": "External running thread",
                "cwd": str(project),
                "originator": "Codex Desktop",
                "runtime_status": {"type": "notLoaded"},
            }
        ]
    }

    # Both instances have a new scanner/cache, as when Console is restarted.
    for _restart in range(2):
        client = FakeClient()
        monitor = ExternalThreadMonitor(allowed_roots=(str(project),), codex_home=home)
        window = MainWindow(
            ConsoleConfig(allowed_roots=(str(project),)),
            api_client=client,
            codex_probe=FakeCodexProbe(),
            codex_update_probe=FakeCodexUpdateProbe(),
            tunnel_supervisor=StableTunnel(),
            external_thread_monitor=monitor,
            tray_available=False,
        )
        try:
            if http_before_scan:
                client.result("threads", server_threads)
            _wait_for(app, lambda window=window: _rows(window).count(THREAD_ID) == 2)
            if not http_before_scan:
                client.result("threads", server_threads)
            assert _rows(window).count(THREAD_ID) == 2
            assert window.thread_pane.list_widget.topLevelItem(0).text(0) == "Currently running"

            # The HTTP page can temporarily omit an external candidate without
            # deleting the scanner-owned row in either section.
            client.result("threads", {"threads": []})
            assert _rows(window).count(THREAD_ID) == 2
            client.result("threads", server_threads)
            assert _rows(window).count(THREAD_ID) == 2
            assert client.json_posts == []
            assert client.control_requests == []
        finally:
            window.close()
        assert not monitor.is_running

    terminal = {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-a"}}
    with session.open("a", encoding="utf-8") as output:
        output.write(json.dumps(terminal) + "\n")
    client = FakeClient()
    monitor = ExternalThreadMonitor(allowed_roots=(str(project),), codex_home=home)
    window = MainWindow(
        ConsoleConfig(allowed_roots=(str(project),)),
        api_client=client,
        codex_probe=FakeCodexProbe(),
        codex_update_probe=FakeCodexUpdateProbe(),
        tunnel_supervisor=StableTunnel(),
        external_thread_monitor=monitor,
        tray_available=False,
    )
    snapshots = []
    monitor.updated.connect(snapshots.append)
    try:
        client.result("threads", server_threads)
        _wait_for(app, lambda: bool(snapshots))
        assert snapshots[-1] == {}
        assert _rows(window) == [THREAD_ID]
        assert window.thread_pane.list_widget.topLevelItem(0).text(0) != "Currently running"
        assert client.json_posts == []
    finally:
        window.close()
