from __future__ import annotations

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
from codex_bridge.workflows.interpreter import SAFE_CODEX_JOB_PROMPT
from codex_bridge.workflows.service import WorkflowService

_TOKEN = "phase5c-test-bearer-token"
_PATH = "/ui-api/control/workflows"


class FakeBridge:
    app_server_ready = True
    app_server_os_pid = 4567


class FakeWorkerManager:
    def __init__(self, state_root: Path, allowed_root: Path) -> None:
        self.state_root = state_root
        self.allowed_roots = (str(allowed_root),)
        self.start_calls: list[Path] = []

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
        assert workspace_root is not None
        self.start_calls.append(run_dir)
        (run_dir / "worker-entry-status.json").write_text(
            json.dumps({"state": "running", "exit_code": None, "error_code": None}),
            encoding="utf-8",
        )
        return object()


def _settings(tmp_path: Path, *, token: str | None = _TOKEN) -> BridgeConfig:
    return BridgeConfig(
        host="127.0.0.1",
        port=18000,
        ui_port=18001,
        allowed_roots=(str(tmp_path),),
        allowed_hosts=(),
        allowed_origins=(),
        codex_executable="codex",
        wait_default_seconds=18.0,
        wait_max_seconds=30.0,
        shutdown_grace_seconds=3.0,
        control_token=token,
    )


def _workflow_yaml(prompt: Any = SAFE_CODEX_JOB_PROMPT) -> str:
    return "\n".join(
        (
            "document:",
            '  dsl: "1.0.3"',
            "  namespace: codexbridge",
            "  name: fixed-codex-job",
            '  version: "1.0.0"',
            "evaluate:",
            "  language: jq",
            "  mode: strict",
            "do:",
            "  - calculate:",
            "      call: codex-job",
            "      with:",
            "        prompt: " + json.dumps(prompt, ensure_ascii=True),
            "",
        )
    )


def _app(
    tmp_path: Path,
    *,
    token: str | None = _TOKEN,
    app_server_ready: bool = True,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    manager = FakeWorkerManager(tmp_path / "state" / "workflows", tmp_path)
    service = WorkflowService(manager, codex_bridge_port=18001)
    bridge = FakeBridge()
    bridge.app_server_ready = app_server_ready
    app = create_ui_app(
        bridge,
        ActivityStore(),
        _settings(tmp_path, token=token),
        workflow_service=service,
    )
    return app, manager, workspace


def _transport(app):
    return httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))


def _valid_payload(workspace: Path) -> dict[str, Any]:
    return {
        "client_request_id": "phase5c-request-api-01",
        "workflow_yaml": _workflow_yaml(),
        "input": {"value": 4},
        "workspace_root": str(workspace),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", _PATH),
        ("GET", _PATH),
        ("GET", _PATH + "/00000000-0000-4000-8000-000000000001"),
        ("GET", _PATH + "/00000000-0000-4000-8000-000000000001/result"),
    ],
)
@pytest.mark.parametrize("authorization", [None, "Bearer wrong-token"])
async def test_all_workflow_endpoints_require_bearer_token(
    tmp_path: Path,
    method: str,
    path: str,
    authorization: str | None,
) -> None:
    app, _manager, workspace = _app(tmp_path)
    headers = {} if authorization is None else {"authorization": authorization}
    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        response = await client.request(
            method,
            path,
            headers=headers,
            json=_valid_payload(workspace) if method == "POST" else None,
        )

    assert response.status_code == 403
    assert _TOKEN not in response.text
    assert "wrong-token" not in response.text


@pytest.mark.asyncio
async def test_workflow_routes_are_not_registered_without_control_token(tmp_path: Path) -> None:
    app, _manager, _workspace = _app(tmp_path, token=None)

    paths = {getattr(route, "path", None) for route in app.routes}

    assert _PATH not in paths
    assert _PATH + "/{workflow_id}" not in paths
    assert _PATH + "/{workflow_id}/result" not in paths


