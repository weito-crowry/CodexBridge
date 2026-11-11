from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

import pytest

from codex_bridge.workflows import manager as manager_module
from codex_bridge.workflows import service as service_module
from codex_bridge.workflows.interpreter import SAFE_CODEX_JOB_PROMPT
from codex_bridge.workflows.manager import WorkflowWorkerManager
from codex_bridge.workflows.service import (
    WorkflowReconciliationRequired,
    WorkflowService,
    WorkflowStartFailed,
)

_SECRET_MARKER = "diagnostic-secret-marker-8921"


class _FakeWorkerManager:
    def __init__(self, state_root: Path, workspace: Path) -> None:
        self.state_root = state_root
        self.allowed_roots = (str(workspace),)
        self.start_calls: list[Path] = []
        self.start_error: BaseException | None = None

    def create_run_directory(self) -> Path:
        self.state_root.mkdir(parents=True, exist_ok=True)
        run_dir = self.state_root / f"run-{uuid.uuid4().hex}"
        run_dir.mkdir()
        return run_dir

    async def start_worker(
        self,
        run_dir: Path,
        *,
        mode: str,
        workspace_root: Path | None = None,
    ) -> object:
        assert mode == "start"
        self.start_calls.append(run_dir)
        if self.start_error is not None:
            raise self.start_error
        (run_dir / "worker-entry-status.json").write_text(
            json.dumps({"state": "running", "exit_code": None, "error_code": None}),
            encoding="utf-8",
        )
        return object()


def _workflow_yaml() -> str:
    prompt = json.dumps(SAFE_CODEX_JOB_PROMPT, ensure_ascii=False)
    return "\n".join(
        (
            "document:",
            '  dsl: "1.0.3"',
            "  namespace: codexbridge",
            "  name: diagnostic-test",
            '  version: "1.0.0"',
            "evaluate:",
            "  language: jq",
            "  mode: strict",
            "do:",
            "  - calculate:",
            "      call: codex-job",
            "      with:",
            f"        prompt: {prompt}",
            "",
        )
    )


def _request(workspace: Path, request_id: str) -> dict[str, object]:
    return {
        "client_request_id": request_id,
        "workflow_yaml": _workflow_yaml(),
        "input_data": {},
        "workspace_root": str(workspace),
    }


def _real_service(tmp_path: Path, workspace: Path) -> tuple[WorkflowService, WorkflowWorkerManager]:
    manager = WorkflowWorkerManager(
        state_root=tmp_path / "state",
        allowed_roots=(str(workspace),),
        control_token=_SECRET_MARKER,
        python_executable=sys.executable,
        frozen=False,
    )
    return WorkflowService(manager, codex_bridge_port=18001), manager


def _read_diagnostic(run_dir: Path) -> tuple[dict[str, object], str]:
    path = run_dir / "startup-diagnostic.json"
    raw = path.read_text(encoding="utf-8")
    assert len(raw.encode("utf-8")) <= 4096
    return json.loads(raw), raw


@pytest.mark.asyncio
async def test_starting_state_exception_is_saved_without_secret_text_or_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = _FakeWorkerManager(tmp_path / "state", workspace)
    service = WorkflowService(manager, codex_bridge_port=18001)
    original_set_state = service._set_state

    async def fail_starting_state(run_id: str, state: str) -> None:
        if state == "starting":
            raise RuntimeError(f"{_SECRET_MARKER} {workspace}")
        await original_set_state(run_id, state)

    monkeypatch.setattr(service, "_set_state", fail_starting_state)
    request = {
        "client_request_id": "workflow409-start-state-0001",
        "workflow_yaml": _workflow_yaml(),
        "input_data": {},
        "workspace_root": str(workspace),
    }

    with pytest.raises(WorkflowReconciliationRequired) as raised:
        await service.start_workflow(**request)

    assert raised.value.snapshot.status == "needs_reconciliation"
    assert manager.start_calls == []
    run_dir = next((tmp_path / "state").glob("run-*"))
    diagnostic_path = run_dir / "startup-diagnostic.json"
    assert diagnostic_path.is_file()
    document = json.loads(diagnostic_path.read_text(encoding="utf-8"))
    assert [(event["stage"], event["phase"]) for event in document["stages"]] == [
        ("request_reserved", "started"),
        ("request_reserved", "completed"),
        ("starting_state_persisted", "started"),
    ]
    assert document["failure"]["stage"] == "starting_state_persisted"
    assert document["failure"]["exception_class"] == "RuntimeError"
    assert document["failure"]["worker_config_write_completed"] is False
    assert document["failure"]["worker_spawned"] is False
    diagnostic_text = diagnostic_path.read_text(encoding="utf-8")
    assert _SECRET_MARKER not in diagnostic_text
    assert str(workspace) not in diagnostic_text
    assert SAFE_CODEX_JOB_PROMPT not in diagnostic_text
    assert len(diagnostic_path.read_bytes()) <= 4096

    replay = await service.start_workflow(**request)
    assert replay.status == "needs_reconciliation"
    assert manager.start_calls == []


