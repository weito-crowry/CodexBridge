from __future__ import annotations

import asyncio
import errno
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from codex_bridge.workflows import manager as manager_module
from codex_bridge.workflows import service as service_module
from codex_bridge.workflows import startup_diagnostics as diagnostics_module
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
    lifecycle_text = (run_dir / "bridge-lifecycle.jsonl").read_text(encoding="utf-8")
    lifecycle_events = [json.loads(line) for line in lifecycle_text.splitlines()]
    reconciliation_transition = next(
        event
        for event in lifecycle_events
        if event["event"] == "workflow_state_changed" and event["state"] == "needs_reconciliation"
    )
    assert "previous_state" not in reconciliation_transition
    assert _SECRET_MARKER not in lifecycle_text
    assert str(workspace) not in lifecycle_text

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
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = _FakeWorkerManager(tmp_path / "state", workspace)
    service = WorkflowService(manager, codex_bridge_port=18001)

    snapshot = await service.start_workflow(**_request(workspace, "workflow409-success-0001"))

    assert snapshot.status == "running"
    assert len(manager.start_calls) == 1
    assert not (manager.start_calls[0] / "startup-diagnostic.json").exists()
    assert capsys.readouterr().err == ""


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
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service, manager = _real_service(tmp_path, workspace)
    secret_text = f"api_key={_SECRET_MARKER} path={workspace} prompt={SAFE_CODEX_JOB_PROMPT}"

    def fail_config_write(_path: Path, _value: object) -> None:
        raise RuntimeError(secret_text)

    monkeypatch.setattr(manager_module, "_write_private_json", fail_config_write)
    original_open = os.open
    write_attempts: list[str] = []
    spawn_attempts: list[bool] = []

    def fail_diagnostic_write(path: object, flags: int, mode: int = 0o777) -> int:
        if isinstance(path, (str, bytes, os.PathLike)) and os.fspath(path).endswith(
            "startup-diagnostic.json"
        ):
            write_attempts.append("diagnostic")
            raise OSError(errno.ENOSPC, secret_text)
        return original_open(path, flags, mode)

    async def track_spawn(*_args: object, **_kwargs: object) -> object:
        spawn_attempts.append(True)
        raise AssertionError("configuration failure must precede process spawn")

    monkeypatch.setattr(os, "open", fail_diagnostic_write)
    monkeypatch.setattr(manager_module.asyncio, "create_subprocess_exec", track_spawn)
    request = _request(workspace, "workflow409-diagnostic-write-0001")

    with pytest.raises(WorkflowReconciliationRequired) as raised:
        await service.start_workflow(**request)

    assert raised.value.snapshot.status == "needs_reconciliation"
    assert write_attempts == ["diagnostic"]
    assert spawn_attempts == []
    run_dir = manager.state_root / raised.value.snapshot.run_id
    assert not (run_dir / "startup-diagnostic.json").exists()
    stderr_output = capsys.readouterr().err
    assert stderr_output == (
        "workflow.startup_diagnostic.persistence_failed "
        "stage=worker_config_write original_exception=RuntimeError "
        "persistence_exception=OSError\n"
    )
    assert _SECRET_MARKER not in stderr_output
    assert str(workspace) not in stderr_output
    assert SAFE_CODEX_JOB_PROMPT not in stderr_output

    replay = await service.start_workflow(**request)
    assert replay.status == "needs_reconciliation"
    assert spawn_attempts == []
    assert capsys.readouterr().err == ""


@pytest.mark.asyncio
async def test_diagnostic_fsync_failure_falls_back_without_changing_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = _FakeWorkerManager(tmp_path / "state", workspace)
    manager.start_error = RuntimeError(
        f"api_key={_SECRET_MARKER} path={workspace} prompt={SAFE_CODEX_JOB_PROMPT}"
    )
    service = WorkflowService(manager, codex_bridge_port=18001)
    original_fsync = os.fsync
    original_open = os.open
    diagnostic_descriptors: set[int] = set()

    def track_diagnostic_open(path: object, flags: int, mode: int = 0o777) -> int:
        descriptor = original_open(path, flags, mode)
        if isinstance(path, (str, bytes, os.PathLike)) and os.fspath(path).endswith(
            "startup-diagnostic.json"
        ):
            diagnostic_descriptors.add(descriptor)
        return descriptor

    def fail_diagnostic_fsync(descriptor: int) -> None:
        if descriptor in diagnostic_descriptors:
            raise OSError(errno.ENOSPC, "fsync failed with diagnostic secret")
        original_fsync(descriptor)

    monkeypatch.setattr(os, "open", track_diagnostic_open)
    monkeypatch.setattr(os, "fsync", fail_diagnostic_fsync)
    request = _request(workspace, "workflow409-diagnostic-fsync-0001")

    with pytest.raises(WorkflowReconciliationRequired) as raised:
        await service.start_workflow(**request)

    assert raised.value.snapshot.status == "needs_reconciliation"
    assert len(manager.start_calls) == 1
    run_dir = manager.start_calls[0]
    diagnostic_path = run_dir / "startup-diagnostic.json"
    assert diagnostic_path.is_file()
    document, raw = _read_diagnostic(run_dir)
    assert document["failure"]["stage"] == "manager_validation"
    assert document["failure"]["exception_class"] == "RuntimeError"
    assert _SECRET_MARKER not in raw
    assert str(workspace) not in raw
    assert SAFE_CODEX_JOB_PROMPT not in raw
    stderr_output = capsys.readouterr().err
    assert stderr_output == (
        "workflow.startup_diagnostic.persistence_failed "
        "stage=manager_validation original_exception=RuntimeError "
        "persistence_exception=OSError\n"
    )
    assert "diagnostic secret" not in stderr_output
    assert _SECRET_MARKER not in stderr_output
    assert str(workspace) not in stderr_output
    assert SAFE_CODEX_JOB_PROMPT not in stderr_output

    replay = await service.start_workflow(**request)
    assert replay.status == "needs_reconciliation"
    assert len(manager.start_calls) == 1
    assert capsys.readouterr().err == ""


