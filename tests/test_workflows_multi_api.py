from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest

from codex_bridge.activity import ActivityStore
from codex_bridge.config import BridgeConfig
from codex_bridge.ui_api import create_ui_app
from codex_bridge.workflows.codex_job_adapter import CodexJobAdapter
from codex_bridge.workflows.interpreter import parse_workflow_text
from codex_bridge.workflows.manager import WorkflowManagerError, WorkflowWorkerManager
from codex_bridge.workflows.service import WorkflowService

_TOKEN = "phase5e2c1-test-token"
_PATH = "/ui-api/control/workflows"
_SINGLE_YAML = """document:
  dsl: "1.0.3"
  namespace: codexbridge
  name: fixed-codex-job
  version: "1.0.0"
evaluate:
  language: jq
  mode: strict
do:
  - answer:
      call: codex-job
      with:
        prompt: "日本の首都を一語で答えて"
"""
_TWO_JOB_YAML = """document:
  dsl: "1.0.3"
  namespace: codexbridge
  name: two-sequential-jobs
  version: "1.0.0"
evaluate:
  language: jq
  mode: strict
do:
  - first:
      call: codex-job
      with:
        prompt: "first prompt"
  - second:
      call: codex-job
      with:
        prompt: "translate: "
        prompt_from: '${ .result.final_text }'
"""


class _Bridge:
    app_server_ready = True
    app_server_os_pid = 4567


class _FakeManager:
    def __init__(self, state_root: Path, allowed_root: Path) -> None:
        self.state_root = state_root
        self.allowed_roots = (str(allowed_root),)
        self.start_calls: list[tuple[Path, dict[str, object]]] = []
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
        **kwargs: object,
    ) -> object:
        assert mode == "start"
        assert workspace_root is not None
        self.start_calls.append(
            (
                run_dir,
                {"workspace_root": workspace_root, **kwargs},
            )
        )
        if self.start_error is not None:
            raise self.start_error
        (run_dir / "worker-entry-status.json").write_text(
            json.dumps({"state": "running", "exit_code": None, "error_code": None}),
            encoding="utf-8",
        )
        return object()


def _settings(root: Path, token: str | None = _TOKEN) -> BridgeConfig:
    return BridgeConfig(
        host="127.0.0.1",
        port=18000,
        ui_port=18001,
        allowed_roots=(str(root),),
        allowed_hosts=(),
        allowed_origins=(),
        codex_executable="codex",
        wait_default_seconds=18.0,
        wait_max_seconds=30.0,
        shutdown_grace_seconds=3.0,
        control_token=token,
    )


def _app(
    root: Path,
    *,
    token: str | None = _TOKEN,
    ready: bool = True,
    workspace: Path | None = None,
    allowed_root: Path | None = None,
):
    root.mkdir(parents=True, exist_ok=True)
    workspace = workspace or root / "workspace"
    workspace.parent.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(exist_ok=True)
    manager = _FakeManager(root / "state" / "workflows", allowed_root or root)
    service = WorkflowService(manager, codex_bridge_port=18001)
    bridge = _Bridge()
    bridge.app_server_ready = ready
    app = create_ui_app(
        bridge,
        ActivityStore(),
        _settings(root, token),
        workflow_service=service,
    )
    return app, service, manager, workspace


def _transport(app):
    return httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))


def _payload(workspace: Path, *, request_id: str = "phase5e2c1-request-01") -> dict[str, Any]:
    return {
        "client_request_id": request_id,
        "workflow_yaml": _TWO_JOB_YAML,
        "input": {"value": 4},
        "workspace_root": str(workspace),
    }


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    )


