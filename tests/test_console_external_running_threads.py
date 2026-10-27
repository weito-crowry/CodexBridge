from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from threading import Event, get_ident

import pytest
from PySide6.QtWidgets import QApplication

from codex_bridge.console import external_running_threads


def _scanner_class():
    scanner_class = getattr(external_running_threads, "CodexSessionScanner", None)
    assert scanner_class is not None
    return scanner_class


def _monitor_class():
    monitor_class = getattr(external_running_threads, "ExternalThreadMonitor", None)
    assert monitor_class is not None
    return monitor_class


def _record(record: dict[str, object]) -> bytes:
    return (json.dumps(record) + "\n").encode("utf-8")


def _metadata(cwd: Path, *, originator: str = "codex_desktop") -> dict[str, object]:
    return {
        "type": "session_meta",
        "payload": {"cwd": str(cwd), "originator": originator},
    }


def _event(event_type: str, turn_id: str | None = None) -> dict[str, object]:
    payload: dict[str, object] = {"type": event_type}
    if turn_id is not None:
        payload["turn_id"] = turn_id
    return {"type": "event_msg", "payload": payload}


def _session_file(root: Path, thread_id: str) -> Path:
    folder = root / "sessions" / "2026" / "10" / "24"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"rollout-2026-10-24T00-00-00-{thread_id}.jsonl"


def _scanner(tmp_path: Path, *roots: Path):
    return _scanner_class()(
        allowed_roots=tuple(str(root) for root in roots), codex_home=tmp_path / "codex-home"
    )


def _write_session(
    path: Path,
    cwd: Path,
    *events: dict[str, object],
    originator: str = "codex_desktop",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        _record(_metadata(cwd, originator=originator))
        + b"".join(_record(event) for event in events)
    )


THREAD_A = "01a16aba-cdff-7413-90e6-f467224b821d"
THREAD_B = "01a16aba-cdff-7413-90e6-f467224b821e"