@pytest.mark.asyncio
async def test_partial_diagnostic_write_falls_back_and_keeps_workflow_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = _FakeWorkerManager(tmp_path / "state", workspace)
    manager.start_error = RuntimeError(_SECRET_MARKER)
    service = WorkflowService(manager, codex_bridge_port=18001)
    original_open = os.open
    original_fdopen = os.fdopen
    diagnostic_descriptors: set[int] = set()
    partial_payloads: list[bytes] = []

    class _PartialWriter:
        def __init__(self, stream: Any) -> None:
            self._stream = stream

        def __enter__(self) -> _PartialWriter:
            return self

        def __exit__(
            self,
            _exception_type: object,
            _exception: object,
            _traceback: object,
        ) -> None:
            self._stream.close()

        def write(self, payload: bytes) -> int:
            partial = payload[:16]
            written = self._stream.write(partial)
            self._stream.flush()
            partial_payloads.append(partial[:written])
            raise OSError(errno.ENOSPC, "partial diagnostic write")

    def track_diagnostic_open(path: object, flags: int, mode: int = 0o777) -> int:
        descriptor = original_open(path, flags, mode)
        if isinstance(path, (str, bytes, os.PathLike)) and os.fspath(path).endswith(
            "startup-diagnostic.json"
        ):
            diagnostic_descriptors.add(descriptor)
        return descriptor

    def wrap_diagnostic_fdopen(descriptor: int, *args: Any, **kwargs: Any) -> Any:
        stream = original_fdopen(descriptor, *args, **kwargs)
        if descriptor in diagnostic_descriptors:
            return _PartialWriter(stream)
        return stream

    monkeypatch.setattr(os, "open", track_diagnostic_open)
    monkeypatch.setattr(os, "fdopen", wrap_diagnostic_fdopen)
    request = _request(workspace, "workflow409-diagnostic-partial-0001")

    with pytest.raises(WorkflowReconciliationRequired) as raised:
        await service.start_workflow(**request)

    assert raised.value.snapshot.status == "needs_reconciliation"
    assert len(manager.start_calls) == 1
    run_dir = manager.start_calls[0]
    diagnostic_path = run_dir / "startup-diagnostic.json"
    assert diagnostic_path.is_file()
    partial_file = diagnostic_path.read_bytes()
    assert partial_payloads == [partial_file]
    assert partial_file
    assert _SECRET_MARKER.encode() not in partial_file
    assert capsys.readouterr().err == (
        "workflow.startup_diagnostic.persistence_failed "
        "stage=manager_validation original_exception=RuntimeError "
        "persistence_exception=OSError\n"
    )

    replay = await service.start_workflow(**request)
    assert replay.status == "needs_reconciliation"
    assert len(manager.start_calls) == 1
    assert capsys.readouterr().err == ""


@pytest.mark.asyncio
async def test_stderr_failure_does_not_change_workflow_exception_or_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = _FakeWorkerManager(tmp_path / "state", workspace)
    manager.start_error = RuntimeError(f"api_key={_SECRET_MARKER} path={workspace}")
    service = WorkflowService(manager, codex_bridge_port=18001)
    original_open = os.open
    diagnostic_writes: list[bool] = []

    def fail_diagnostic_open(path: object, flags: int, mode: int = 0o777) -> int:
        if isinstance(path, (str, bytes, os.PathLike)) and os.fspath(path).endswith(
            "startup-diagnostic.json"
        ):
            diagnostic_writes.append(True)
            raise OSError(errno.ENOSPC, "diagnostic disk full")
        return original_open(path, flags, mode)

    class _BrokenStderr:
        def __init__(self) -> None:
            self.write_attempts = 0

        def write(self, _message: str) -> int:
            self.write_attempts += 1
            raise OSError("stderr is unavailable")

        def flush(self) -> None:
            raise OSError("stderr is unavailable")

    broken_stderr = _BrokenStderr()
    monkeypatch.setattr(os, "open", fail_diagnostic_open)
    monkeypatch.setattr(diagnostics_module.sys, "stderr", broken_stderr)
    request = _request(workspace, "workflow409-diagnostic-stderr-0001")

    with pytest.raises(WorkflowReconciliationRequired) as raised:
        await service.start_workflow(**request)

    assert raised.value.snapshot.status == "needs_reconciliation"
    assert diagnostic_writes == [True]
    assert broken_stderr.write_attempts == 1
    assert len(manager.start_calls) == 1

    replay = await service.start_workflow(**request)
    assert replay.status == "needs_reconciliation"
    assert diagnostic_writes == [True]
    assert broken_stderr.write_attempts == 1
    assert len(manager.start_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("diagnostic_target", ["existing_file", "symlink"])
async def test_diagnostic_does_not_overwrite_existing_file_or_follow_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    diagnostic_target: str,
    capsys: pytest.CaptureFixture[str],
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
    assert capsys.readouterr().err == ""
    if diagnostic_target == "existing_file":
        assert diagnostic_path.read_text(encoding="utf-8") == protected_content
    else:
        assert diagnostic_path.is_symlink()
        assert protected_target.read_text(encoding="utf-8") == protected_content


@pytest.mark.asyncio
async def test_diagnostic_symlink_detection_prevents_file_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
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
    assert capsys.readouterr().err == ""