@pytest.mark.asyncio
async def test_manager_validation_exception_is_saved_before_any_worker_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service, manager = _real_service(tmp_path, workspace)
    spawn_calls: list[bool] = []

    def fail_validation(*_args: object, **_kwargs: object) -> Path:
        raise RuntimeError(_SECRET_MARKER)

    async def create_process(*_args: object, **_kwargs: object) -> object:
        spawn_calls.append(True)
        raise AssertionError("validation failure must precede process spawn")

    monkeypatch.setattr(manager, "_validate_run_directory", fail_validation)
    monkeypatch.setattr(manager_module.asyncio, "create_subprocess_exec", create_process)

    with pytest.raises(WorkflowReconciliationRequired) as raised:
        await service.start_workflow(**_request(workspace, "workflow409-manager-0001"))

    run_dir = manager.state_root / raised.value.snapshot.run_id
    document, raw = _read_diagnostic(run_dir)
    assert document["failure"]["stage"] == "manager_validation"
    assert document["failure"]["exception_class"] == "RuntimeError"
    assert document["failure"]["worker_config_write_completed"] is False
    assert document["failure"]["worker_spawned"] is False
    assert _SECRET_MARKER not in raw
    assert spawn_calls == []
    assert list(run_dir.glob("worker-config-*.json")) == []

    replay = await service.start_workflow(**_request(workspace, "workflow409-manager-0001"))
    assert replay.status == "needs_reconciliation"
    assert spawn_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("exception_type", [OSError, RuntimeError])
async def test_worker_config_write_exception_is_saved_without_secret_text_or_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exception_type: type[Exception],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service, manager = _real_service(tmp_path, workspace)
    spawn_calls: list[bool] = []

    def fail_config_write(_path: Path, _value: object) -> None:
        raise exception_type(f"{_SECRET_MARKER} {workspace}")

    async def create_process(*_args: object, **_kwargs: object) -> object:
        spawn_calls.append(True)
        raise AssertionError("config failure must precede process spawn")

    monkeypatch.setattr(manager_module, "_write_private_json", fail_config_write)
    monkeypatch.setattr(manager_module.asyncio, "create_subprocess_exec", create_process)

    with pytest.raises(WorkflowReconciliationRequired) as raised:
        await service.start_workflow(
            **_request(workspace, f"workflow409-config-{exception_type.__name__.lower()}")
        )

    run_dir = manager.state_root / raised.value.snapshot.run_id
    document, raw = _read_diagnostic(run_dir)
    assert document["failure"]["stage"] == "worker_config_write"
    assert document["failure"]["exception_class"] == exception_type.__name__
    assert document["failure"]["worker_config_write_completed"] is False
    assert document["failure"]["worker_spawned"] is False
    assert _SECRET_MARKER not in raw
    assert str(workspace) not in raw
    assert spawn_calls == []
    assert list(run_dir.glob("worker-config-*.json")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exception_type", "expected_error", "expected_status", "expected_spawned"),
    [
        (OSError, WorkflowStartFailed, "failed_to_start", False),
        (RuntimeError, WorkflowReconciliationRequired, "needs_reconciliation", None),
    ],
)
async def test_worker_spawn_exception_keeps_existing_classification_and_never_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exception_type: type[Exception],
    expected_error: type[Exception],
    expected_status: str,
    expected_spawned: bool | None,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service, manager = _real_service(tmp_path, workspace)
    spawn_calls: list[bool] = []

    async def fail_spawn(*_args: object, **_kwargs: object) -> object:
        spawn_calls.append(True)
        raise exception_type(f"{_SECRET_MARKER} {workspace}")

    monkeypatch.setattr(manager_module.asyncio, "create_subprocess_exec", fail_spawn)
    request = _request(workspace, f"workflow409-spawn-{exception_type.__name__.lower()}")

    with pytest.raises(expected_error) as raised:
        await service.start_workflow(**request)

    assert raised.value.snapshot.status == expected_status
    run_dir = manager.state_root / raised.value.snapshot.run_id
    document, raw = _read_diagnostic(run_dir)
    assert document["failure"]["stage"] == "worker_spawn"
    assert document["failure"]["exception_class"] == exception_type.__name__
    assert document["failure"]["worker_config_write_completed"] is True
    assert document["failure"]["worker_spawned"] is expected_spawned
    assert _SECRET_MARKER not in raw
    assert str(workspace) not in raw
    assert spawn_calls == [True]

    replay = await service.start_workflow(**request)
    assert replay.status == expected_status
    assert spawn_calls == [True]


