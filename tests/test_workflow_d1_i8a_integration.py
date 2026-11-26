from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx2 as httpx
import pytest

from codex_bridge.activity import ActivityStore
from codex_bridge.config import BridgeConfig
from codex_bridge.ui_api import create_ui_app
from codex_bridge.workflows.codex_job_adapter import CodexJobAdapter
from codex_bridge.workflows.definitions import WorkflowDefinitionRepository
from codex_bridge.workflows.interpreter import SAFE_CODEX_JOB_PROMPT
from codex_bridge.workflows.manager import WorkflowWorkerManager
from codex_bridge.workflows.service import WorkflowService, _find_workflow, _inspect_run
from codex_bridge.workflows.strict_definition_repository import StrictWorkflowDefinitionRepository
from codex_bridge.workflows.strict_definitions import StrictWorkflowDefinitionStore
from codex_bridge.workflows.strict_requests import StrictWorkflowRequestStore
from tests.test_workflow_d1_i7b_variable_runtime import _contract, _source
from tests.test_workflow_definition_api import _source as _legacy_source
from tests.test_workflow_mcp import FakeRuntime, _mcp_client
from tests.test_workflow_mcp import _config as _mcp_config
from tests.test_workflows_api import FakeBridge

_CONTROL_TOKEN = "i8a-isolated-loopback-control-token"


def test_service_job_identities_match_interpreter_paths_and_profile_three_input_order() -> None:
    from codex_bridge.workflows.interpreter import _non_looped_codex_job_task_identities
    from codex_bridge.workflows.service import (
        _three_job_task_identities,
        _two_job_task_identities,
    )
    from tests.test_workflows_multijob import (
        _THREE_ITEM_INPUT,
        _THREE_JOB_INPUT,
        _three_item_workflow,
        _three_job_workflow,
    )

    nested_single = {
        "do": [{"outer": {"do": [{"dispatch": {"call": "codex-job", "with": {"prompt": "p"}}}]}}]
    }
    assert _non_looped_codex_job_task_identities(nested_single) == ("do[0].outer.do[0].dispatch",)
    two_jobs = {
        "do": [
            {"first": {"call": "codex-job", "with": {"prompt": "p"}}},
            {"second": {"call": "codex-job", "with": {"prompt": "p"}}},
        ]
    }
    assert _two_job_task_identities(two_jobs) == ("do[0].first", "do[1].second")
    assert _three_job_task_identities(_three_job_workflow(), _THREE_JOB_INPUT) == (
        "do[0].processIssues.iteration[1].do[1].processIssue",
        "do[0].processIssues.iteration[2].do[1].processIssue",
        "do[0].processIssues.iteration[3].do[1].processIssue",
    )
    assert _three_job_task_identities(_three_item_workflow(), _THREE_ITEM_INPUT) == (
        "do[0].processItems.iteration[0].do[1].processItem",
        "do[0].processItems.iteration[2].do[1].processItem",
    )


