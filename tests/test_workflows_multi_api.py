from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx2 as httpx
import pytest
import yaml

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


def _three_job_yaml() -> str:
    fixture = (
        Path(__file__).parents[1]
        / "poc"
        / "ows"
        / "fixtures"
        / ("three_codex_job_loop_workflow.yaml")
    )
    return fixture.read_text(encoding="utf-8")


_THREE_JOB_INPUT = {
    "issues": [
        {"number": 17, "enabled": True},
        {"number": 5, "enabled": False},
        {"number": 9, "enabled": True},
        {"number": 21, "enabled": True},
    ]
}


class _LoopbackAPIState:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []
        self.posts: list[dict[str, Any]] = []
        self.jobs: dict[str, dict[str, Any]] = {}
        self.unknown_after_job: int | None = None


class _LoopbackAPIHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        del format, args

    @property
    def state(self) -> _LoopbackAPIState:
        return self.server.state  # type: ignore[attr-defined, no-any-return]

    def _reply(self, status: int, value: dict[str, Any]) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self.state.requests.append((self.command, self.path))
        parsed = urlsplit(self.path)
        if parsed.path == "/healthz":
            self._reply(200, {"status": "ok"})
            return
        if parsed.path == "/ui-api/status":
            self._reply(200, {"app_server": "ready"})
            return
        if parsed.path == "/ui-api/control/capabilities":
            self._reply(200, {"models": []})
            return
        segments = parsed.path.split("/")
        if len(segments) >= 5 and segments[1:3] == ["ui-api", "threads"]:
            job = self.state.jobs.get(segments[3])
            if job is None:
                self._reply(404, {"error": "not_found"})
                return
            state = (
                "needs_approval" if self.state.unknown_after_job == job["index"] else "completed"
            )
            resource = segments[4]
            if resource == "status":
                self._reply(
                    200,
                    {
                        "thread_id": segments[3],
                        "turn_id": job["turn_id"],
                        "state": state,
                    },
                )
            elif resource == "turns":
                self._reply(
                    200,
                    {
                        "thread_id": segments[3],
                        "turns": [{"id": job["turn_id"], "status": state}],
                    },
                )
            elif resource == "items":
                items = []
                if state == "completed":
                    items = [
                        {
                            "turn_id": job["turn_id"],
                            "item": {
                                "type": "agentMessage",
                                "phase": "final_answer",
                                "text": job["final_text"],
                            },
                        }
                    ]
                self._reply(200, {"thread_id": segments[3], "items": items})
            else:
                self._reply(404, {"error": "not_found"})
            return
        self._reply(404, {"error": "not_found"})

    def do_POST(self) -> None:
        self.state.requests.append((self.command, self.path))
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        self.state.posts.append(payload)
        index = len(self.state.posts)
        thread_id = f"thread_{index:08d}"
        turn_id = f"turn_{index:08d}"
        final_text = payload["prompt"].rsplit(" ", 1)[-1]
        self.state.jobs[thread_id] = {
            "index": index,
            "turn_id": turn_id,
            "final_text": final_text,
        }
        self._reply(
            201,
            {"thread_id": thread_id, "turn_id": turn_id, "state": "in_progress"},
        )


class _LoopbackAPIServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        del request, client_address