@pytest.mark.asyncio
async def test_worker_spawn_cancellation_remains_uncertain_and_is_not_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service, manager = _real_service(tmp_path, workspace)
    spawn_calls: list[bool] = []

    async def cancel_spawn(*_args: object, **_kwargs: object) -> object:
        spawn_calls.append(True)
        raise asyncio.CancelledError()

    monkeypatch.setattr(manager_module.asyncio, "create_subprocess_exec", cancel_spawn)
    request = _request(workspace, "workflow409-cancelled-0001")

    with pytest.raises(asyncio.CancelledError):
        await service.start_workflow(**request)

    run_dirs = list(manager.state_root.glob("run-*"))
    assert len(run_dirs) == 1
    document, _raw = _read_diagnostic(run_dirs[0])
    assert document["failure"]["stage"] == "worker_spawn"
    assert document["failure"]["exception_class"] == "CancelledError"
    assert document["failure"]["worker_config_write_completed"] is True
    assert document["failure"]["worker_spawned"] is None
    replay = await service.start_workflow(**request)
    assert replay.status == "needs_reconciliation"
    assert spawn_calls == [True]


@pytest.mark.asyncio
async def test_successful_worker_start_does_not_create_startup_diagnostic(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = _FakeWorkerManager(tmp_path / "state", workspace)
    service = WorkflowService(manager, codex_bridge_port=18001)

    snapshot = await service.start_workflow(**_request(workspace, "workflow409-success-0001"))

    assert snapshot.status == "running"
    assert len(manager.start_calls) == 1
    assert not (manager.start_calls[0] / "startup-diagnostic.json").exists()


class _ExitedProcess:
    pid = 12345
    returncode = 0

    async def wait(self) -> int:
        return 0


@pytest.mark.asyncio
async def test_readiness_timeout_is_diagnostic_and_keeps_reconciliation_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service, manager = _real_service(tmp_path, workspace)
    spawn_calls: list[bool] = []

    async def create_process(*_args: object, **_kwargs: object) -> _ExitedProcess:
        spawn_calls.append(True)
        return _ExitedProcess()

    original_inspect = service_module._inspect_run
    inspection_calls = 0

    def inspect_starting(*args: object, **kwargs: object) -> service_module._RunInspection:
        nonlocal inspection_calls
        inspection_calls += 1
        if inspection_calls == 1:
            return service_module._RunInspection(
                "starting", "running", None, None, None, "2026-11-12T00:00:00+00:00"
            )
        return original_inspect(*args, **kwargs)

    monkeypatch.setattr(manager_module.asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(service_module, "_inspect_run", inspect_starting)
    monkeypatch.setattr(service_module, "_WORKER_START_TIMEOUT_SECONDS", 0.0)

    with pytest.raises(WorkflowReconciliationRequired) as raised:
        await service.start_workflow(**_request(workspace, "workflow409-timeout-0001"))

    run_dir = manager.state_root / raised.value.snapshot.run_id
    document, _raw = _read_diagnostic(run_dir)
    assert document["failure"]["stage"] == "worker_readiness_wait"
    assert document["failure"]["exception_class"] is None
    assert document["failure"]["reason"] == "readiness_timeout"
    assert document["failure"]["worker_config_write_completed"] is True
    assert document["failure"]["worker_spawned"] is True
    replay = await service.start_workflow(**_request(workspace, "workflow409-timeout-0001"))
    assert replay.status == "needs_reconciliation"
    assert spawn_calls == [True]


@pytest.mark.asyncio
async def test_diagnostic_write_failure_does_not_change_status_or_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = _FakeWorkerManager(tmp_path / "state", workspace)
    manager.start_error = RuntimeError(_SECRET_MARKER)
    service = WorkflowService(manager, codex_bridge_port=18001)
    original_open = os.open
    write_attempts: list[str] = []

    def fail_diagnostic_write(path: object, flags: int, mode: int = 0o777) -> int:
        if isinstance(path, (str, bytes, os.PathLike)) and os.fspath(path).endswith(
            "startup-diagnostic.json"
        ):
            write_attempts.append("diagnostic")
            raise OSError(_SECRET_MARKER)
        return original_open(path, flags, mode)

    monkeypatch.setattr(os, "open", fail_diagnostic_write)
    request = _request(workspace, "workflow409-diagnostic-write-0001")

    with pytest.raises(WorkflowReconciliationRequired) as raised:
        await service.start_workflow(**request)

    assert raised.value.snapshot.status == "needs_reconciliation"
    assert write_attempts == ["diagnostic"]
    replay = await service.start_workflow(**request)
    assert replay.status == "needs_reconciliation"
    assert len(manager.start_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("diagnostic_target", ["existing_file", "symlink"])
async def test_diagnostic_does_not_overwrite_existing_file_or_follow_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    diagnostic_target: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    state_root.mkdir()
    run_dir = state_root / "run-0123456789abcdef0123456789abcdef"
    run_dir.mkdir()
    diagnostic_path = run_dir / "startup-diagnostic.json"
    protected_content = "preserve-existing-diagnostic"
    if diagnostic_target == "existing_file":
        diagnostic_path.write_text(protected_content, encoding="utf-8")
    else:
        protected_target = tmp_path / "protected.json"
        protected_target.write_text(protected_content, encoding="utf-8")
        try:
            diagnostic_path.symlink_to(protected_target)
        except (OSError, NotImplementedError) as error:
            pytest.skip(f"Windows symlink creation is unavailable: {error}")

    manager = _FakeWorkerManager(state_root, workspace)
    manager.start_error = RuntimeError(_SECRET_MARKER)
    monkeypatch.setattr(manager, "create_run_directory", lambda: run_dir)
    service = WorkflowService(manager, codex_bridge_port=18001)

    with pytest.raises(WorkflowReconciliationRequired) as raised:
        await service.start_workflow(**_request(workspace, f"workflow409-{diagnostic_target}"))

    assert raised.value.snapshot.status == "needs_reconciliation"
    if diagnostic_target == "existing_file":
        assert diagnostic_path.read_text(encoding="utf-8") == protected_content
    else:
        assert diagnostic_path.is_symlink()
        assert protected_target.read_text(encoding="utf-8") == protected_content


@pytest.mark.asyncio
async def test_diagnostic_symlink_detection_prevents_file_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    state_root.mkdir()
    run_dir = state_root / "run-abcdef0123456789abcdef0123456789"
    run_dir.mkdir()
    diagnostic_path = run_dir / "startup-diagnostic.json"
    manager = _FakeWorkerManager(state_root, workspace)
    manager.start_error = RuntimeError(_SECRET_MARKER)
    monkeypatch.setattr(manager, "create_run_directory", lambda: run_dir)
    original_is_symlink = Path.is_symlink
    original_open = os.open
    diagnostic_opens: list[Path] = []

    def report_diagnostic_symlink(path: Path) -> bool:
        if path == diagnostic_path:
            return True
        return original_is_symlink(path)

    def track_diagnostic_open(path: object, flags: int, mode: int = 0o777) -> int:
        if isinstance(path, (str, bytes, os.PathLike)) and Path(path) == diagnostic_path:
            diagnostic_opens.append(diagnostic_path)
            raise OSError("diagnostic path must not be opened")
        return original_open(path, flags, mode)

    monkeypatch.setattr(Path, "is_symlink", report_diagnostic_symlink)
    monkeypatch.setattr(os, "open", track_diagnostic_open)
    service = WorkflowService(manager, codex_bridge_port=18001)

    with pytest.raises(WorkflowReconciliationRequired) as raised:
        await service.start_workflow(**_request(workspace, "workflow409-symlink-guard-01"))

    assert raised.value.snapshot.status == "needs_reconciliation"
    assert diagnostic_opens == []
    assert not diagnostic_path.exists()
