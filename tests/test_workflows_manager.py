from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

from codex_bridge.runtime_mode import INTERNAL_RUNTIME_FLAG, WORKFLOW_WORKER_FLAG
from codex_bridge.workflows.codex_job_adapter import CodexJobAdapter
from codex_bridge.workflows.http_client import CONTROL_TOKEN_ENV
from codex_bridge.workflows.interpreter import SAFE_CODEX_JOB_PROMPT
from codex_bridge.workflows.manager import WorkflowManagerError, WorkflowWorkerManager

_DUMMY_TOKEN = "phase5b-test-token-never-persist"


class FakeProcess:
    def __init__(self, *, exit_code: int | None = None) -> None:
        self.pid = 43210
        self.returncode = exit_code
        self._exited = asyncio.Event()
        self.terminated = False
        self.killed = False
        if exit_code is not None:
            self._exited.set()

    async def wait(self) -> int:
        await self._exited.wait()
        assert self.returncode is not None
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15
        self._exited.set()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._exited.set()


def _write_manifest(run_dir: Path, *, live: bool = False) -> None:
    (run_dir / "manifest.json").write_text(
        json.dumps({"control": {"codex_job_live": live}}),
        encoding="utf-8",
    )


def test_manager_creation_and_run_directory_do_not_start_a_worker(tmp_path: Path) -> None:
    manager = WorkflowWorkerManager(state_root=tmp_path / "state")

    first = manager.create_run_directory()
    second = manager.create_run_directory()

    assert first.parent == second.parent == tmp_path / "state"
    assert first.is_dir() and second.is_dir() and first != second
    assert manager.get_status(first.name) is None


@pytest.mark.asyncio
async def test_shutdown_preserves_turns_from_durable_requests_after_manager_recreation(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "workflow-state"
    run_id = "run-0123456789abcdef0123456789abcdef"
    workflow_id = str(uuid.uuid4())
    request_digest = "a" * 64
    run_dir = state_root / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "workflow_id": workflow_id,
                "run_id": run_id,
                "request_digest": request_digest,
                "mode": "codex-job-live",
                "control": {"codex_job_live": True},
            }
        ),
        encoding="utf-8",
    )
    adapter = CodexJobAdapter(run_dir / "codex_jobs.sqlite")
    prepared = adapter.prepare_job(
        workflow_id,
        "do[0].calculate",
        SAFE_CODEX_JOB_PROMPT,
        run_dir,
        port=18001,
    )
    assert adapter.claim_post(prepared["job_id"])
    adapter.mark_accepted(
        prepared["job_id"], "workflow-thread-123", "workflow-turn-123", "in_progress"
    )

    requests_db = sqlite3.connect(state_root / "requests.sqlite")
    requests_db.execute(
        "CREATE TABLE workflow_requests (client_request_id TEXT, workflow_id TEXT, "
        "run_id TEXT, request_digest TEXT, state TEXT, created_at TEXT, updated_at TEXT)"
    )
    requests_db.execute(
        "INSERT INTO workflow_requests VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "phase5c-request-durable",
            workflow_id,
            run_id,
            request_digest,
            "running",
            "2026-11-11T00:00:00+00:00",
            "2026-11-11T00:00:00+00:00",
        ),
    )
    requests_db.commit()
    requests_db.close()

    manager_after_restart = WorkflowWorkerManager(state_root=state_root)
    report = await manager_after_restart.shutdown()

    assert report.ownership_complete
    assert report.owned_turns == {("workflow-thread-123", "workflow-turn-123")}


def test_default_state_root_uses_localappdata_without_eager_directory_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_app_data = tmp_path / "local-app-data"
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))

    manager = WorkflowWorkerManager()

    assert manager.state_root == local_app_data / "CodexBridge" / "workflows"
    assert not local_app_data.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(("exit_code", "expected_state"), [(0, "completed"), (7, "failed")])