@pytest.fixture
def fake_codex_api(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CODEX_BRIDGE_CONTROL_TOKEN", _TOKEN)
    server = _LoopbackAPIServer(("127.0.0.1", 0), _LoopbackAPIHandler)
    server.state = _LoopbackAPIState()  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class _Bridge:
    app_server_ready = True
    app_server_os_pid = 4567


class _FakeManager:
    def __init__(self, state_root: Path, allowed_root: Path) -> None:
        self.state_root = state_root
        self.allowed_roots = (str(allowed_root),)
        self.start_calls: list[tuple[Path, dict[str, object]]] = []

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
        (run_dir / "worker-entry-status.json").write_text(
            json.dumps({"state": "running", "exit_code": None, "error_code": None}),
            encoding="utf-8",
        )
        return object()


class _ImmediateDBOS:
    workflow_id = ""
    step_names: list[str] = []

    @classmethod
    def step(cls, *, name: str):
        def decorate(function: Any):
            def run(*args: Any, **kwargs: Any):
                cls.step_names.append(name)
                return function(*args, **kwargs)

            return run

        return decorate

    @staticmethod
    def sleep(seconds: float) -> None:
        del seconds


def _execute_three_job_worker(
    run_dir: Path,
    workspace: Path,
    manifest: dict[str, Any],
    *,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    from codex_bridge.workflows import worker

    _ImmediateDBOS.workflow_id = str(manifest["workflow_id"])
    _ImmediateDBOS.step_names = []
    worker._named_step.cache_clear()
    worker._durable_codex_job_step.cache_clear()
    monkeypatch.setattr(worker, "DBOS", _ImmediateDBOS)
    loaded_manifest, definition_text, input_data = worker.load_run_request(run_dir)
    assert loaded_manifest == manifest
    control = {
        **manifest["control"],
        "workspace_root": str(workspace.resolve()),
        "allowed_roots": [str(workspace.parent.resolve())],
    }
    try:
        return worker.execute_ows_workflow.__wrapped__(
            definition_text,
            manifest["definition_sha256"],
            input_data,
            manifest["input_sha256"],
            str(run_dir / "task-events.jsonl"),
            control,
            manifest["max_tasks"],
        )
    finally:
        worker._named_step.cache_clear()
        worker._durable_codex_job_step.cache_clear()


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
    codex_bridge_port: int = 18001,
):
    root.mkdir(parents=True, exist_ok=True)
    workspace = workspace or root / "workspace"
    workspace.parent.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(exist_ok=True)
    manager = _FakeManager(root / "state" / "workflows", allowed_root or root)
    service = WorkflowService(manager, codex_bridge_port=codex_bridge_port)
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
@pytest.mark.parametrize("value", [True, False, None, 0, 4, "2"])
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
async def test_three_job_fixture_requires_explicit_api_limit(tmp_path: Path) -> None:
    app, _service, manager, workspace = _app(tmp_path)
    payload = _payload(workspace, request_id="phase5f1a-no-three-opt-in")
    payload.update({"workflow_yaml": _three_job_yaml(), "input": _THREE_JOB_INPUT})

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        response = await client.post(
            _PATH,
            headers={"authorization": f"Bearer {_TOKEN}"},
            json=payload,
        )

    assert response.status_code == 400
    assert manager.start_calls == []
    assert not manager.state_root.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "input_data",
    [
        {"issues": [{"number": index + 1, "enabled": False} for index in range(5)]},
        {"issues": [{"number": index + 1, "enabled": True} for index in range(4)]},
        {"issues": [{"number": 1, "enabled": True}, {"number": 1, "enabled": False}]},
        {"issues": [{"number": 0, "enabled": True}]},
        {"issues": [{"number": -2, "enabled": True}]},
        {"issues": [{"number": True, "enabled": True}]},
        {"issues": [{"number": 4, "enabled": 1}]},
        {"issues": [{"number": 4, "enabled": True, "prompt": "unsafe"}]},
        {"issues": [], "prompt": "unsafe"},
        {"issues": "not-an-array"},
    ],
    ids=[
        "five-items",
        "four-enabled",
        "duplicate-number",
        "zero-number",
        "negative-number",
        "boolean-number",
        "integer-enabled",
        "extra-issue-key",
        "extra-input-key",
        "issues-not-array",
    ],
)
async def test_three_job_invalid_input_is_rejected_before_run_or_codex_post(
    tmp_path: Path,
    fake_codex_api: _LoopbackAPIServer,
    input_data: Any,
) -> None:
    app, _service, manager, workspace = _app(
        tmp_path,
        codex_bridge_port=fake_codex_api.server_address[1],
    )
    payload = _payload(workspace, request_id="phase5f1a-invalid-input")
    payload.update(
        {
            "workflow_yaml": _three_job_yaml(),
            "input": input_data,
            "max_live_codex_jobs": 3,
        }
    )

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        response = await client.post(
            _PATH,
            headers={"authorization": f"Bearer {_TOKEN}"},
            json=payload,
        )

    assert response.status_code == 400
    assert manager.start_calls == []
    assert not manager.state_root.exists()
    assert fake_codex_api.state.posts == []  # type: ignore[attr-defined]
    assert fake_codex_api.state.requests == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    ["invalid-prompt-from", "extra-task", "extra-call", "nested-for"],
)
async def test_three_job_invalid_static_shape_is_rejected_before_run_or_codex_post(
    tmp_path: Path,
    fake_codex_api: _LoopbackAPIServer,
    mutation: str,
) -> None:
    app, _service, manager, workspace = _app(
        tmp_path,
        codex_bridge_port=fake_codex_api.server_address[1],
    )
    definition = yaml.safe_load(_three_job_yaml())
    if mutation == "invalid-prompt-from":
        definition["do"][0]["processIssues"]["do"][1]["processIssue"]["with"]["prompt_from"] = (
            "${ .unsafe }"
        )
    elif mutation == "extra-task":
        definition["do"].append({"extra": {"set": {"value": 1}}})
    elif mutation == "extra-call":
        definition["do"][0]["processIssues"]["do"].append(
            {"secondCall": {"call": "codex-job", "with": {"prompt": "unsafe"}}}
        )
    else:
        definition["do"][0]["processIssues"]["do"][1]["processIssue"] = {
            "for": {"in": "${ . }"},
            "do": [{"nestedCall": {"call": "codex-job", "with": {"prompt": "unsafe"}}}],
        }
    payload = _payload(workspace, request_id=f"phase5f1a-invalid-{mutation}")
    payload.update(
        {
            "workflow_yaml": yaml.safe_dump(definition, allow_unicode=True, sort_keys=False),
            "input": _THREE_JOB_INPUT,
            "max_live_codex_jobs": 3,
        }
    )

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        response = await client.post(
            _PATH,
            headers={"authorization": f"Bearer {_TOKEN}"},
            json=payload,
        )

    assert response.status_code == 400
    assert manager.start_calls == []
    assert not manager.state_root.exists()
    assert fake_codex_api.state.posts == []  # type: ignore[attr-defined]
    assert fake_codex_api.state.requests == []  # type: ignore[attr-defined]


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
async def test_three_job_loop_runs_against_fake_loopback_and_reads_back_result(
    tmp_path: Path,
    fake_codex_api: _LoopbackAPIServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, service, manager, workspace = _app(
        tmp_path,
        codex_bridge_port=fake_codex_api.server_address[1],
    )
    payload = _payload(workspace, request_id="phase5f1a-three-success")
    payload.update(
        {
            "workflow_yaml": _three_job_yaml(),
            "input": _THREE_JOB_INPUT,
            "max_live_codex_jobs": 3,
        }
    )
    headers = {"authorization": f"Bearer {_TOKEN}"}

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        accepted = await client.post(_PATH, headers=headers, json=payload)
    assert accepted.status_code == 202
    run_dir = manager.start_calls[0][0]
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    definition = json.loads((run_dir / "workflow.yaml").read_text(encoding="utf-8"))
    input_data = json.loads((run_dir / "input.json").read_text(encoding="utf-8"))
    assert manager.start_calls[0][1]["max_live_codex_jobs"] == 3
    assert manifest["control"]["max_live_codex_jobs"] == 3
    digest_payload = {
        "workflow": definition,
        "input": input_data,
        "workspace_root": str(workspace.resolve()),
        "max_live_codex_jobs": 3,
    }
    assert (
        manifest["request_digest"]
        == hashlib.sha256(_canonical_json(digest_payload).encode("utf-8")).hexdigest()
    )
    result = _execute_three_job_worker(
        run_dir,
        workspace,
        manifest,
        monkeypatch=monkeypatch,
    )
    _write_worker_state(run_dir, "completed")
    _write_dbos_status(run_dir, manifest["workflow_id"], "SUCCESS")
    stored_result = {
        "workflow_id": manifest["workflow_id"],
        "definition_sha256": manifest["definition_sha256"],
        "input_sha256": manifest["input_sha256"],
        "result": result,
    }
    (run_dir / "result.json").write_text(json.dumps(stored_result), encoding="utf-8")

    jobs = CodexJobAdapter(run_dir / "codex_jobs.sqlite").get_jobs_for_workflow(
        manifest["workflow_id"]
    )
    assert len(fake_codex_api.state.posts) == 3  # type: ignore[attr-defined]
    assert [item["number"] for item in result] == [5, 9, 17, 21]
    assert [item["status"] for item in result] == [
        "skipped",
        "SUCCEEDED",
        "SUCCEEDED",
        "SUCCEEDED",
    ]
    assert [item["final_text"] for item in result[1:]] == ["9", "17", "21"]
    assert [job["state"] for job in jobs] == ["SUCCEEDED", "SUCCEEDED", "SUCCEEDED"]
    assert [job["task_identity"] for job in jobs] == [
        "do[0].processIssues.iteration[1].do[1].processIssue",
        "do[0].processIssues.iteration[2].do[1].processIssue",
        "do[0].processIssues.iteration[3].do[1].processIssue",
    ]
    assert _ImmediateDBOS.step_names
    assert len(_ImmediateDBOS.step_names) == len(set(_ImmediateDBOS.step_names))

    workflow_id = str(manifest["workflow_id"])
    readback = await service.get_result(workflow_id)
    assert readback.workflow.status == "succeeded"
    assert readback.result == stored_result
    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        detail = await client.get(f"{_PATH}/{workflow_id}", headers=headers)
        result_response = await client.get(f"{_PATH}/{workflow_id}/result", headers=headers)
        replay = await client.post(_PATH, headers=headers, json=payload)
    assert detail.status_code == 200
    assert detail.json()["status"] == "succeeded"
    assert detail.json()["max_live_codex_jobs"] == 3
    assert len(detail.json()["jobs"]) == 3
    assert result_response.status_code == 200
    assert result_response.json() == stored_result
    assert replay.status_code == 202
    assert replay.json()["workflow_id"] == workflow_id
    assert len(manager.start_calls) == 1
    assert len(fake_codex_api.state.posts) == 3  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_three_job_loop_stops_after_second_job_requires_reconciliation(
    tmp_path: Path,
    fake_codex_api: _LoopbackAPIServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_codex_api.state.unknown_after_job = 2  # type: ignore[attr-defined]
    app, service, manager, workspace = _app(
        tmp_path,
        codex_bridge_port=fake_codex_api.server_address[1],
    )
    payload = _payload(workspace, request_id="phase5f1a-three-unknown")
    payload.update(
        {
            "workflow_yaml": _three_job_yaml(),
            "input": _THREE_JOB_INPUT,
            "max_live_codex_jobs": 3,
        }
    )
    headers = {"authorization": f"Bearer {_TOKEN}"}
    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        accepted = await client.post(_PATH, headers=headers, json=payload)
    run_dir = manager.start_calls[0][0]
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))

    from codex_bridge.workflows.codex_job_adapter import CodexJobReconciliationRequiredError

    with pytest.raises(CodexJobReconciliationRequiredError):
        _execute_three_job_worker(
            run_dir,
            workspace,
            manifest,
            monkeypatch=monkeypatch,
        )

    assert accepted.status_code == 202
    assert len(fake_codex_api.state.posts) == 2  # type: ignore[attr-defined]
    snapshot = await service.get_workflow(str(manifest["workflow_id"]))
    assert snapshot.status == "needs_reconciliation"
    assert len(snapshot.jobs) == 2
    assert [job.state for job in snapshot.jobs] == ["SUCCEEDED", "ACCEPTED"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["input-hash", "request-digest", "max-live-limit"])