@pytest.mark.asyncio
async def test_explicit_two_job_start_is_durable_idempotent_and_projects_detail(
    tmp_path: Path,
) -> None:
    app, _service, manager, workspace = _app(tmp_path)
    payload = _payload(workspace)
    payload["max_live_codex_jobs"] = 2
    headers = {"authorization": f"Bearer {_TOKEN}"}

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        accepted = await client.post(_PATH, headers=headers, json=payload)
        replay = await client.post(_PATH, headers=headers, json=payload)

    assert accepted.status_code == 202
    assert replay.status_code == 202
    assert accepted.json()["workflow_id"] == replay.json()["workflow_id"]
    assert len(manager.start_calls) == 1
    run_dir = manager.start_calls[0][0]
    assert manager.start_calls[0][1]["max_live_codex_jobs"] == 2
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["control"]["max_live_codex_jobs"] == 2
    definition = json.loads((run_dir / "workflow.yaml").read_text(encoding="utf-8"))
    input_data = json.loads((run_dir / "input.json").read_text(encoding="utf-8"))
    digest_payload = {
        "workflow": definition,
        "input": input_data,
        "workspace_root": str(workspace.resolve()),
        "max_live_codex_jobs": 2,
    }
    assert (
        manifest["request_digest"]
        == hashlib.sha256(_canonical_json(digest_payload).encode("utf-8")).hexdigest()
    )
    assert (
        manifest["input_sha256"]
        == hashlib.sha256(_canonical_json(input_data).encode("utf-8")).hexdigest()
    )

    workflow_id = accepted.json()["workflow_id"]
    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        detail = await client.get(f"{_PATH}/{workflow_id}", headers=headers)
    detail_json = detail.json()
    assert detail.status_code == 200
    assert detail_json["max_live_codex_jobs"] == 2
    assert detail_json["jobs"] == []
    assert detail_json["status"] == "running"

    restarted_manager = _FakeManager(manager.state_root, tmp_path)
    restarted_service = WorkflowService(restarted_manager, codex_bridge_port=18001)
    replayed_after_restart = await restarted_service.start_workflow(
        client_request_id=payload["client_request_id"],
        workflow_yaml=payload["workflow_yaml"],
        input_data=payload["input"],
        workspace_root=str(workspace),
        max_live_codex_jobs=2,
    )
    assert replayed_after_restart.workflow_id == workflow_id
    assert replayed_after_restart.status == "running"
    assert restarted_manager.start_calls == []

    payload["workflow_yaml"] = _TWO_JOB_YAML.replace("first prompt", "different prompt")
    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        different_prompt = await client.post(_PATH, headers=headers, json=payload)
    assert different_prompt.status_code == 409
    assert len(manager.start_calls) == 1

    payload["workflow_yaml"] = _TWO_JOB_YAML
    payload["max_live_codex_jobs"] = 1
    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        different_limit = await client.post(_PATH, headers=headers, json=payload)
    assert different_limit.status_code == 409
    assert len(manager.start_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("start_error", "http_status", "error_code", "workflow_status", "exception_class"),
    [
        (
            RuntimeError(f"{_TOKEN} diagnostic must not contain {__file__}"),
            409,
            "workflow_needs_reconciliation",
            "needs_reconciliation",
            "RuntimeError",
        ),
        (
            WorkflowManagerError("worker process could not be started"),
            503,
            "workflow_worker_not_ready",
            "failed_to_start",
            "WorkflowManagerError",
        ),
    ],
)
async def test_startup_exception_diagnostic_preserves_http_classification_and_no_retry(
    tmp_path: Path,
    start_error: BaseException,
    http_status: int,
    error_code: str,
    workflow_status: str,
    exception_class: str,
) -> None:
    app, _service, manager, workspace = _app(tmp_path)
    manager.start_error = start_error
    payload = _payload(workspace, request_id="phase5e2c2-diagnostic-http-01")
    payload["max_live_codex_jobs"] = 2
    headers = {"authorization": f"Bearer {_TOKEN}"}

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        first = await client.post(_PATH, headers=headers, json=payload)

    assert first.status_code == http_status
    assert first.json()["error"] == error_code
    assert first.json()["status"] == workflow_status
    assert len(manager.start_calls) == 1
    run_dir = manager.start_calls[0][0]
    diagnostic_path = run_dir / "startup-diagnostic.json"
    assert diagnostic_path.is_file()
    diagnostic_text = diagnostic_path.read_text(encoding="utf-8")
    diagnostic = json.loads(diagnostic_text)
    assert diagnostic["failure"]["stage"] == "manager_validation"
    assert diagnostic["failure"]["exception_class"] == exception_class
    assert _TOKEN not in diagnostic_text
    assert str(workspace) not in diagnostic_text

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        replay = await client.post(_PATH, headers=headers, json=payload)
    assert replay.status_code == 202
    assert replay.json()["status"] == workflow_status
    assert len(manager.start_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [True, False, None, 0, 3, "2"])