class _FakeCodexHandler(BaseHTTPRequestHandler):
    calls: list[tuple[str, str]]

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _respond(self, value: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        self.calls.append(("GET", path))
        if path == "/healthz":
            self._respond({"status": "ok"})
        elif path == "/ui-api/status":
            self._respond({"app_server": "ready"})
        elif path == "/ui-api/control/capabilities":
            self._respond({})
        elif path.endswith("/status"):
            self._respond(
                {
                    "thread_id": "thread-i8a-12345678",
                    "turn_id": "turn-i8a-12345678",
                    "state": "completed",
                }
            )
        elif path.endswith("/turns"):
            self._respond(
                {
                    "thread_id": "thread-i8a-12345678",
                    "turns": [{"id": "turn-i8a-12345678", "status": "completed"}],
                }
            )
        elif path.endswith("/items"):
            self._respond(
                {
                    "thread_id": "thread-i8a-12345678",
                    "items": [
                        {
                            "turn_id": "turn-i8a-12345678",
                            "item": {
                                "type": "agentMessage",
                                "phase": "final_answer",
                                "text": "isolated loopback response",
                            },
                        }
                    ],
                }
            )
        else:
            self._respond({"error": "not_found"}, status=404)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        self.calls.append(("POST", path))
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        request = json.loads(body or b"{}")
        if (
            path != "/ui-api/control/threads"
            or self.headers.get("Authorization") != f"Bearer {_CONTROL_TOKEN}"
            or request.get("prompt") != SAFE_CODEX_JOB_PROMPT
        ):
            self._respond({"error": "invalid_fake_request"}, status=400)
            return
        self._respond(
            {
                "thread_id": "thread-i8a-12345678",
                "turn_id": "turn-i8a-12345678",
                "state": "in_progress",
            }
        )


@pytest.mark.asyncio
async def test_isolated_strict_saved_run_http_and_mcp_replay_use_one_worker_and_one_fake_post(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    StrictWorkflowRequestStore(state_root).initialize_new_database()
    store = StrictWorkflowDefinitionStore(state_root)
    store.initialize_new_database()
    revision = store.create(
        mutation_request_id="i8a-definition-create-0001",
        source_yaml=_source(
            [
                {
                    "calculate": {
                        "call": "codex-job",
                        "with": {"prompt": SAFE_CODEX_JOB_PROMPT},
                    }
                }
            ],
            output="${ {} }",
        ),
        max_live_codex_jobs=1,
        contract=_contract(),
    )
    repository: WorkflowDefinitionRepository = StrictWorkflowDefinitionRepository(state_root)

    fake_server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeCodexHandler)
    _FakeCodexHandler.calls = []
    fake_port = int(fake_server.server_address[1])
    fake_thread = threading.Thread(target=fake_server.serve_forever, daemon=True)
    fake_thread.start()

    manager = WorkflowWorkerManager(
        state_root=state_root,
        allowed_roots=(str(workspace.resolve()),),
        control_token=_CONTROL_TOKEN,
        python_executable=sys.executable,
        frozen=False,
        graceful_shutdown_seconds=0.5,
        enable_isolated_strict_runtime=True,
        enable_isolated_strict_codex_jobs=True,
    )
    service = WorkflowService(
        manager,
        codex_bridge_port=fake_port,
        enable_isolated_strict_runtime=True,
        enable_isolated_strict_codex_jobs=True,
    )
    settings = BridgeConfig.from_sources(
        explicit_allowed_roots=(str(tmp_path),),
        environ={},
        config_data={},
    )
    settings = replace(settings, ui_port=18001, control_token=_CONTROL_TOKEN)
    http_app = create_ui_app(
        FakeBridge(),
        ActivityStore(),
        settings,
        workflow_service=service,
        workflow_definitions=repository,
    )
    request = {
        "client_request_id": "i8a-http-mcp-replay-0001",
        "revision": revision.revision,
        "workspace_root": str(workspace.resolve()),
        "input": {},
    }
    endpoint = f"/ui-api/control/workflow-definitions/{revision.definition_id}/runs"
    auth = {"authorization": f"Bearer {_CONTROL_TOKEN}"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=http_app, client=("127.0.0.1", 1234)),
            base_url="http://localhost",
        ) as client:
            started = await client.post(endpoint, headers=auth, json=request)
            assert started.status_code == 202, started.text
            workflow_id = started.json()["workflow_id"]

            deadline = asyncio.get_running_loop().time() + 45
            while asyncio.get_running_loop().time() < deadline:
                detail = await client.get(f"/ui-api/control/workflows/{workflow_id}", headers=auth)
                if detail.status_code == 200 and detail.json().get("status") in {
                    "succeeded",
                    "failed",
                }:
                    break
                await asyncio.sleep(0.05)
            assert detail.status_code == 200, detail.text
            assert detail.json()["status"] == "succeeded", detail.text
            listed = await client.get("/ui-api/control/workflows", headers=auth)
            result = await client.get(
                f"/ui-api/control/workflows/{workflow_id}/result", headers=auth
            )
            assert listed.status_code == result.status_code == 200
            assert listed.json()["workflows"][0]["workflow_id"] == workflow_id
            assert result.json()["status"] == "succeeded"
            assert result.json()["result"] == {}

        invalid_revision = store.create(
            mutation_request_id="i8a-definition-create-0002",
            source_yaml=_source(
                [
                    {
                        "calculate": {
                            "call": "codex-job",
                            "with": {"prompt": SAFE_CODEX_JOB_PROMPT},
                        }
                    },
                    {"invalid_output": {"set": '${ {"unexpected": true} }'}},
                ]
            ),
            max_live_codex_jobs=1,
            contract=_contract(),
        )
        invalid_request = {
            **request,
            "client_request_id": "i8a-output-invalid-0001",
            "revision": invalid_revision.revision,
        }
        invalid_endpoint = (
            f"/ui-api/control/workflow-definitions/{invalid_revision.definition_id}/runs"
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=http_app, client=("127.0.0.1", 1234)),
            base_url="http://localhost",
        ) as client:
            invalid_started = await client.post(
                invalid_endpoint, headers=auth, json=invalid_request
            )
            assert invalid_started.status_code in {202, 503}, invalid_started.text
            invalid_workflow_id = invalid_started.json()["workflow_id"]
            deadline = asyncio.get_running_loop().time() + 45
            while asyncio.get_running_loop().time() < deadline:
                invalid_detail = await client.get(
                    f"/ui-api/control/workflows/{invalid_workflow_id}", headers=auth
                )
                if (
                    invalid_detail.status_code == 200
                    and invalid_detail.json().get("status") == "failed"
                ):
                    break
                await asyncio.sleep(0.05)
            assert invalid_detail.status_code == 200, invalid_detail.text
            assert invalid_detail.json()["status"] == "failed", invalid_detail.text
            invalid_result = await client.get(
                f"/ui-api/control/workflows/{invalid_workflow_id}/result", headers=auth
            )
            assert invalid_result.status_code == 409
            assert invalid_result.json()["error"] == "workflow_output_invalid"

        legacy_revision = repository.create(
            mutation_request_id="i8a-legacy-prepared-v2-create-0001",
            source_yaml=_legacy_source("i8a-legacy-prepared-v2", SAFE_CODEX_JOB_PROMPT),
            max_live_codex_jobs=1,
        )
        legacy_request = {
            "client_request_id": "i8a-legacy-prepared-v2-run-0001",
            "revision": legacy_revision.revision,
            "workspace_root": str(workspace.resolve()),
            "input": {},
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=http_app, client=("127.0.0.1", 1234)),
            base_url="http://localhost",
        ) as client:
            legacy_started = await client.post(
                f"/ui-api/control/workflow-definitions/{legacy_revision.definition_id}/runs",
                headers=auth,
                json=legacy_request,
            )
            assert legacy_started.status_code == 202, legacy_started.text
            legacy_workflow_id = legacy_started.json()["workflow_id"]
            deadline = asyncio.get_running_loop().time() + 45
            while asyncio.get_running_loop().time() < deadline:
                legacy_detail = await client.get(
                    f"/ui-api/control/workflows/{legacy_workflow_id}", headers=auth
                )
                if legacy_detail.status_code == 200 and legacy_detail.json().get("status") in {
                    "succeeded",
                    "failed",
                }:
                    break
                await asyncio.sleep(0.05)
            assert legacy_detail.status_code == 200, legacy_detail.text
            assert legacy_detail.json()["status"] == "succeeded", legacy_detail.text
            legacy_result = await client.get(
                f"/ui-api/control/workflows/{legacy_workflow_id}/result", headers=auth
            )
            assert legacy_result.status_code == 200, legacy_result.text
            legacy_http_result = legacy_result.json()
            assert isinstance(legacy_http_result, dict)
            assert not {
                "run_format_version",
                "contract_version",
                "contract_sha256",
                "binding_digest",
            }.intersection(legacy_detail.json())

        runtime = FakeRuntime(service)
        runtime.bridge.app_server_ready = True
        runtime.workflow_definitions = repository
        mcp_app = __import__("codex_bridge.server", fromlist=["create_app"]).create_app(
            _mcp_config(tmp_path, ("read", "execute")),
            runtime_factory=lambda _config: runtime,
        )
        async with _mcp_client(mcp_app) as mcp:
            replay = await mcp.call_tool(
                "workflow_run_start",
                {
                    "definition_id": revision.definition_id,
                    **request,
                },
            )
            mcp_detail = await mcp.call_tool("workflow_run_get", {"workflow_id": workflow_id})
            mcp_result = await mcp.call_tool("workflow_run_result", {"workflow_id": workflow_id})
            mcp_invalid_result = await mcp.call_tool(
                "workflow_run_result", {"workflow_id": invalid_workflow_id}
            )
            legacy_replay = await mcp.call_tool(
                "workflow_run_start",
                {"definition_id": legacy_revision.definition_id, **legacy_request},
            )
            legacy_mcp_detail = await mcp.call_tool(
                "workflow_run_get", {"workflow_id": legacy_workflow_id}
            )
            legacy_mcp_result = await mcp.call_tool(
                "workflow_run_result", {"workflow_id": legacy_workflow_id}
            )
            mcp_list = await mcp.call_tool("workflow_runs_list", {})

        assert replay.structured_content["workflow_id"] == workflow_id
        assert replay.structured_content["status"] == "succeeded"
        assert mcp_detail.structured_content["status"] == "succeeded"
        assert mcp_result.structured_content["status"] == "succeeded"
        assert mcp_result.structured_content["result"] == {}
        assert mcp_invalid_result.structured_content["error"] == "workflow_output_invalid"
        assert legacy_replay.structured_content["workflow_id"] == legacy_workflow_id
        assert legacy_replay.structured_content["status"] == "succeeded"
        assert legacy_mcp_detail.structured_content["status"] == "succeeded"
        assert legacy_mcp_result.structured_content == legacy_http_result
        assert not {
            "run_format_version",
            "contract_version",
            "contract_sha256",
            "binding_digest",
        }.intersection(legacy_mcp_detail.structured_content)
        assert not {
            "run_format_version",
            "contract_version",
            "contract_sha256",
            "binding_digest",
        }.intersection(legacy_http_result)
        assert {item["workflow_id"] for item in mcp_list.structured_content["workflows"]} == {
            workflow_id,
            invalid_workflow_id,
            legacy_workflow_id,
        }
        assert len(manager._records) == 3
        assert [method for method, path in _FakeCodexHandler.calls if method == "POST"] == [
            "POST",
            "POST",
            "POST",
        ]
    finally:
        await manager.shutdown()
        fake_server.shutdown()
        fake_thread.join(timeout=5)
        fake_server.server_close()