async def test_three_job_worker_rechecks_saved_hashes_before_any_codex_post(
    tmp_path: Path,
    fake_codex_api: _LoopbackAPIServer,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    app, _service, manager, workspace = _app(
        tmp_path,
        codex_bridge_port=fake_codex_api.server_address[1],
    )
    payload = _payload(workspace, request_id=f"phase5f1a-tampered-{mutation}")
    payload.update(
        {
            "workflow_yaml": _three_job_yaml(),
            "input": _THREE_JOB_INPUT,
            "max_live_codex_jobs": 3,
        }
    )
    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        accepted = await client.post(
            _PATH,
            headers={"authorization": f"Bearer {_TOKEN}"},
            json=payload,
        )
    assert accepted.status_code == 202
    run_dir = manager.start_calls[0][0]
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if mutation == "input-hash":
        input_data = json.loads((run_dir / "input.json").read_text(encoding="utf-8"))
        input_data["issues"][0]["number"] = 18
        (run_dir / "input.json").write_text(json.dumps(input_data), encoding="utf-8")
    elif mutation == "request-digest":
        manifest["request_digest"] = "f" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    else:
        manifest["control"]["max_live_codex_jobs"] = 3.0
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    from codex_bridge.workflows.interpreter import OwsError
    from codex_bridge.workflows.worker import load_run_request

    with pytest.raises(OwsError):
        load_run_request(run_dir)

    assert fake_codex_api.state.posts == []  # type: ignore[attr-defined]
    assert not (run_dir / "codex_jobs.sqlite").exists()
    assert not (run_dir / "dbos.sqlite").exists()


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