async def test_worker_starts_only_on_explicit_call_and_reports_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exit_code: int,
    expected_state: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    process = FakeProcess()
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def create_process(*args: object, **kwargs: object) -> FakeProcess:
        calls.append((args, kwargs))
        return process

    monkeypatch.setattr(
        "codex_bridge.workflows.manager.asyncio.create_subprocess_exec", create_process
    )
    manager = WorkflowWorkerManager(
        state_root=tmp_path / "state",
        allowed_roots=(str(workspace),),
        python_executable=sys.executable,
        frozen=False,
    )
    run_dir = manager.create_run_directory()
    _write_manifest(run_dir)
    assert calls == []

    handle = await manager.start_worker(run_dir, mode="start")
    process.returncode = exit_code
    process._exited.set()
    status = await handle.wait()

    assert len(calls) == 1
    command = calls[0][0]
    assert command[:3] == (sys.executable, "-m", "codex_bridge.workflows.worker_entry")
    assert _DUMMY_TOKEN not in repr(command)
    assert status.pid == process.pid
    assert status.returncode == exit_code
    assert status.state == expected_state
    events = [
        json.loads(line)
        for line in (run_dir / "bridge-lifecycle.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [event["event"] for event in events] == [
        "worker_launch_requested",
        "worker_spawn_succeeded",
        "worker_exited",
    ]
    launch_ids = {event["launch_id"] for event in events}
    assert len(launch_ids) == 1 and len(next(iter(launch_ids))) == 32
    assert events[1]["pid"] == process.pid
    assert events[2]["returncode"] == exit_code
    assert events[2]["state"] == expected_state


@pytest.mark.asyncio
async def test_live_worker_gets_token_only_in_child_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    process = FakeProcess(exit_code=0)
    captured: dict[str, object] = {}

    async def create_process(*args: object, **kwargs: object) -> FakeProcess:
        captured.update(kwargs)
        captured["command"] = args
        return process

    monkeypatch.setattr(
        "codex_bridge.workflows.manager.asyncio.create_subprocess_exec", create_process
    )
    manager = WorkflowWorkerManager(
        state_root=tmp_path / "state",
        allowed_roots=(str(workspace),),
        control_token=_DUMMY_TOKEN,
        python_executable=sys.executable,
        frozen=False,
    )
    run_dir = manager.create_run_directory()
    _write_manifest(run_dir, live=True)

    handle = await manager.start_worker(
        run_dir,
        mode="start",
        workspace_root=workspace,
    )
    await handle.wait()

    command = captured["command"]
    environment = captured["env"]
    assert isinstance(command, tuple)
    assert isinstance(environment, dict)
    assert _DUMMY_TOKEN not in repr(command)
    assert environment[CONTROL_TOKEN_ENV] == _DUMMY_TOKEN
    config_argument = command[command.index("--config") + 1]
    config_text = Path(str(config_argument)).read_text(encoding="utf-8")
    assert _DUMMY_TOKEN not in config_text
    assert "stdout" in captured and "stderr" in captured
    assert "PATH" in environment
    assert "SystemRoot" in environment


@pytest.mark.asyncio
async def test_live_worker_without_token_or_allowed_workspace_is_rejected_before_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = tmp_path / "state" / "run-0123456789abcdef0123456789abcdef"
    run_dir.mkdir(parents=True)
    _write_manifest(run_dir, live=True)
    calls: list[object] = []

    async def create_process(*args: object, **kwargs: object) -> FakeProcess:
        calls.append(args)
        return FakeProcess(exit_code=0)

    monkeypatch.setattr(
        "codex_bridge.workflows.manager.asyncio.create_subprocess_exec", create_process
    )
    manager = WorkflowWorkerManager(
        state_root=run_dir.parent,
        allowed_roots=(str(tmp_path),),
        python_executable=sys.executable,
        frozen=False,
    )

    with pytest.raises(WorkflowManagerError, match="control token"):
        await manager.start_worker(run_dir, mode="start", workspace_root=workspace)
    assert calls == []


@pytest.mark.asyncio
async def test_shutdown_is_bounded_and_terminates_only_managed_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    process = FakeProcess()

    async def create_process(*_args: object, **_kwargs: object) -> FakeProcess:
        return process

    monkeypatch.setattr(
        "codex_bridge.workflows.manager.asyncio.create_subprocess_exec", create_process
    )
    manager = WorkflowWorkerManager(
        state_root=tmp_path / "state",
        allowed_roots=(str(workspace),),
        control_token=_DUMMY_TOKEN,
        python_executable=sys.executable,
        frozen=False,
        graceful_shutdown_seconds=0.05,
    )
    run_dir = manager.create_run_directory()
    _write_manifest(run_dir, live=True)
    CodexJobAdapter(run_dir / "codex_jobs.sqlite")
    await manager.start_worker(run_dir, mode="start", workspace_root=workspace)

    report = await asyncio.wait_for(manager.shutdown(), timeout=1.0)

    assert process.terminated
    assert report.ownership_complete
    assert list(run_dir.glob("worker-shutdown-*.request"))
    assert manager.get_status(run_dir.name).state == "stopped"
    events = [
        json.loads(line)
        for line in (run_dir / "bridge-lifecycle.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert "shutdown_signal_requested" in [event["event"] for event in events]
    assert "worker_terminate_requested" in [event["event"] for event in events]
    exited = next(event for event in events if event["event"] == "worker_exited")
    assert exited["shutdown_requested"] is True
    assert exited["state"] == "stopped"


@pytest.mark.asyncio
async def test_shutdown_kill_escalation_and_unknown_exit_are_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class StubbornProcess(FakeProcess):
        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    process = StubbornProcess()

    async def create_process(*_args: object, **_kwargs: object) -> StubbornProcess:
        return process

    monkeypatch.setattr(
        "codex_bridge.workflows.manager.asyncio.create_subprocess_exec", create_process
    )
    manager = WorkflowWorkerManager(
        state_root=tmp_path / "state",
        allowed_roots=(str(workspace),),
        graceful_shutdown_seconds=0.05,
    )
    run_dir = manager.create_run_directory()
    _write_manifest(run_dir)
    await manager.start_worker(run_dir, mode="start")

    await manager.shutdown()

    events = [
        json.loads(line)
        for line in (run_dir / "bridge-lifecycle.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    event_names = [event["event"] for event in events]
    assert process.terminated and process.killed
    assert "worker_terminate_requested" in event_names
    assert "worker_kill_requested" in event_names
    shutdown_failed = next(event for event in events if event["event"] == "worker_shutdown_failed")
    assert shutdown_failed["reason_code"] == "worker_did_not_exit"
    assert "worker_exited" not in event_names


@pytest.mark.asyncio
async def test_shutdown_signal_failure_is_recorded_before_terminate_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "shutdown signal path token"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    process = FakeProcess()

    async def create_process(*_args: object, **_kwargs: object) -> FakeProcess:
        return process

    def fail_signal(_path: Path) -> None:
        raise OSError(secret)

    monkeypatch.setattr(
        "codex_bridge.workflows.manager.asyncio.create_subprocess_exec", create_process
    )
    monkeypatch.setattr("codex_bridge.workflows.manager._write_shutdown_signal", fail_signal)
    manager = WorkflowWorkerManager(
        state_root=tmp_path / "state",
        allowed_roots=(str(workspace),),
        graceful_shutdown_seconds=0.05,
    )
    run_dir = manager.create_run_directory()
    _write_manifest(run_dir)
    await manager.start_worker(run_dir, mode="start")

    await manager.shutdown()

    log_text = (run_dir / "bridge-lifecycle.jsonl").read_text(encoding="utf-8")
    events = [json.loads(line) for line in log_text.splitlines()]
    failure = next(event for event in events if event["event"] == "shutdown_signal_failed")
    assert failure["reason_code"] == "shutdown_signal_write_failed"
    assert failure["exception_class"] == "OSError"
    assert "worker_terminate_requested" in [event["event"] for event in events]
    assert events[-1]["event"] == "worker_exited"
    assert events[-1]["state"] == "stopped"
    assert secret not in log_text


@pytest.mark.asyncio
async def test_shutdown_waits_for_inflight_spawn_before_completing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    process = FakeProcess()
    spawn_entered = asyncio.Event()
    release_spawn = asyncio.Event()

    async def create_process(*_args: object, **_kwargs: object) -> FakeProcess:
        spawn_entered.set()
        await release_spawn.wait()
        return process

    monkeypatch.setattr(
        "codex_bridge.workflows.manager.asyncio.create_subprocess_exec", create_process
    )
    manager = WorkflowWorkerManager(
        state_root=tmp_path / "state",
        allowed_roots=(str(workspace),),
        control_token=_DUMMY_TOKEN,
        graceful_shutdown_seconds=0.05,
    )
    run_dir = manager.create_run_directory()
    _write_manifest(run_dir, live=True)
    CodexJobAdapter(run_dir / "codex_jobs.sqlite")

    start_task = asyncio.create_task(
        manager.start_worker(run_dir, mode="start", workspace_root=workspace)
    )
    await spawn_entered.wait()
    shutdown_task = asyncio.create_task(manager.shutdown())
    await asyncio.sleep(0)
    shutdown_completed_before_spawn_release = shutdown_task.done()

    release_spawn.set()
    handle = await start_task
    report = await shutdown_task
    worker_was_stopped_by_shutdown = process.terminated
    status_after_shutdown = handle.status
    if not process.terminated:
        process.terminate()
    await handle.wait()

    assert not shutdown_completed_before_spawn_release
    assert worker_was_stopped_by_shutdown
    assert status_after_shutdown.state == "stopped"
    assert report.ownership_complete
    assert report.owned_turns == frozenset()


@pytest.mark.asyncio
async def test_start_during_shutdown_is_rejected_without_spawning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import codex_bridge.workflows.manager as manager_module

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    process = FakeProcess()
    spawn_calls = 0
    shutdown_signal_written = asyncio.Event()
    write_signal = manager_module._write_shutdown_signal

    async def create_process(*_args: object, **_kwargs: object) -> FakeProcess:
        nonlocal spawn_calls
        spawn_calls += 1
        return process

    def write_shutdown_signal(path: Path) -> None:
        write_signal(path)
        shutdown_signal_written.set()

    monkeypatch.setattr(
        "codex_bridge.workflows.manager.asyncio.create_subprocess_exec", create_process
    )
    monkeypatch.setattr(
        "codex_bridge.workflows.manager._write_shutdown_signal", write_shutdown_signal
    )
    manager = WorkflowWorkerManager(
        state_root=tmp_path / "state",
        allowed_roots=(str(workspace),),
        graceful_shutdown_seconds=0.05,
    )
    active_run = manager.create_run_directory()
    next_run = manager.create_run_directory()
    _write_manifest(active_run)
    _write_manifest(next_run)
    await manager.start_worker(active_run, mode="start")

    shutdown_task = asyncio.create_task(manager.shutdown())
    await shutdown_signal_written.wait()

    with pytest.raises(WorkflowManagerError, match="shutting down"):
        await manager.start_worker(next_run, mode="start")
    report = await shutdown_task

    assert spawn_calls == 1
    assert report.ownership_complete
    assert manager.get_status(active_run.name).state == "stopped"
    assert manager.get_status(next_run.name) is None


@pytest.mark.asyncio
async def test_concurrent_shutdown_calls_share_one_shutdown_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import codex_bridge.workflows.manager as manager_module

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    process = FakeProcess()
    signals: list[Path] = []
    write_signal = manager_module._write_shutdown_signal

    async def create_process(*_args: object, **_kwargs: object) -> FakeProcess:
        return process

    def write_shutdown_signal(path: Path) -> None:
        signals.append(path)
        write_signal(path)

    monkeypatch.setattr(
        "codex_bridge.workflows.manager.asyncio.create_subprocess_exec", create_process
    )
    monkeypatch.setattr(
        "codex_bridge.workflows.manager._write_shutdown_signal", write_shutdown_signal
    )
    manager = WorkflowWorkerManager(
        state_root=tmp_path / "state",
        allowed_roots=(str(workspace),),
        graceful_shutdown_seconds=0.05,
    )
    run_dir = manager.create_run_directory()
    _write_manifest(run_dir)
    await manager.start_worker(run_dir, mode="start")

    first = asyncio.create_task(manager.shutdown())
    second = asyncio.create_task(manager.shutdown())
    first_report, second_report = await asyncio.gather(first, second)

    assert len(signals) == 1
    assert first_report is second_report
    assert process.terminated
    assert manager.get_status(run_dir.name).state == "stopped"


@pytest.mark.asyncio
async def test_live_worker_shutdown_reports_unknown_when_ledger_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    async def create_process(*_args: object, **_kwargs: object) -> FakeProcess:
        return FakeProcess(exit_code=0)

    monkeypatch.setattr(
        "codex_bridge.workflows.manager.asyncio.create_subprocess_exec", create_process
    )
    manager = WorkflowWorkerManager(
        state_root=tmp_path / "state",
        allowed_roots=(str(workspace),),
        control_token=_DUMMY_TOKEN,
        python_executable=sys.executable,
        frozen=False,
        graceful_shutdown_seconds=0.05,
    )
    run_dir = manager.create_run_directory()
    _write_manifest(run_dir, live=True)
    await manager.start_worker(run_dir, mode="start", workspace_root=workspace)

    report = await manager.shutdown()

    assert report.owned_turns == frozenset()
    assert not report.ownership_complete
    assert report.errors == ("workflow turn ownership is incomplete",)


@pytest.mark.asyncio
async def test_frozen_worker_uses_its_dedicated_executable_flag_and_minimal_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    process = FakeProcess(exit_code=0)
    captured: dict[str, object] = {}

    async def create_process(*args: object, **kwargs: object) -> FakeProcess:
        captured["command"] = args
        captured["env"] = kwargs["env"]
        return process

    monkeypatch.setattr(
        "codex_bridge.workflows.manager.asyncio.create_subprocess_exec", create_process
    )
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-inherited")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-be-inherited")
    manager = WorkflowWorkerManager(
        state_root=tmp_path / "state",
        allowed_roots=(str(workspace),),
        python_executable=str(tmp_path / "CodexBridge.exe"),
        frozen=True,
    )
    run_dir = manager.create_run_directory()
    _write_manifest(run_dir)

    await manager.start_worker(run_dir, mode="recover")

    command = captured["command"]
    environment = captured["env"]
    assert isinstance(command, tuple) and isinstance(environment, dict)
    assert command[:2] == (str(tmp_path / "CodexBridge.exe"), WORKFLOW_WORKER_FLAG)
    assert INTERNAL_RUNTIME_FLAG not in command
    assert "OPENAI_API_KEY" not in environment
    assert "UNRELATED_SECRET" not in environment


@pytest.mark.asyncio
async def test_worker_start_failure_has_bounded_status_without_error_text_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def create_process(*_args: object, **_kwargs: object) -> FakeProcess:
        raise OSError(_DUMMY_TOKEN)

    monkeypatch.setattr(
        "codex_bridge.workflows.manager.asyncio.create_subprocess_exec", create_process
    )
    manager = WorkflowWorkerManager(
        state_root=tmp_path / "state",
        python_executable=sys.executable,
        frozen=False,
    )
    run_dir = manager.create_run_directory()
    _write_manifest(run_dir)

    with pytest.raises(WorkflowManagerError, match="could not be started"):
        await manager.start_worker(run_dir, mode="start")

    status = manager.get_status(run_dir.name)
    assert status is not None and status.state == "failed"
    assert _DUMMY_TOKEN not in repr(status)
    log_path = run_dir / "bridge-lifecycle.jsonl"
    log_text = log_path.read_text(encoding="utf-8")
    events = [json.loads(line) for line in log_text.splitlines()]
    assert [event["event"] for event in events] == [
        "worker_launch_requested",
        "worker_spawn_failed",
    ]
    assert events[-1]["exception_class"] == "OSError"
    assert events[-1]["reason_code"] == "spawn_os_error"
    assert _DUMMY_TOKEN not in log_text