@pytest.mark.asyncio
async def test_strict_worker_recovers_post_commit_process_death_before_dbos_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codex_bridge.workflows.strict_variable_runtime import (
        StrictWorkflowVariableRuntimeAdapter,
    )
    from tests.test_workflow_d1_i7b_variable_runtime import _reserved_adapter

    contract = _contract()
    output_schema = contract["output_schema"]
    assert isinstance(output_schema, dict)
    contract["output_schema"] = {
        **output_schema,
        "properties": {"count": {"type": "integer"}},
        "required": ["count"],
    }
    tasks = [
        {
            "initialize": {
                "call": "codexbridge-variable",
                "with": {
                    "operation": "initialize",
                    "declaration_id": "count",
                    "scope": "workflow",
                    "value": 0,
                },
            }
        },
        {
            "increment": {
                "call": "codexbridge-variable",
                "with": {
                    "operation": "increment",
                    "declaration_id": "count",
                    "scope": "workflow",
                    "by": 1,
                },
            }
        },
        {
            "increment_again": {
                "call": "codexbridge-variable",
                "with": {
                    "operation": "increment",
                    "declaration_id": "count",
                    "scope": "workflow",
                    "by": 1,
                },
            }
        },
        {"capture": {"set": '${ {"count": $cbvars.workflow.count} }'}},
    ]
    adapter, run_dir = _reserved_adapter(
        tmp_path,
        tasks=tasks,
        contract=contract,
    )
    assert isinstance(adapter, StrictWorkflowVariableRuntimeAdapter)
    workspace = run_dir.parent.parent / "workspace"

    hook_dir = tmp_path / "worker-hook"
    hook_dir.mkdir()
    (hook_dir / "sitecustomize.py").write_text(
        """
import time
from pathlib import Path

try:
    from codex_bridge.workflows import strict_executor
    original = strict_executor._apply_mutation_checkpoint_body
    hook_dir = Path(__file__).parent
    marker = hook_dir / "mutation-committed"
    release = hook_dir / "allow-checkpoint"
    replay = hook_dir / "historical-replay"
    def after_commit(*args, **kwargs):
        result = original(*args, **kwargs)
        node_id = args[5] if len(args) > 5 else kwargs.get("node_id")
        if node_id == "/do/1/increment":
            if not marker.exists():
                marker.write_text("committed", encoding="utf-8")
                while not release.exists():
                    time.sleep(0.05)
            else:
                replay.write_text("replayed", encoding="utf-8")
        return result
    strict_executor._apply_mutation_checkpoint_body = after_commit
except Exception as error:
    (Path(__file__).parent / "hook-failure").write_text(
        type(error).__name__, encoding="ascii"
    )
""",
        encoding="utf-8",
    )
    old_pythonpath = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv(
        "PYTHONPATH",
        str(hook_dir) + (os.pathsep + old_pythonpath if old_pythonpath else ""),
    )

    manager = WorkflowWorkerManager(
        state_root=run_dir.parent,
        allowed_roots=(str(workspace.resolve()),),
        python_executable=sys.executable,
        frozen=False,
        graceful_shutdown_seconds=0.5,
        enable_isolated_strict_runtime=True,
    )
    try:
        first = await manager.start_worker(
            run_dir,
            mode="start",
            workspace_root=workspace,
            max_live_codex_jobs=1,
            strict_runtime=True,
        )
        deadline = asyncio.get_running_loop().time() + 60
        marker = hook_dir / "mutation-committed"
        while asyncio.get_running_loop().time() < deadline and not marker.exists():
            if (hook_dir / "hook-failure").exists():
                break
            await asyncio.sleep(0.05)
        assert not (hook_dir / "hook-failure").exists(), "worker test hook did not install"
        assert marker.is_file(), "worker did not reach the post-commit checkpoint gate"

        committed = adapter._store.inspect_ledger()
        assert committed.mutation_count == 2
        assert [item.after_value for item in committed.mutations] == [0, 1]
        first_process = manager._records[run_dir.name].process
        assert first_process is not None and first_process.returncode is None
        first_process.terminate()
        await first.wait(15)

        recovered = await manager.start_worker(
            run_dir,
            mode="recover",
            workspace_root=workspace,
            max_live_codex_jobs=1,
            strict_runtime=True,
        )
        recovered_status = await recovered.wait(90)
        assert recovered_status.state == "completed"
        assert (hook_dir / "historical-replay").read_text(encoding="utf-8") == "replayed"
        ledger = adapter._store.inspect_ledger()
        assert ledger.mutation_count == 3
        assert [item.after_value for item in ledger.mutations] == [0, 1, 2]

        service = WorkflowService(
            manager,
            codex_bridge_port=18001,
            enable_isolated_strict_runtime=True,
        )
        result = await service.get_result(str(adapter.checkpoint_identity["workflow_id"]))
        assert result.workflow.status == "succeeded"
        assert result.result is not None and result.result["result"] == {"count": 2}
        assert not (run_dir / "codex_jobs.sqlite").exists()
    finally:
        await manager.shutdown()