async def test_invalid_two_job_limit_is_rejected_before_worker_start(
    tmp_path: Path, value: Any
) -> None:
    app, _service, manager, workspace = _app(tmp_path)
    payload = _payload(workspace)
    payload["max_live_codex_jobs"] = value

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        response = await client.post(
            _PATH,
            headers={"authorization": f"Bearer {_TOKEN}"},
            json=payload,
        )

    assert response.status_code == 400
    assert manager.start_calls == []


@pytest.mark.asyncio
async def test_two_job_yaml_without_opt_in_is_rejected(tmp_path: Path) -> None:
    app, _service, manager, workspace = _app(tmp_path)

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        response = await client.post(
            _PATH,
            headers={"authorization": f"Bearer {_TOKEN}"},
            json=_payload(workspace),
        )

    assert response.status_code == 400
    assert manager.start_calls == []


@pytest.mark.asyncio
async def test_two_job_opt_in_rejects_an_invalid_ows_shape_before_reserving(
    tmp_path: Path,
) -> None:
    app, _service, manager, workspace = _app(tmp_path)
    payload = _payload(workspace, request_id="phase5e2c1-invalid-shape")
    payload["workflow_yaml"] += (
        '  - third:\n      call: codex-job\n      with:\n        prompt: "third prompt"\n'
    )
    payload["max_live_codex_jobs"] = 2

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        response = await client.post(
            _PATH,
            headers={"authorization": f"Bearer {_TOKEN}"},
            json=payload,
        )

    assert response.status_code == 400
    assert manager.start_calls == []


@pytest.mark.asyncio
async def test_single_job_default_and_explicit_one_keep_legacy_digest_and_detail_shape(
    tmp_path: Path,
) -> None:
    headers = {"authorization": f"Bearer {_TOKEN}"}
    admitted: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for name, max_value in (("omitted", None), ("explicit-one", 1)):
        root = tmp_path / name
        workspace = tmp_path / "shared-workspace"
        app, _service, manager, workspace = _app(
            root,
            workspace=workspace,
            allowed_root=tmp_path,
        )
        payload = {
            "client_request_id": f"phase5e2c1-{name}-request",
            "workflow_yaml": _SINGLE_YAML,
            "input": {"value": 4},
            "workspace_root": str(workspace),
        }
        if max_value is not None:
            payload["max_live_codex_jobs"] = max_value
        async with httpx.AsyncClient(
            transport=_transport(app), base_url="http://localhost"
        ) as client:
            response = await client.post(_PATH, headers=headers, json=payload)
            detail = await client.get(f"{_PATH}/{response.json()['workflow_id']}", headers=headers)
        assert response.status_code == 202
        assert detail.status_code == 200
        assert "max_live_codex_jobs" not in detail.json()
        assert "jobs" not in detail.json()
        assert len(manager.start_calls) == 1
        run_dir = manager.start_calls[0][0]
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        assert "max_live_codex_jobs" not in manifest["control"]
        assert "max_live_codex_jobs" not in manager.start_calls[0][1]
        with sqlite3.connect(root / "state" / "workflows" / "requests.sqlite") as connection:
            digest = connection.execute("SELECT request_digest FROM workflow_requests").fetchone()[
                0
            ]
        admitted.append((manifest, {"request_digest": digest}))

    assert admitted[0][1]["request_digest"] == admitted[1][1]["request_digest"]
    expected_definition = parse_workflow_text(_SINGLE_YAML)
    expected_digest = hashlib.sha256(
        _canonical_json(
            {
                "workflow": expected_definition,
                "input": {"value": 4},
                "workspace_root": str((tmp_path / "shared-workspace").resolve()),
            }
        ).encode("utf-8")
    ).hexdigest()
    assert admitted[0][0]["request_digest"] == expected_digest


