from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pytest

from codex_bridge.workflows import diagnostic_log

RUN_ID = "run-0123456789abcdef0123456789abcdef"
WORKFLOW_ID = "01234567-89ab-4cde-8012-3456789abcde"
LAUNCH_ID = "a" * 32


def test_lifecycle_events_are_bounded_allowlisted_and_append_in_order(tmp_path: Path) -> None:
    assert diagnostic_log.append_lifecycle_event(
        tmp_path,
        source="bridge",
        event="worker_launch_requested",
        run_id=RUN_ID,
        workflow_id=WORKFLOW_ID,
        launch_id=LAUNCH_ID,
        mode="recover",
    )
    assert diagnostic_log.append_lifecycle_event(
        tmp_path,
        source="bridge",
        event="worker_spawn_succeeded",
        run_id=RUN_ID,
        workflow_id=WORKFLOW_ID,
        launch_id=LAUNCH_ID,
        mode="recover",
        pid=1234,
    )

    path = tmp_path / "bridge-lifecycle.jsonl"
    raw_lines = path.read_bytes().splitlines()
    assert all(len(line) + 1 <= 2048 for line in raw_lines)
    events = [json.loads(line) for line in raw_lines]
    assert [event["event"] for event in events] == [
        "worker_launch_requested",
        "worker_spawn_succeeded",
    ]
    for event in events:
        assert event["version"] == 1
        assert event["source"] == "bridge"
        assert event["run_id"] == RUN_ID
        assert event["workflow_id"] == WORKFLOW_ID
        assert event["launch_id"] == LAUNCH_ID
        timestamp = datetime.fromisoformat(event["timestamp_utc"].replace("Z", "+00:00"))
        assert timestamp.utcoffset().total_seconds() == 0
        assert len(event["timestamp_utc"].split(".")[-1].removesuffix("Z")) == 3
    assert events[1]["pid"] == 1234
    assert not (tmp_path / "worker-lifecycle.jsonl").exists()


def test_lifecycle_event_rejects_secret_like_identifiers_and_exception_text(
    tmp_path: Path,
) -> None:
    secret = "sk-secret-token prompt input result body Authorization"

    written = diagnostic_log.append_lifecycle_event(
        tmp_path,
        source="worker",
        event="worker_failed",
        run_id=RUN_ID,
        workflow_id=secret,
        mode="start",
        pid=1234,
        reason_code="workflow_wait_failed",
        exception_class=f"RuntimeError:{secret}",
    )

    assert written is False
    for path in tmp_path.glob("*-lifecycle.jsonl"):
        assert secret.encode() not in path.read_bytes()


def test_concurrent_lifecycle_appends_remain_complete_jsonl_records(tmp_path: Path) -> None:
    def append(index: int) -> bool:
        return diagnostic_log.append_lifecycle_event(
            tmp_path,
            source="bridge",
            event="request_reserved",
            run_id=RUN_ID,
            workflow_id=WORKFLOW_ID,
            request_digest=f"{index:064x}",
        )

    with ThreadPoolExecutor(max_workers=8) as workers:
        outcomes = list(workers.map(append, range(40)))

    lines = (tmp_path / "bridge-lifecycle.jsonl").read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in lines]
    assert all(outcomes)
    assert len(events) == 40
    assert {event["request_digest"] for event in events} == {f"{i:064x}" for i in range(40)}


def test_lifecycle_writer_does_not_follow_symlink(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    target = tmp_path / "target.jsonl"
    target.write_text("preserve\n", encoding="utf-8")
    link = run_dir / "bridge-lifecycle.jsonl"
    try:
        link.symlink_to(target)
    except OSError as error:
        pytest.skip(f"Windows symlink privilege unavailable: {type(error).__name__}")

    assert not diagnostic_log.append_lifecycle_event(
        run_dir,
        source="bridge",
        event="request_reserved",
        run_id=RUN_ID,
        workflow_id=WORKFLOW_ID,
        request_digest="a" * 64,
    )
    assert target.read_text(encoding="utf-8") == "preserve\n"


def test_lifecycle_writer_treats_an_occupied_directory_as_a_best_effort_failure(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "worker-lifecycle.jsonl").mkdir()
    assert not diagnostic_log.append_lifecycle_event(
        run_dir,
        source="worker",
        event="worker_bootstrap",
        run_id=RUN_ID,
        mode="start",
        pid=1234,
    )


def test_lifecycle_write_failure_and_size_limit_are_best_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_open(_run_dir: Path, _log_path: Path) -> int:
        raise PermissionError("test-only diagnostic write failure")

    monkeypatch.setattr(diagnostic_log, "_open_append_file", fail_open)
    assert not diagnostic_log.append_lifecycle_event(
        tmp_path,
        source="bridge",
        event="request_reserved",
        run_id=RUN_ID,
        workflow_id=WORKFLOW_ID,
        request_digest="a" * 64,
    )

    monkeypatch.undo()
    monkeypatch.setattr(diagnostic_log, "_MAX_FILE_BYTES", 1)
    assert not diagnostic_log.append_lifecycle_event(
        tmp_path,
        source="bridge",
        event="request_reserved",
        run_id=RUN_ID,
        workflow_id=WORKFLOW_ID,
        request_digest="a" * 64,
    )
    assert (tmp_path / "bridge-lifecycle.jsonl").stat().st_size == 0