def test_task_started_without_a_terminal_event_is_estimated_running(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    scanner = _scanner(tmp_path, project)
    _write_session(
        _session_file(tmp_path / "codex-home", THREAD_A),
        project,
        _event("task_started", "turn-a"),
    )

    assert scanner.scan() == {
        THREAD_A: {"id": THREAD_A, "cwd": str(project.resolve()), "originator": "codex_desktop"}
    }


@pytest.mark.parametrize("originator", ("codexbridge", " CodexBridge ", "CODEXBRIDGE"))
def test_codexbridge_unterminated_turn_is_not_an_external_candidate(
    tmp_path: Path, originator: str
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    scanner = _scanner(tmp_path, project)
    _write_session(
        _session_file(tmp_path / "codex-home", THREAD_A),
        project,
        _event("task_started", "turn-a"),
        originator=originator,
    )

    assert scanner.scan() == {}


def test_non_event_msg_payload_does_not_start_a_turn(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    scanner = _scanner(tmp_path, project)
    _write_session(
        _session_file(tmp_path / "codex-home", THREAD_A),
        project,
        {"type": "response_item", "payload": {"type": "task_started", "turn_id": "turn-a"}},
    )

    assert scanner.scan() == {}


def test_initial_scan_parses_only_metadata_and_relevant_event_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    scanner = _scanner(tmp_path, project)
    path = _session_file(tmp_path / "codex-home", THREAD_A)
    filler = _record({"type": "history_item", "payload": {"text": "ordinary history"}})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        _record(_metadata(project)) + filler * 2_000 + _record(_event("task_started", "turn-a"))
    )
    real_loads = external_running_threads.json.loads
    parsed_lines = 0

    def count_json_loads(value: object, *args: object, **kwargs: object):
        nonlocal parsed_lines
        parsed_lines += 1
        return real_loads(value, *args, **kwargs)

    monkeypatch.setattr(external_running_threads.json, "loads", count_json_loads)

    assert set(scanner.scan()) == {THREAD_A}
    assert parsed_lines < 10


@pytest.mark.parametrize(
    "terminal_event", ("task_complete", "turn_complete", "turn_aborted", "turn_interrupted")
)
def test_matching_terminal_and_interruption_events_clear_running(
    tmp_path: Path, terminal_event: str
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    scanner = _scanner(tmp_path, project)
    _write_session(
        _session_file(tmp_path / "codex-home", THREAD_A),
        project,
        _event("task_started", "turn-a"),
        _event(terminal_event, "turn-a"),
    )

    assert scanner.scan() == {}


def test_latest_turn_controls_state_and_old_terminal_does_not_clear_it(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    scanner = _scanner(tmp_path, project)
    _write_session(
        _session_file(tmp_path / "codex-home", THREAD_A),
        project,
        _event("task_started", "old"),
        _event("task_complete", "old"),
        _event("turn_started", "new"),
        _event("turn_complete", "old"),
    )
    _write_session(
        _session_file(tmp_path / "codex-home", THREAD_B),
        project,
        _event("task_started", "old"),
        _event("turn_started", "new"),
        _event("turn_complete", "new"),
    )

    assert set(scanner.scan()) == {THREAD_A}


def test_sessions_are_independent_and_limited_to_existing_allowed_projects(tmp_path: Path) -> None:
    allowed_a = tmp_path / "allowed-a"
    allowed_b = tmp_path / "allowed-b"
    outside = tmp_path / "outside"
    allowed_a.mkdir()
    allowed_b.mkdir()
    outside.mkdir()
    scanner = _scanner(tmp_path, allowed_a, allowed_b)
    home = tmp_path / "codex-home"
    _write_session(_session_file(home, THREAD_A), allowed_a, _event("task_started", "turn-a"))
    _write_session(_session_file(home, THREAD_B), allowed_b, _event("task_started", "turn-b"))
    outside_id = "01a16aba-cdff-7413-90e6-f467224b821f"
    _write_session(_session_file(home, outside_id), outside, _event("task_started", "turn-x"))

    assert {row["cwd"] for row in scanner.scan().values()} == {
        str(allowed_a.resolve()),
        str(allowed_b.resolve()),
    }


def test_new_session_file_is_discovered_on_a_later_scan(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    scanner = _scanner(tmp_path, project)

    assert scanner.scan() == {}
    _write_session(
        _session_file(tmp_path / "codex-home", THREAD_A),
        project,
        _event("turn_started", "turn-a"),
    )

    assert set(scanner.scan()) == {THREAD_A}


@pytest.mark.parametrize("use_environment", (False, True))
def test_codex_home_uses_environment_or_user_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, use_environment: bool
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "configured-home" if use_environment else tmp_path / "user-home" / ".codex"
    _write_session(
        _session_file(home, THREAD_A),
        project,
        _event("task_started", "turn-a"),
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "user-home")
    if use_environment:
        monkeypatch.setenv("CODEX_HOME", str(home))
    else:
        monkeypatch.delenv("CODEX_HOME", raising=False)
    scanner = _scanner_class()(allowed_roots=(str(project),))

    assert set(scanner.scan()) == {THREAD_A}


def test_incomplete_last_line_is_reparsed_after_append(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    scanner = _scanner(tmp_path, project)
    path = _session_file(tmp_path / "codex-home", THREAD_A)
    started = json.dumps(_event("task_started", "turn-a")).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_record(_metadata(project)) + started[:20])

    assert scanner.scan() == {}
    with path.open("ab") as session:
        session.write(started[20:] + b"\n")

    assert set(scanner.scan()) == {THREAD_A}


@pytest.mark.parametrize("replace_file", (False, True))
def test_truncated_or_replaced_session_is_reparsed(tmp_path: Path, replace_file: bool) -> None:
    project = tmp_path / "project"
    project.mkdir()
    scanner = _scanner(tmp_path, project)
    path = _session_file(tmp_path / "codex-home", THREAD_A)
    _write_session(
        path,
        project,
        _event("task_started", "turn-a"),
        {"type": "ignored", "payload": {"padding": "x" * 4000}},
    )
    assert set(scanner.scan()) == {THREAD_A}

    content = _record(_metadata(project)) + _record(_event("task_started", "turn-a"))
    content += _record(_event("turn_complete", "turn-a"))
    if replace_file:
        replacement = path.with_suffix(".replacement")
        replacement.write_bytes(content)
        os.replace(replacement, path)
    else:
        path.write_bytes(content)

    assert scanner.scan() == {}


def test_unchanged_session_is_not_reopened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    scanner = _scanner(tmp_path, project)
    path = _session_file(tmp_path / "codex-home", THREAD_A)
    _write_session(path, project, _event("task_started", "turn-a"))
    scanner.scan()

    opened: list[Path] = []
    real_open = Path.open

    def track_open(self: Path, *args: object, **kwargs: object):
        if self == path:
            opened.append(self)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", track_open)
    scanner.scan()

    assert opened == []


def test_corrupt_rows_missing_files_and_invalid_session_root_do_not_crash(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    scanner = _scanner(tmp_path, project)
    path = _session_file(tmp_path / "codex-home", THREAD_A)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_record(_metadata(project)) + b"{broken json}\n")

    assert scanner.scan() == {}
    path.unlink()
    assert scanner.scan() == {}
    invalid = _scanner_class()(allowed_roots=(str(project),), codex_home=tmp_path / "missing")
    assert invalid.scan() == {}


class _BlockingScanner:
    def __init__(self) -> None:
        self.started = Event()
        self.scan_threads: list[int] = []

    def scan(self, stop_event: threading.Event) -> dict[str, dict[str, object]]:
        self.scan_threads.append(get_ident())
        self.started.set()
        stop_event.wait(2)
        return {THREAD_A: {"id": THREAD_A, "cwd": "C:/project"}}


def test_monitor_scans_off_gui_without_overlap_and_suppresses_results_after_stop() -> None:
    app = QApplication.instance() or QApplication([])
    assert app is not None
    scanner = _BlockingScanner()
    monitor = _monitor_class()(allowed_roots=(), scanner=scanner)
    updates: list[object] = []
    monitor.updated.connect(updates.append)
    gui_thread = get_ident()

    monitor.request_scan()
    assert scanner.started.wait(2)
    monitor.request_scan()
    assert len(scanner.scan_threads) == 1
    monitor.stop()
    app.processEvents()

    assert scanner.scan_threads[0] != gui_thread
    assert updates == []
    assert not monitor.is_running


def test_monitor_turns_scanner_errors_into_empty_updates() -> None:
    app = QApplication.instance() or QApplication([])
    assert app is not None

    class BrokenScanner:
        def scan(self, _stop_event: threading.Event) -> dict[str, dict[str, object]]:
            raise OSError("private file details must not escape")

    monitor = _monitor_class()(allowed_roots=(), scanner=BrokenScanner())
    updates: list[object] = []
    monitor.updated.connect(updates.append)
    monitor.request_scan()
    deadline = time.monotonic() + 2
    while not updates and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    monitor.stop()

    assert updates == [{}]