@pytest.mark.asyncio
async def test_workflow_start_requires_ready_app_server(tmp_path: Path) -> None:
    app, manager, workspace = _app(tmp_path, app_server_ready=False)
    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        response = await client.post(
            _PATH,
            headers={"authorization": f"Bearer {_TOKEN}"},
            json=_valid_payload(workspace),
        )

    assert response.status_code == 503
    assert response.json() == {"error": "bridge_not_ready"}
    assert manager.start_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", _PATH),
        ("GET", _PATH),
        ("GET", _PATH + "/00000000-0000-4000-8000-000000000001"),
        ("GET", _PATH + "/00000000-0000-4000-8000-000000000001/result"),
    ],
)
async def test_all_workflow_endpoints_reject_mismatched_localhost_origin(
    tmp_path: Path,
    method: str,
    path: str,
) -> None:
    app, _manager, workspace = _app(tmp_path)
    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        response = await client.request(
            method,
            path,
            headers={"authorization": f"Bearer {_TOKEN}", "origin": "http://localhost:18002"},
            json=_valid_payload(workspace) if method == "POST" else None,
        )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_workflow_post_returns_durable_run_and_status_is_redacted(tmp_path: Path) -> None:
    app, manager, workspace = _app(tmp_path)
    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        accepted = await client.post(
            _PATH,
            headers={"authorization": f"Bearer {_TOKEN}"},
            json=_valid_payload(workspace),
        )
        assert accepted.status_code == 202
        started = accepted.json()
        detail = await client.get(
            _PATH + "/" + started["workflow_id"],
            headers={"authorization": f"Bearer {_TOKEN}"},
        )
        listing = await client.get(_PATH, headers={"authorization": f"Bearer {_TOKEN}"})

    assert started["status"] == "running"
    assert detail.status_code == 200
    assert detail.json()["status"] == "running"
    assert listing.status_code == 200
    assert len(listing.json()["workflows"]) == 1
    assert set(listing.json()["workflows"][0]) == {
        "workflow_id",
        "status",
        "created_at",
        "updated_at",
    }
    assert len(manager.start_calls) == 1
    for response in (accepted, detail, listing):
        assert _TOKEN not in response.text
        assert str(workspace) not in response.text
        assert _workflow_yaml() not in response.text


@pytest.mark.asyncio
async def test_result_route_covers_running_reconciliation_success_and_missing(
    tmp_path: Path,
) -> None:
    app, manager, workspace = _app(tmp_path)
    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        accepted = await client.post(
            _PATH,
            headers={"authorization": f"Bearer {_TOKEN}"},
            json=_valid_payload(workspace),
        )
        workflow_id = accepted.json()["workflow_id"]
        result_path = _PATH + "/" + workflow_id + "/result"
        headers = {"authorization": f"Bearer {_TOKEN}"}
        running = await client.get(result_path, headers=headers)
        missing = await client.get(
            _PATH + "/00000000-0000-4000-8000-000000000002/result",
            headers=headers,
        )

        run_dir = manager.start_calls[0]
        connection = sqlite3.connect(run_dir / "codex_jobs.sqlite")
        try:
            connection.execute(
                "CREATE TABLE jobs (job_id TEXT, workflow_id TEXT, state TEXT, "
                "thread_id TEXT, turn_id TEXT)"
            )
            connection.execute(
                "INSERT INTO jobs VALUES ('job-1', ?, 'MAYBE_SENT', NULL, NULL)",
                (workflow_id,),
            )
            connection.commit()
        finally:
            connection.close()
        reconciliation = await client.get(result_path, headers=headers)

        (run_dir / "codex_jobs.sqlite").unlink()
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        (run_dir / "worker-entry-status.json").write_text(
            json.dumps({"state": "completed", "exit_code": 0, "error_code": None}),
            encoding="utf-8",
        )
        connection = sqlite3.connect(run_dir / "dbos.sqlite")
        try:
            connection.execute(
                "CREATE TABLE workflow_status (workflow_uuid TEXT PRIMARY KEY, "
                "status TEXT NOT NULL)"
            )
            connection.execute("INSERT INTO workflow_status VALUES (?, 'SUCCESS')", (workflow_id,))
            connection.commit()
        finally:
            connection.close()
        stored_result = {
            "workflow_id": workflow_id,
            "result": {"final_text": "4"},
            "definition_sha256": manifest["definition_sha256"],
            "input_sha256": manifest["input_sha256"],
        }
        (run_dir / "result.json").write_text(json.dumps(stored_result), encoding="utf-8")
        succeeded = await client.get(result_path, headers=headers)

    assert accepted.status_code == 202
    assert running.status_code == 202
    assert missing.status_code == 404
    assert reconciliation.status_code == 409
    assert reconciliation.json() == {"error": "workflow_needs_reconciliation"}
    assert succeeded.status_code == 200
    assert succeeded.json() == stored_result
    assert len(manager.start_calls) == 1


