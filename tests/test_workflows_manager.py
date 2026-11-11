from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from codex_bridge.runtime_mode import INTERNAL_RUNTIME_FLAG, WORKFLOW_WORKER_FLAG
from codex_bridge.workflows.codex_job_adapter import CodexJobAdapter
from codex_bridge.workflows.http_client import CONTROL_TOKEN_ENV
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


def test_default_state_root_uses_localappdata_without_eager_directory_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_app_data = tmp_path / "local-app-data"
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))

    manager = WorkflowWorkerManager()

    assert manager.state_root == local_app_data / "CodexBridge" / "workflows"
    assert not local_app_data.exists()


@pytest.mark.asyncio
async def test_worker_starts_only_on_explicit_call_and_reports_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    process.returncode = 0
    process._exited.set()
    status = await handle.wait()

    assert len(calls) == 1
    command = calls[0][0]
    assert command[:3] == (sys.executable, "-m", "codex_bridge.workflows.worker_entry")
    assert _DUMMY_TOKEN not in repr(command)
    assert status.pid == process.pid
    assert status.returncode == 0
    assert status.state == "completed"


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