class _FakeProcess:
    def __init__(self) -> None:
        self.pid = 43210
        self.returncode = 0

    async def wait(self) -> int:
        return 0

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


def _two_job_manifest(run_dir: Path, workflow_id: str, digest: str) -> dict[str, Any]:
    value = {
        "workflow_id": workflow_id,
        "run_id": run_dir.name,
        "request_digest": digest,
        "mode": "codex-job-live",
        "workflow_source": "ui-api",
        "workspace_root": str(run_dir.parent.parent / "workspace"),
        "definition_sha256": "a" * 64,
        "input_sha256": "b" * 64,
        "max_tasks": 10_000,
        "codex_bridge_port": 18001,
        "control": {
            "codex_job_live": True,
            "codex_bridge_port": 18001,
            "codex_job_max_polls": 600,
            "codex_job_poll_interval_seconds": 0.2,
            "max_live_codex_jobs": 2,
        },
        "created_at": "2026-11-11T00:00:00+00:00",
    }
    (run_dir / "manifest.json").write_text(json.dumps(value), encoding="utf-8")
    return value


@pytest.mark.asyncio
async def test_manager_requires_matching_explicit_two_job_start_argument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = WorkflowWorkerManager(
        state_root=tmp_path / "state",
        allowed_roots=(str(tmp_path),),
        control_token=_TOKEN,
        python_executable="python",
        frozen=False,
    )
    run_dir = manager.create_run_directory()
    manifest = _two_job_manifest(run_dir, str(uuid.uuid4()), "c" * 64)
    spawned: list[object] = []

    async def create_process(*_args: object, **_kwargs: object) -> _FakeProcess:
        spawned.append(True)
        return _FakeProcess()

    monkeypatch.setattr(
        "codex_bridge.workflows.manager.asyncio.create_subprocess_exec", create_process
    )
    with pytest.raises(WorkflowManagerError):
        await manager.start_worker(run_dir, mode="start", workspace_root=workspace)
    assert spawned == []

    duplicate_control = json.dumps(manifest).replace(
        '"max_live_codex_jobs": 2',
        '"max_live_codex_jobs": 1, "max_live_codex_jobs": 2',
    )
    (run_dir / "manifest.json").write_text(duplicate_control, encoding="utf-8")
    with pytest.raises(WorkflowManagerError):
        await manager.start_worker(
            run_dir,
            mode="start",
            workspace_root=workspace,
            max_live_codex_jobs=2,
        )
    assert spawned == []
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    handle = await manager.start_worker(
        run_dir,
        mode="start",
        workspace_root=workspace,
        max_live_codex_jobs=2,
    )
    status = await handle.wait()
    assert status.state == "completed"
    assert spawned == [True]


def _write_active_request(state_root: Path, run_dir: Path, workflow_id: str, digest: str) -> None:
    database = sqlite3.connect(state_root / "requests.sqlite")
    database.execute(
        "CREATE TABLE workflow_requests (client_request_id TEXT, workflow_id TEXT, "
        "run_id TEXT, request_digest TEXT, state TEXT, created_at TEXT, updated_at TEXT)"
    )
    database.execute(
        "INSERT INTO workflow_requests VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "phase5e2c1-shutdown-request",
            workflow_id,
            run_dir.name,
            digest,
            "running",
            "2026-11-11T00:00:00+00:00",
            "2026-11-11T00:00:00+00:00",
        ),
    )
    database.commit()
    database.close()