@pytest.mark.asyncio
async def test_success_transition_keeps_api_running_until_worker_entry_completes(
    tmp_path: Path,
) -> None:
    app, manager, workspace = _app(tmp_path)
    headers = {"authorization": f"Bearer {_TOKEN}"}
    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        accepted = await client.post(_PATH, headers=headers, json=_valid_payload(workspace))
        workflow_id = accepted.json()["workflow_id"]
        detail_path = _PATH + "/" + workflow_id
        result_path = detail_path + "/result"
        initial_detail = await client.get(detail_path, headers=headers)
        initial_listing = await client.get(_PATH, headers=headers)
        initial_result = await client.get(result_path, headers=headers)

        run_dir = manager.start_calls[0]
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        with sqlite3.connect(run_dir / "dbos.sqlite") as connection:
            connection.execute(
                "CREATE TABLE workflow_status (workflow_uuid TEXT PRIMARY KEY, "
                "status TEXT NOT NULL)"
            )
            connection.execute("INSERT INTO workflow_status VALUES (?, 'SUCCESS')", (workflow_id,))
        dbos_success_result = await client.get(result_path, headers=headers)

        stored_result = {
            "workflow_id": workflow_id,
            "result": {"final_text": "4"},
            "definition_sha256": manifest["definition_sha256"],
            "input_sha256": manifest["input_sha256"],
        }
        (run_dir / "result.json").write_text(json.dumps(stored_result), encoding="utf-8")
        result_saved_detail = await client.get(detail_path, headers=headers)
        result_saved_listing = await client.get(_PATH, headers=headers)
        result_saved = await client.get(result_path, headers=headers)

        (run_dir / "worker-entry-status.json").write_text(
            json.dumps({"state": "completed", "exit_code": 0, "error_code": None}),
            encoding="utf-8",
        )
        completed_detail = await client.get(detail_path, headers=headers)
        completed_listing = await client.get(_PATH, headers=headers)
        completed_result = await client.get(result_path, headers=headers)

    assert accepted.status_code == 202
    assert initial_detail.status_code == 200
    assert initial_detail.json()["status"] == "running"
    assert initial_listing.status_code == 200
    assert initial_listing.json()["workflows"][0]["status"] == "running"
    assert initial_result.status_code == 202
    assert dbos_success_result.status_code == 202
    assert result_saved_detail.status_code == 200
    assert result_saved_detail.json()["status"] == "running"
    assert result_saved_listing.status_code == 200
    assert result_saved_listing.json()["workflows"][0]["status"] == "running"
    assert result_saved.status_code == 202
    assert completed_detail.status_code == 200
    assert completed_detail.json()["status"] == "succeeded"
    assert completed_listing.status_code == 200
    assert completed_listing.json()["workflows"][0]["status"] == "succeeded"
    assert completed_result.status_code == 200
    assert completed_result.json() == stored_result
    assert len(manager.start_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "body"),
    [
        ({"content-type": "text/plain"}, b"{}"),
        ({"content-type": "application/json"}, b'{"client_request_id":"abcdefgh","input":NaN}'),
        ({"content-type": "application/json"}, b"{" + b" " * (128 * 1024) + b"}"),
    ],
    ids=["wrong_content_type", "non_json_constant", "oversized_body"],
)
async def test_workflow_post_rejects_content_type_non_json_and_oversized_body(
    tmp_path: Path,
    headers: dict[str, str],
    body: bytes,
) -> None:
    app, manager, _workspace = _app(tmp_path)
    request_headers = {"authorization": f"Bearer {_TOKEN}", **headers}
    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        response = await client.post(_PATH, headers=request_headers, content=body)

    assert response.status_code == 400
    assert len(manager.start_calls) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "extra_key",
        "missing_workspace",
        "invalid_request_id",
        "invalid_prompt",
        "outside_root",
        "path_traversal",
        "yaml_too_large",
    ],
)
async def test_workflow_post_rejects_invalid_shape_and_workspace(
    tmp_path: Path,
    mutation: str,
) -> None:
    app, manager, workspace = _app(tmp_path)
    payload = _valid_payload(workspace)
    if mutation == "extra_key":
        payload["unexpected"] = True
    elif mutation == "missing_workspace":
        payload.pop("workspace_root")
    elif mutation == "invalid_request_id":
        payload["client_request_id"] = "short"
    elif mutation == "invalid_prompt":
        payload["workflow_yaml"] = _workflow_yaml(" \n\t")
    elif mutation == "outside_root":
        payload["workspace_root"] = str(tmp_path.parent)
    elif mutation == "path_traversal":
        payload["workspace_root"] = str(workspace / ".." / "outside")
    elif mutation == "yaml_too_large":
        payload["workflow_yaml"] += "#" * (64 * 1024)
    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        response = await client.post(
            _PATH,
            headers={"authorization": f"Bearer {_TOKEN}"},
            json=payload,
        )

    assert response.status_code == 400
    assert len(manager.start_calls) == 0