@pytest.mark.parametrize("job_state", ["PREPARED", "ACCEPTED", "RUNNING", "MAYBE_SENT"])
def test_strict_terminal_failure_with_pending_codex_job_remains_reconciliation(
    tmp_path: Path,
    job_state: str,
) -> None:
    from tests.test_workflow_d1_i7b_variable_runtime import _reserved_adapter

    adapter, run_dir = _reserved_adapter(
        tmp_path,
        tasks=[
            {
                "calculate": {
                    "call": "codex-job",
                    "with": {"prompt": SAFE_CODEX_JOB_PROMPT},
                }
            }
        ],
    )
    workflow_id = str(adapter.checkpoint_identity["workflow_id"])
    ledger = CodexJobAdapter(run_dir / "codex_jobs.sqlite")
    job = ledger.prepare_job(
        workflow_id,
        "do[0].calculate",
        SAFE_CODEX_JOB_PROMPT,
        run_dir,
        port=18001,
    )
    if job_state != "PREPARED":
        assert ledger.claim_post(job["job_id"])
        if job_state != "MAYBE_SENT":
            ledger.mark_accepted(
                job["job_id"], "thread-i8a-12345678", "turn-i8a-12345678", "in_progress"
            )
            if job_state == "RUNNING":
                ledger._mark_running(job["job_id"], "in_progress")

    (run_dir / "worker-entry-status.json").write_text(
        json.dumps({"state": "failed", "exit_code": 1, "error_code": None}),
        encoding="utf-8",
    )
    (run_dir / "start-worker.json").write_text(
        json.dumps(
            {
                "mode": "start",
                "pid": 1234,
                "run_dir": str(run_dir),
                "workflow_id": workflow_id,
                "creation_time_100ns": 1,
                "image_path": "worker.exe",
            }
        ),
        encoding="utf-8",
    )
    with sqlite3.connect(run_dir / "dbos.sqlite") as connection:
        connection.execute(
            "CREATE TABLE workflow_status (workflow_uuid TEXT NOT NULL, status TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO workflow_status VALUES (?, 'ERROR')", (workflow_id,))
    (run_dir / "strict-terminal.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "status": "failed",
                "reason_code": "strict_execution_invalid",
                "run_id": run_dir.name,
                "workflow_id": workflow_id,
                "binding_identity": adapter.checkpoint_identity,
            }
        ),
        encoding="utf-8",
    )

    row = _find_workflow(run_dir.parent, workflow_id)
    assert row is not None
    inspection = _inspect_run(
        run_dir.parent,
        row,
        allowed_roots=(str(run_dir.parent.parent / "workspace"),),
    )

    assert inspection.status == "needs_reconciliation"
    assert inspection.result is None
    assert inspection.jobs[0].state == job_state