@pytest.mark.asyncio
async def test_persisted_two_job_shutdown_owns_both_active_turns_and_maybe_sent_fails_closed(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = state_root / "run-0123456789abcdef0123456789abcdef"
    run_dir.mkdir(parents=True)
    workflow_id = str(uuid.uuid4())
    digest = "d" * 64
    _two_job_manifest(run_dir, workflow_id, digest)
    adapter = CodexJobAdapter(run_dir / "codex_jobs.sqlite")
    for index, task in enumerate(("do[0].first", "do[1].second")):
        job = adapter.prepare_job(
            workflow_id,
            task,
            f"prompt-{index}",
            workspace,
            port=18001,
            max_jobs_per_workflow=2,
        )
        assert adapter.claim_post(job["job_id"])
        adapter.mark_accepted(
            job["job_id"], f"owned-thread-{index}", f"owned-turn-{index}", "in_progress"
        )
    _write_active_request(state_root, run_dir, workflow_id, digest)

    report = await WorkflowWorkerManager(state_root=state_root).shutdown()

    assert report.ownership_complete
    assert report.owned_turns == {
        ("owned-thread-0", "owned-turn-0"),
        ("owned-thread-1", "owned-turn-1"),
    }

    with sqlite3.connect(run_dir / "codex_jobs.sqlite") as connection:
        connection.execute(
            "UPDATE jobs SET state = 'MAYBE_SENT', thread_id = NULL, turn_id = NULL "
            "WHERE job_id = ?",
            (job["job_id"],),
        )
    report_after_maybe_sent = await WorkflowWorkerManager(state_root=state_root).shutdown()
    assert not report_after_maybe_sent.ownership_complete
    assert report_after_maybe_sent.owned_turns == {
        ("owned-thread-0", "owned-turn-0"),
    }


def _set_job_succeeded(adapter: CodexJobAdapter, job_id: str, final_text: str) -> None:
    adapter._mark_succeeded(job_id, final_text)


def _write_dbos_status(run_dir: Path, workflow_id: str, status: str) -> None:
    with sqlite3.connect(run_dir / "dbos.sqlite") as connection:
        connection.execute(
            "CREATE TABLE workflow_status (workflow_uuid TEXT PRIMARY KEY, status TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO workflow_status VALUES (?, ?)", (workflow_id, status))


def _write_worker_state(run_dir: Path, state: str) -> None:
    (run_dir / "worker-entry-status.json").write_text(
        json.dumps({"state": state, "exit_code": 0 if state == "completed" else None}),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_two_job_projection_tracks_sequence_and_returns_second_result(tmp_path: Path) -> None:
    app, _service, manager, workspace = _app(tmp_path)
    payload = _payload(workspace, request_id="phase5e2c1-state-request")
    payload["max_live_codex_jobs"] = 2
    headers = {"authorization": f"Bearer {_TOKEN}"}
    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        started = await client.post(_PATH, headers=headers, json=payload)
    workflow_id = started.json()["workflow_id"]
    run_dir = manager.start_calls[0][0]
    adapter = CodexJobAdapter(run_dir / "codex_jobs.sqlite")
    first = adapter.prepare_job(
        workflow_id,
        "do[0].first",
        "first prompt",
        workspace,
        port=18001,
        max_jobs_per_workflow=2,
    )
    adapter.claim_post(first["job_id"])
    adapter.mark_accepted(first["job_id"], "first-thread-123", "first-turn-123", "in_progress")
    _set_job_succeeded(adapter, first["job_id"], "first final answer")

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        first_only = await client.get(f"{_PATH}/{workflow_id}", headers=headers)
        pending_result = await client.get(f"{_PATH}/{workflow_id}/result", headers=headers)
    assert first_only.json()["status"] == "running"
    assert [job["task_identity"] for job in first_only.json()["jobs"]] == ["do[0].first"]
    assert pending_result.status_code == 202

    second = adapter.prepare_job(
        workflow_id,
        "do[1].second",
        "translate: first final answer",
        workspace,
        port=18001,
        max_jobs_per_workflow=2,
    )
    adapter.claim_post(second["job_id"])
    adapter.mark_accepted(second["job_id"], "second-thread-123", "second-turn-123", "in_progress")
    _write_worker_state(run_dir, "running")
    before_dbos = (run_dir / "codex_jobs.sqlite").read_bytes()
    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        active = await client.get(f"{_PATH}/{workflow_id}", headers=headers)
    active_json = active.json()
    assert active_json["status"] == "running"
    assert [job["task_identity"] for job in active_json["jobs"]] == [
        "do[0].first",
        "do[1].second",
    ]
    assert active_json["job_id"] == second["job_id"]
    assert active_json["thread_id"] == "second-thread-123"
    assert set(active_json["jobs"][1]) == {
        "job_id",
        "task_identity",
        "state",
        "thread_id",
        "turn_id",
    }
    assert "first prompt" not in json.dumps(active_json)
    assert "first final answer" not in json.dumps(active_json)
    assert (run_dir / "codex_jobs.sqlite").read_bytes() == before_dbos

    adapter._mark_running(second["job_id"], "in_progress")
    with sqlite3.connect(run_dir.parent / "requests.sqlite") as connection:
        connection.execute(
            "UPDATE workflow_requests SET state = 'succeeded' WHERE workflow_id = ?",
            (workflow_id,),
        )
    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        active_again = await client.get(f"{_PATH}/{workflow_id}", headers=headers)
    assert active_again.json()["status"] == "running"
    assert active_again.json()["jobs"][1]["state"] == "RUNNING"

    _set_job_succeeded(adapter, second["job_id"], "second final answer")
    _write_dbos_status(run_dir, workflow_id, "SUCCESS")
    _write_worker_state(run_dir, "completed")
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    result = {
        "workflow_id": workflow_id,
        "result": {
            "job_id": second["job_id"],
            "request_key": f"{workflow_id}::do[1].second",
            "thread_id": "second-thread-123",
            "turn_id": "second-turn-123",
            "status": "SUCCEEDED",
            "result": {"final_text": "second final answer"},
        },
        "definition_sha256": manifest["definition_sha256"],
        "input_sha256": manifest["input_sha256"],
    }
    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        incomplete = await client.get(f"{_PATH}/{workflow_id}", headers=headers)
        incomplete_result = await client.get(f"{_PATH}/{workflow_id}/result", headers=headers)
    assert incomplete.json()["status"] == "unknown"
    assert incomplete_result.status_code == 409

    mismatched_result = {
        **result,
        "result": {
            "job_id": first["job_id"],
            "request_key": f"{workflow_id}::do[0].first",
            "thread_id": "first-thread-123",
            "turn_id": "first-turn-123",
            "status": "SUCCEEDED",
            "result": {"final_text": "first final answer"},
        },
    }
    (run_dir / "result.json").write_text(json.dumps(mismatched_result), encoding="utf-8")
    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        mismatched_status = await client.get(f"{_PATH}/{workflow_id}", headers=headers)
        mismatched_response = await client.get(f"{_PATH}/{workflow_id}/result", headers=headers)
    assert mismatched_status.json()["status"] == "unknown"
    assert mismatched_response.status_code == 409

    (run_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
    before_dbos = (run_dir / "dbos.sqlite").read_bytes()

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        completed = await client.get(f"{_PATH}/{workflow_id}", headers=headers)
        result_response = await client.get(f"{_PATH}/{workflow_id}/result", headers=headers)
        listed = await client.get(_PATH, headers=headers)
    assert completed.json()["status"] == "succeeded"
    assert len(completed.json()["jobs"]) == 2
    assert result_response.status_code == 200
    assert result_response.json() == result
    assert listed.status_code == 200
    assert listed.json()["workflows"][0]["status"] == "succeeded"
    assert "jobs" not in listed.json()["workflows"][0]
    assert (run_dir / "dbos.sqlite").read_bytes() == before_dbos


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "expected_status"),
    [
        ("maybe-sent", "needs_reconciliation"),
        ("reconciliation-marker", "needs_reconciliation"),
        ("failed-first", "failed"),
        ("failed-second", "failed"),
        ("worker-failed", "failed"),
        ("dbos-failed", "failed"),
        ("second-before-first-succeeded", "needs_reconciliation"),
        ("wrong-order", "needs_reconciliation"),
        ("extra-row", "needs_reconciliation"),
        ("wrong-workflow", "needs_reconciliation"),
        ("tampered-receipt", "needs_reconciliation"),
    ],
)
async def test_multi_job_projection_reconciles_ambiguous_ledgers_and_exposes_failures(
    tmp_path: Path,
    scenario: str,
    expected_status: str,
) -> None:
    app, _service, manager, workspace = _app(tmp_path)
    payload = _payload(workspace, request_id=f"phase5e2c1-{scenario}-request")
    payload["max_live_codex_jobs"] = 2
    headers = {"authorization": f"Bearer {_TOKEN}"}
    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        started = await client.post(_PATH, headers=headers, json=payload)
    assert started.status_code == 202
    workflow_id = started.json()["workflow_id"]
    run_dir = manager.start_calls[0][0]
    adapter = CodexJobAdapter(run_dir / "codex_jobs.sqlite")

    if scenario == "worker-failed":
        _write_worker_state(run_dir, "failed")
    elif scenario == "dbos-failed":
        _write_dbos_status(run_dir, workflow_id, "FAILED")
    elif scenario in {"maybe-sent", "reconciliation-marker", "failed-first", "wrong-workflow"}:
        first = adapter.prepare_job(
            workflow_id,
            "do[0].first",
            "first prompt",
            workspace,
            port=18001,
            max_jobs_per_workflow=2,
        )
        adapter.claim_post(first["job_id"])
        if scenario != "maybe-sent":
            adapter.mark_accepted(
                first["job_id"], "first-thread-123", "first-turn-123", "in_progress"
            )
        if scenario == "reconciliation-marker":
            adapter.mark_reconciliation(first["job_id"], "synthetic marker")
        elif scenario == "failed-first":
            adapter._mark_failed(first["job_id"], "failed")
            (run_dir / "dbos.sqlite").write_text("corrupted", encoding="utf-8")
            (run_dir / "result.json").write_text("{}", encoding="utf-8")
        elif scenario == "wrong-workflow":
            with sqlite3.connect(run_dir / "codex_jobs.sqlite") as connection:
                connection.execute(
                    "UPDATE jobs SET workflow_id = ? WHERE job_id = ?",
                    (str(uuid.uuid4()), first["job_id"]),
                )
    elif scenario in {
        "failed-second",
        "second-before-first-succeeded",
        "wrong-order",
        "extra-row",
        "tampered-receipt",
    }:
        first = adapter.prepare_job(
            workflow_id,
            "do[0].first",
            "first prompt",
            workspace,
            port=18001,
            max_jobs_per_workflow=2,
        )
        adapter.claim_post(first["job_id"])
        adapter.mark_accepted(first["job_id"], "first-thread-123", "first-turn-123", "in_progress")
        if scenario in {"failed-second", "wrong-order", "extra-row", "tampered-receipt"}:
            adapter._mark_succeeded(first["job_id"], "first final answer")
        second = adapter.prepare_job(
            workflow_id,
            "do[1].second",
            "second prompt",
            workspace,
            port=18001,
            max_jobs_per_workflow=2,
        )
        adapter.claim_post(second["job_id"])
        adapter.mark_accepted(
            second["job_id"], "second-thread-123", "second-turn-123", "in_progress"
        )
        if scenario == "failed-second":
            adapter._mark_failed(second["job_id"], "interrupted")
        elif scenario == "wrong-order":
            with sqlite3.connect(run_dir / "codex_jobs.sqlite") as connection:
                connection.execute(
                    "UPDATE jobs SET created_at = '2000-01-01T00:00:00+00:00' WHERE job_id = ?",
                    (second["job_id"],),
                )
                connection.execute(
                    "UPDATE jobs SET created_at = '2001-01-01T00:00:00+00:00' WHERE job_id = ?",
                    (first["job_id"],),
                )
        elif scenario == "extra-row":
            with sqlite3.connect(run_dir / "codex_jobs.sqlite") as connection:
                connection.execute(
                    "INSERT INTO jobs (job_id, request_key, workflow_id, task_identity, "
                    "request_digest, request_json, state, client_request_id, title_marker, "
                    "agent_cwd, created_at, updated_at, thread_id, turn_id, remote_state, "
                    "receipt_json, error) "
                    "SELECT 'extra-job-123', workflow_id || '::do[2].extra', workflow_id, "
                    "'do[2].extra', request_digest, request_json, 'PREPARED', "
                    "client_request_id || '-extra', title_marker || '-extra', agent_cwd, "
                    "created_at, updated_at, NULL, NULL, NULL, NULL, NULL "
                    "FROM jobs WHERE job_id = ?",
                    (first["job_id"],),
                )
        elif scenario == "tampered-receipt":
            with sqlite3.connect(run_dir / "codex_jobs.sqlite") as connection:
                connection.execute(
                    "UPDATE jobs SET receipt_json = '{}' WHERE job_id = ?",
                    (first["job_id"],),
                )

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        response = await client.get(f"{_PATH}/{workflow_id}", headers=headers)
        result = await client.get(f"{_PATH}/{workflow_id}/result", headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == expected_status
    assert result.status_code == 409


@pytest.mark.asyncio
async def test_multi_job_symlinked_ledger_is_not_followed(tmp_path: Path) -> None:
    app, _service, manager, workspace = _app(tmp_path)
    payload = _payload(workspace, request_id="phase5e2c1-symlink-request")
    payload["max_live_codex_jobs"] = 2
    headers = {"authorization": f"Bearer {_TOKEN}"}
    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        started = await client.post(_PATH, headers=headers, json=payload)
    run_dir = manager.start_calls[0][0]
    outside = tmp_path / "outside.sqlite"
    sqlite3.connect(outside).close()
    try:
        (run_dir / "codex_jobs.sqlite").symlink_to(outside)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"Windows symlink creation is unavailable: {error}")

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        detail = await client.get(f"{_PATH}/{started.json()['workflow_id']}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["status"] == "needs_reconciliation"


@pytest.mark.asyncio
async def test_missing_multi_job_ledger_cannot_be_projected_as_success(
    tmp_path: Path,
) -> None:
    app, _service, manager, workspace = _app(tmp_path)
    payload = _payload(workspace, request_id="phase5e2c1-missing-ledger")
    payload["max_live_codex_jobs"] = 2
    headers = {"authorization": f"Bearer {_TOKEN}"}
    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        started = await client.post(_PATH, headers=headers, json=payload)
    workflow_id = started.json()["workflow_id"]
    run_dir = manager.start_calls[0][0]
    adapter = CodexJobAdapter(run_dir / "codex_jobs.sqlite")
    first = adapter.prepare_job(
        workflow_id,
        "do[0].first",
        "first prompt",
        workspace,
        port=18001,
        max_jobs_per_workflow=2,
    )
    adapter.claim_post(first["job_id"])
    adapter.mark_accepted(first["job_id"], "first-thread-123", "first-turn-123", "in_progress")
    adapter._mark_succeeded(first["job_id"], "first final answer")
    second = adapter.prepare_job(
        workflow_id,
        "do[1].second",
        "second prompt",
        workspace,
        port=18001,
        max_jobs_per_workflow=2,
    )
    adapter.claim_post(second["job_id"])
    adapter.mark_accepted(second["job_id"], "second-thread-123", "second-turn-123", "in_progress")
    adapter._mark_succeeded(second["job_id"], "second final answer")
    _write_dbos_status(run_dir, workflow_id, "SUCCESS")
    _write_worker_state(run_dir, "completed")
    (run_dir / "codex_jobs.sqlite").unlink()

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        detail = await client.get(f"{_PATH}/{workflow_id}", headers=headers)
        result = await client.get(f"{_PATH}/{workflow_id}/result", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["status"] == "unknown"
    assert result.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_field", ["unknown"])
async def test_workflow_api_rejects_unknown_request_fields(tmp_path: Path, bad_field: str) -> None:
    app, _service, manager, workspace = _app(tmp_path)
    payload = _payload(workspace)
    payload["max_live_codex_jobs"] = 2
    payload[bad_field] = "not allowed"
    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        response = await client.post(
            _PATH,
            headers={"authorization": f"Bearer {_TOKEN}"},
            json=payload,
        )
    assert response.status_code == 400
    assert manager.start_calls == []