@pytest.mark.asyncio
async def test_workflow_post_accepts_literal_japanese_prompt_and_replays_once(
    tmp_path: Path,
) -> None:
    app, manager, workspace = _app(tmp_path)
    prompt = "日本の首都を一語で答えて"
    payload = _valid_payload(workspace)
    payload["workflow_yaml"] = _workflow_yaml(prompt)
    headers = {"authorization": f"Bearer {_TOKEN}"}

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        accepted = await client.post(_PATH, headers=headers, json=payload)
        replayed = await client.post(_PATH, headers=headers, json=payload)
        changed = {**payload, "workflow_yaml": _workflow_yaml("異なるprompt")}
        conflict = await client.post(_PATH, headers=headers, json=changed)

    assert accepted.status_code == 202
    assert replayed.status_code == 202
    assert replayed.json()["workflow_id"] == accepted.json()["workflow_id"]
    assert replayed.json()["run_id"] == accepted.json()["run_id"]
    assert conflict.status_code == 409
    assert len(manager.start_calls) == 1
    for response in (accepted, replayed, conflict):
        assert prompt not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prompt",
    [
        "",
        " \n\t",
        True,
        1,
        [],
        {},
        None,
        "private-marker\u0001prompt",
        "private-marker\u007fprompt",
        "private-marker\u0085prompt",
        "private-marker\ud800prompt",
        "x" * (16 * 1024 + 1),
    ],
)
async def test_workflow_post_rejects_invalid_prompt_without_worker_or_prompt_echo(
    tmp_path: Path,
    prompt: object,
) -> None:
    app, manager, workspace = _app(tmp_path)
    payload = _valid_payload(workspace)
    payload["workflow_yaml"] = _workflow_yaml(prompt)

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        response = await client.post(
            _PATH,
            headers={"authorization": f"Bearer {_TOKEN}"},
            json=payload,
        )

    assert response.status_code == 400
    assert len(manager.start_calls) == 0
    assert "private-marker" not in response.text


@pytest.mark.asyncio
async def test_workflow_post_accepts_prompt_at_exact_utf8_byte_limit(tmp_path: Path) -> None:
    app, manager, workspace = _app(tmp_path)
    prompt = "あ" * 5461 + "x"
    assert len(prompt.encode("utf-8")) == 16 * 1024
    payload = _valid_payload(workspace)
    payload["workflow_yaml"] = _workflow_yaml(prompt)

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        response = await client.post(
            _PATH,
            headers={"authorization": f"Bearer {_TOKEN}"},
            json=payload,
        )

    assert response.status_code == 202
    assert len(manager.start_calls) == 1


@pytest.mark.asyncio
async def test_workflow_post_rejects_symlink_escape(tmp_path: Path) -> None:
    app, manager, _workspace = _app(tmp_path)
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    escaped = tmp_path.parent / f"{tmp_path.name}-outside"
    escaped.mkdir()
    link = allowed / "escape"
    try:
        link.symlink_to(escaped, target_is_directory=True)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"directory symlink unavailable: {error}")
    payload = _valid_payload(allowed)
    payload["workspace_root"] = str(link)
    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        response = await client.post(
            _PATH,
            headers={"authorization": f"Bearer {_TOKEN}"},
            json=payload,
        )

    assert response.status_code == 400
    assert len(manager.start_calls) == 0
