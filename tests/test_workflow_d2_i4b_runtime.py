from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import yaml

from codex_bridge.config import BridgeConfig, ConfigurationError
from codex_bridge.workflows.contracts import compile_contract
from codex_bridge.workflows.interpreter import OwsError, validate_workflow
from tests.test_workflow_d1_i7a_variable_call_validation import _contract


def _reserved_agent_run(
    tmp_path: Any,
    tasks: list[dict[str, Any]],
    *,
    port: int = 18001,
) -> tuple[Any, Any, Any, Any]:
    from codex_bridge.workflows.agent_contracts import TargetContext
    from codex_bridge.workflows.agent_ledger import AgentInvocationLedger
    from codex_bridge.workflows.contracts import DRAFT_2020_12
    from codex_bridge.workflows.strict_binding import StrictWorkflowBindingVerifier
    from codex_bridge.workflows.strict_definitions import StrictWorkflowDefinitionStore
    from codex_bridge.workflows.strict_manifest import StrictWorkflowSnapshotWriter
    from codex_bridge.workflows.strict_reader_gate import _reserve_prepared_strict_run
    from codex_bridge.workflows.strict_requests import StrictWorkflowRequestStore
    from codex_bridge.workflows.strict_variable_runtime import (
        StrictWorkflowVariableRuntimeAdapter,
        checkpoint_identity_from_manifest,
    )
    from codex_bridge.workflows.strict_variables import (
        StrictWorkflowRunBindingInput,
        StrictWorkflowVariableStore,
    )

    state_root = tmp_path / "state"
    workspace = tmp_path / "workspace"
    state_root.mkdir()
    workspace.mkdir()
    allowed_roots = (str(workspace.resolve()),)
    StrictWorkflowRequestStore(state_root).initialize_new_database()
    definitions = StrictWorkflowDefinitionStore(state_root)
    definitions.initialize_new_database()
    definition = _definition(tasks)
    definition["output"] = {"as": "${ {} }"}
    source = yaml.safe_dump(definition, sort_keys=False, allow_unicode=True)
    contract = {
        "contract_version": 1,
        "input_schema": {
            "$schema": DRAFT_2020_12,
            "type": "object",
            "additionalProperties": False,
        },
        "output_schema": {
            "$schema": DRAFT_2020_12,
            "type": "object",
            "additionalProperties": False,
        },
        "variables": [
            {
                "declaration_id": "count",
                "name": "count",
                "owner_scope": "workflow",
                "value_schema": {
                    "$schema": DRAFT_2020_12,
                    "type": "integer",
                    "additionalProperties": False,
                },
                "sensitivity": "public",
            }
        ],
    }
    validate_workflow(
        definition,
        strict_contract=compile_contract(contract),
        max_live_codex_jobs=1,
    )
    revision = definitions.create(
        mutation_request_id="i4b-definition-create-0001",
        source_yaml=source,
        max_live_codex_jobs=1,
        contract=contract,
    )
    normalized = StrictWorkflowBindingVerifier(state_root).normalize_for_definition(
        revision.definition_id,
        revision.revision,
        {},
    )
    run_id = f"run-{uuid.uuid4().hex}"
    workflow_id = str(uuid.uuid4())
    created_at = datetime.now(UTC)
    run_dir = state_root / run_id
    run_dir.mkdir()
    variable_binding = StrictWorkflowRunBindingInput(
        run_id=run_id,
        workflow_id=workflow_id,
        definition_id=revision.definition_id,
        definition_revision=revision.revision,
        contract_sha256=revision.contract_sha256,
        binding_digest=revision.binding_digest,
        normalized_input_sha256=normalized.normalized_input_sha256,
        created_at=created_at,
    )
    StrictWorkflowVariableStore(run_dir).initialize_new_database(variable_binding)
    manifest = StrictWorkflowSnapshotWriter(state_root, allowed_roots).prepare_snapshot(
        workflow_id=workflow_id,
        run_id=run_id,
        definition_id=revision.definition_id,
        definition_revision=revision.revision,
        workspace_root=str(workspace.resolve()),
        normalized_input=normalized,
        created_at=created_at,
    )
    context = TargetContext(
        "codex",
        "default",
        1,
        endpoint=f"http://127.0.0.1:{port}",
        workspace=str(workspace.resolve()),
    )
    AgentInvocationLedger.initialize_new_database(
        run_dir,
        checkpoint_identity_from_manifest(manifest),
        context,
    )
    reserved = _reserve_prepared_strict_run(
        state_root,
        run_id,
        client_request_id="i4b-agent-request-0001",
        allowed_roots=allowed_roots,
    )
    adapter = StrictWorkflowVariableRuntimeAdapter.from_reserved_run(
        state_root,
        run_id,
        expected_workflow_id=workflow_id,
        expected_request_digest=reserved.reservation.request_digest,
        allowed_roots=allowed_roots,
    )
    return adapter, run_dir, allowed_roots, context


def _agent_wire(**overrides: Any) -> dict[str, Any]:
    wire: dict[str, Any] = {
        "provider": "codex",
        "target": "default",
        "session": {"mode": "new"},
        "prompt": "Summarize {{source}}",
        "bindings": {"source": {"from": "${ .source }", "format": "text"}},
        "response": {
            "format": "json",
            "schema": {
                "type": "object",
                "properties": {"call": {"type": "string"}},
                "required": ["call"],
                "additionalProperties": False,
            },
        },
        "wait": False,
    }
    wire.update(overrides)
    return wire


def _definition(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "document": {
            "dsl": "1.0.3",
            "namespace": "codexbridge",
            "name": "d2-i4b-runtime",
            "version": "1.0.0",
        },
        "evaluate": {"language": "jq", "mode": "strict"},
        "do": tasks,
    }


def _agent_task(name: str, wire: dict[str, Any] | None = None) -> dict[str, Any]:
    return {name: {"call": "codexbridge-agent", "with": wire or _agent_wire()}}


def test_task_tree_classifier_only_reads_actual_task_call_targets() -> None:
    from codex_bridge.workflows.task_tree import StrictTaskTreeKind, classify_task_tree

    pure = _definition(
        [
            {
                "literal": {
                    "set": {
                        "prompt": "call: codexbridge-agent",
                        "schema": {"call": "codexbridge-agent"},
                        "data": {"call": "codex-job"},
                    }
                }
            }
        ]
    )
    classified = classify_task_tree(pure, strict_contract=True)
    assert classified.kind is StrictTaskTreeKind.STRICT_PURE
    assert classified.agent_calls == ()
    assert classified.codex_job_calls == ()

    agent = _definition([_agent_task("invoke")])
    classified = classify_task_tree(agent, strict_contract=True)
    assert classified.kind is StrictTaskTreeKind.STRICT_AGENT
    assert len(classified.agent_calls) == 1


def test_strict_executor_modes_match_the_classified_pinned_task_tree() -> None:
    from codex_bridge.workflows.strict_executor import (
        StrictWorkflowExecutionError,
        _strict_task_map,
    )

    pure = _definition(
        [{"literal": {"set": {"prompt": "call: codex-job", "data": {"call": "codex-job"}}}}]
    )
    assert "/do/0/literal" in _strict_task_map(pure, execution_mode="isolated")

    agent = _definition([_agent_task("invoke")])
    assert _strict_task_map(agent, execution_mode="authorized_agent")["/do/0/invoke"][1] == "call"
    with pytest.raises(StrictWorkflowExecutionError):
        _strict_task_map(agent, execution_mode="isolated")
    with pytest.raises(StrictWorkflowExecutionError):
        _strict_task_map(agent, execution_mode="authorized_codex")

    codex = _definition([{"legacy": {"call": "codex-job", "with": {"prompt": "hello"}}}])
    assert _strict_task_map(codex, execution_mode="authorized_codex")["/do/0/legacy"][1] == "call"
    with pytest.raises(StrictWorkflowExecutionError):
        _strict_task_map(codex, execution_mode="authorized_agent")


def test_strict_validation_accepts_four_static_agent_nodes_and_non_loop_control() -> None:
    contract = compile_contract(_contract())
    definition = _definition(
        [
            _agent_task("first"),
            {
                "scope": {
                    "do": [
                        _agent_task("second", _agent_wire(wait=True)),
                        {
                            "choose": {
                                "switch": [
                                    {"selected": {"when": "${ .selected }", "then": "third"}},
                                    {"fallback": {"then": "fourth"}},
                                ]
                            }
                        },
                        _agent_task("third"),
                        _agent_task("fourth"),
                    ]
                }
            },
        ]
    )

    validate_workflow(definition, strict_contract=contract, max_live_codex_jobs=1)


@pytest.mark.parametrize("profile", [2, 3])
def test_strict_agent_rejects_legacy_job_profiles(profile: int) -> None:
    definition = _definition([_agent_task("invoke")])
    with pytest.raises(OwsError):
        validate_workflow(
            definition,
            codex_job_live=True,
            max_live_codex_jobs=profile,
            strict_contract=compile_contract(_contract()),
        )


def test_strict_agent_requires_contract_and_rejects_mixed_jobs_and_for_loops() -> None:
    agent = _definition([_agent_task("invoke")])
    with pytest.raises(OwsError):
        validate_workflow(agent)

    with pytest.raises(OwsError):
        validate_workflow(
            _definition(
                [
                    _agent_task("invoke"),
                    {"legacy": {"call": "codex-job", "with": {"prompt": "hello"}}},
                ]
            ),
            codex_job_live=True,
            strict_contract=compile_contract(_contract()),
        )

    with pytest.raises(OwsError):
        validate_workflow(
            _definition(
                [
                    {
                        "iterate": {
                            "for": {"in": "${ .items }", "each": "item", "at": "index"},
                            "do": [_agent_task("invoke")],
                        }
                    }
                ]
            ),
            strict_contract=compile_contract(_contract()),
        )


def test_dynamic_agent_preparation_failure_returns_safe_result_without_ledger_row(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codex_bridge.workflows import strict_executor
    from codex_bridge.workflows.agent_contracts import codex_agent_i4b_registry
    from codex_bridge.workflows.agent_ledger import AgentInvocationLedger

    wire = _agent_wire(bindings={"source": {"from": "${ .missing }", "format": "text"}})
    adapter, run_dir, allowed_roots, context = _reserved_agent_run(
        tmp_path,
        [_agent_task("invoke", wire)],
    )
    registry = codex_agent_i4b_registry()
    provider = registry._providers["codex"]
    monkeypatch.setattr(
        strict_executor,
        "build_codex_agent_runtime",
        lambda **_kwargs: (provider, registry, context),
    )
    task_spec = adapter.pinned_definition["do"][0]["invoke"]
    checkpoint = strict_executor._prepare_agent_request_checkpoint_body(
        str(run_dir.parent),
        run_dir.name,
        list(allowed_roots),
        adapter.checkpoint_identity,
        int(context.endpoint.rsplit(":", 1)[1]),
        "/do/0/invoke",
        [],
        "invoke",
        task_spec,
        {},
        {"context": {}, "input": {}},
    )
    ledger = AgentInvocationLedger.open_existing(
        run_dir,
        adapter.checkpoint_identity,
        context,
    )

    assert checkpoint["error_code"] == "invalid_request"
    assert checkpoint["request_json"] is None
    assert checkpoint["invocation_id"] == ledger.invocation_id_for("/do/0/invoke", [])
    assert ledger.list_invocations(limit=10) == ()


@pytest.mark.parametrize(
    "wire",
    [
        {
            "operation": "interrupt",
            "provider": "codex",
            "handle": {"thread_id": "thread_12345678", "turn_id": "turn_12345678"},
        },
        {
            "provider": "codex",
            "session": {"mode": "continue", "thread_id": "thread_12345678"},
            "prompt": "Continue the thread.",
        },
        _agent_wire(options={"model": "example-model"}),
    ],
)
def test_static_agent_validation_accepts_i5_operations_and_options(wire: dict[str, Any]) -> None:
    validate_workflow(
        _definition([_agent_task("invoke", wire)]),
        strict_contract=compile_contract(_contract()),
    )


def test_strict_agent_flag_defaults_off_and_is_independent_of_codex_jobs() -> None:
    default = BridgeConfig.from_sources(environ={}, config_data={})
    assert default.workflow_strict_agent_enabled is False

    agent_enabled = BridgeConfig.from_sources(
        environ={},
        config_data={
            "workflow": {
                "strict_runtime_enabled": True,
                "strict_codex_jobs_enabled": False,
                "strict_agent_enabled": True,
            }
        },
    )
    assert agent_enabled.workflow_strict_agent_enabled is True
    assert agent_enabled.workflow_strict_codex_jobs_enabled is False

    codex_enabled = BridgeConfig.from_sources(
        environ={},
        config_data={
            "workflow": {
                "strict_runtime_enabled": True,
                "strict_codex_jobs_enabled": True,
                "strict_agent_enabled": False,
            }
        },
    )
    assert codex_enabled.workflow_strict_agent_enabled is False
    assert codex_enabled.workflow_strict_codex_jobs_enabled is True

    with pytest.raises(ConfigurationError, match="requires workflow.strict_runtime_enabled"):
        BridgeConfig.from_sources(
            environ={}, config_data={"workflow": {"strict_agent_enabled": True}}
        )


@pytest.mark.parametrize("mode", ["start", "recover"])
def test_worker_entry_flag_off_refuses_agent_start_and_recovery(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    mode: str,
) -> None:
    from codex_bridge.workflows import worker_entry

    adapter, run_dir, allowed_roots, context = _reserved_agent_run(
        tmp_path,
        [_agent_task("invoke")],
    )
    config_path = run_dir / "worker-config.json"
    config_path.write_text(
        json.dumps(
            {
                "version": 2,
                "mode": mode,
                "workspace_root": context.workspace,
                "allowed_roots": list(allowed_roots),
                "shutdown_file": None,
                "strict_runtime": True,
                "execution_mode": "authorized_agent",
                "codex_bridge_port": int(context.endpoint.rsplit(":", 1)[1]),
                "strict_agent_enabled": True,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("CODEXBRIDGE_WORKFLOW_STRICT_AGENT_ENABLED", raising=False)

    exit_code = worker_entry.main(
        ["--mode", mode, "--run-dir", str(run_dir), "--config", str(config_path)]
    )

    assert exit_code == 2
    assert (run_dir / "worker-entry-status.json").is_file()
    assert not (run_dir / "dbos.sqlite").exists()
    assert adapter.checkpoint_identity["workflow_id"]
    assert "strict Agent runtime is not enabled" in capfd.readouterr().err


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["start", "recover"])
async def test_manager_agent_config_passes_worker_entry_and_reaches_worker_main(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    from pathlib import Path

    from codex_bridge.workflows import manager as manager_module
    from codex_bridge.workflows import worker, worker_entry
    from codex_bridge.workflows.manager import WorkflowWorkerManager

    _adapter, run_dir, allowed_roots, context = _reserved_agent_run(
        tmp_path,
        [_agent_task("invoke")],
    )
    manager = WorkflowWorkerManager(
        state_root=run_dir.parent,
        allowed_roots=allowed_roots,
        control_token="test-control-token",
        enable_isolated_strict_runtime=True,
        enable_strict_agent=True,
    )
    captured: dict[str, Any] = {}

    class FakeProcess:
        pid = 43101

        async def wait(self) -> int:
            return 0

    async def fake_create_subprocess_exec(*args: Any, **kwargs: Any) -> FakeProcess:
        captured["args"] = args
        captured["env"] = kwargs["env"]
        return FakeProcess()

    monkeypatch.setattr(
        manager_module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )
    handle = await manager.start_worker(
        run_dir,
        mode=mode,
        workspace_root=Path(context.workspace),
        max_live_codex_jobs=1,
        strict_runtime=True,
        codex_bridge_port=int(context.endpoint.rsplit(":", 1)[1]),
    )
    config_path = run_dir / f"worker-config-{manager._records[run_dir.name].launch_id}.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["execution_mode"] == "authorized_agent"
    assert config["strict_agent_enabled"] is True

    worker_args: list[list[str]] = []
    monkeypatch.setattr(worker_entry, "missing_workflow_dependencies", lambda: ())
    monkeypatch.setattr(worker, "main", lambda args: worker_args.append(list(args)) or 0)
    for key in (
        worker.CONTROL_TOKEN_ENV,
        "CODEXBRIDGE_WORKFLOW_STRICT_AGENT_ENABLED",
        "CODEXBRIDGE_WORKFLOW_BRIDGE_PORT",
    ):
        monkeypatch.setenv(key, captured["env"][key])

    exit_code = worker_entry.main(
        ["--mode", mode, "--run-dir", str(run_dir), "--config", str(config_path)]
    )
    await handle.wait(timeout_seconds=2)
    await manager.shutdown()

    assert exit_code == 0
    assert len(worker_args) == 1
    assert "--strict-runtime-mode" in worker_args[0]
    assert worker_args[0][worker_args[0].index("--strict-runtime-mode") + 1] == "authorized_agent"
    assert "--codex-bridge-port" in worker_args[0]
    assert (
        json.loads((run_dir / "worker-entry-status.json").read_text(encoding="utf-8"))["state"]
        == "completed"
    )


def test_agent_worker_cli_cannot_relabel_the_pinned_task_tree(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from argparse import Namespace

    from codex_bridge.workflows import worker
    from codex_bridge.workflows.interpreter import OwsError

    _adapter, run_dir, allowed_roots, context = _reserved_agent_run(
        tmp_path,
        [_agent_task("invoke")],
    )
    monkeypatch.setenv("CODEXBRIDGE_WORKFLOW_STRICT_AGENT_ENABLED", "1")
    monkeypatch.setenv(worker.CONTROL_TOKEN_ENV, "test-token")

    with pytest.raises(OwsError):
        worker._strict_worker_adapter(
            Namespace(
                strict_runtime_mode="isolated",
                allowed_root=list(allowed_roots),
                workspace_root=context.workspace,
                codex_bridge_port=None,
            ),
            run_dir,
        )


def test_agent_ledger_can_read_its_pinned_context_without_current_port(tmp_path: Any) -> None:
    from codex_bridge.workflows.agent_contracts import TargetContext
    from codex_bridge.workflows.agent_ledger import AgentInvocationLedger

    binding = {
        "run_id": "run-1234567890abcdef1234567890abcdef",
        "workflow_id": "12345678-1234-4234-8234-1234567890ab",
        "definition_id": "12345678-1234-4234-8234-1234567890ac",
        "definition_revision": 1,
        "request_digest": "1" * 64,
        "source_sha256": "2" * 64,
        "contract_sha256": "3" * 64,
        "binding_digest": "4" * 64,
        "normalized_input_sha256": "5" * 64,
    }
    context = TargetContext(
        "codex",
        "default",
        1,
        endpoint="http://127.0.0.1:18222",
        workspace="D:\\workspace",
    )
    run_directory = tmp_path / "run-dir"
    run_directory.mkdir()

    ledger = AgentInvocationLedger.initialize_new_database(run_directory, binding, context)
    stored = AgentInvocationLedger.inspect_stored_binding(run_directory)

    assert ledger.read_binding()["target_context"] == stored.target_context
    assert stored.binding_identity == binding
    assert stored.target_context == context.to_dict()


def test_reserved_agent_ledger_missing_does_not_get_recreated(tmp_path: Any) -> None:
    from codex_bridge.workflows.agent_ledger import AgentLedgerIntegrityError
    from codex_bridge.workflows.agent_runtime import open_agent_ledger_for_manifest

    adapter, run_dir, _allowed_roots, context = _reserved_agent_run(
        tmp_path,
        [_agent_task("invoke")],
    )
    manifest = adapter._verified_manifest
    assert manifest is not None
    ledger_path = run_dir / "agent_invocations.sqlite"
    moved_path = run_dir / "agent_invocations.sqlite.moved"
    ledger_path.replace(moved_path)
    try:
        with pytest.raises(AgentLedgerIntegrityError):
            open_agent_ledger_for_manifest(
                run_dir,
                manifest,
                expected_port=int(context.endpoint.rsplit(":", 1)[1]),
            )
        assert not ledger_path.exists()
    finally:
        moved_path.replace(ledger_path)


def test_authorized_agent_outer_wait_can_exceed_120_seconds_without_sleep(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codex_bridge.workflows import worker

    clock = [0.0]

    class CompletedHandle:
        def get_result(self) -> dict[str, str]:
            return {"status": "succeeded"}

    def read_completed_status(*_args: Any) -> str:
        clock[0] = 121.0
        return "SUCCESS"

    monkeypatch.setattr(worker, "_read_workflow_status", read_completed_status)
    monkeypatch.setattr(worker.DBOS, "retrieve_workflow", lambda _workflow_id: CompletedHandle())
    result, reason = worker._wait_for_strict_result(
        tmp_path / "dbos.sqlite",
        "workflow-id",
        None,
        None,
    )

    assert clock[0] > 120
    assert result == {"status": "succeeded"}
    assert reason is None


def test_legacy_outer_wait_timeout_keeps_its_existing_deadline(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codex_bridge.workflows import worker

    values = iter((0.0, 0.0, 121.0))
    monkeypatch.setattr(worker.time, "monotonic", lambda: next(values))
    monkeypatch.setattr(worker, "_read_workflow_status", lambda *_args: "PENDING")
    monkeypatch.setattr(worker.time, "sleep", lambda _seconds: None)

    with pytest.raises(TimeoutError):
        worker._wait_for_strict_result(
            tmp_path / "dbos.sqlite",
            "workflow-id",
            120.0,
            None,
        )


def test_shutdown_agent_ownership_pages_past_100_and_ignores_foreign_handles(
    tmp_path: Any,
) -> None:
    import sqlite3

    from codex_bridge.workflows.agent_contracts import (
        AgentRequest,
        DispatchReceipt,
        PreparedProviderRequest,
    )
    from codex_bridge.workflows.agent_ledger import AgentInvocationLedger
    from codex_bridge.workflows.manager import _persisted_reserved_strict_agent_turns

    tasks = [_agent_task(f"invoke_{index:03d}") for index in range(100)]
    tasks.append(
        {
            "foreign_observe": {
                "call": "codexbridge-agent",
                "with": {
                    "operation": "observe",
                    "provider": "codex",
                    "target": "default",
                    "handle": "${ .foreign_handle }",
                },
            }
        }
    )
    adapter, run_dir, allowed_roots, context = _reserved_agent_run(tmp_path, tasks)
    ledger = AgentInvocationLedger.open_existing(
        run_dir,
        adapter.checkpoint_identity,
        context,
    )
    invoke_request = AgentRequest(
        operation="invoke",
        provider="codex",
        target="default",
        target_context=context,
        session={"mode": "new"},
        handle=None,
        options={},
        prompt="shutdown ownership fixture",
        response={"format": "text"},
        wait=False,
        timeout_seconds=None,
    )
    invoke_prepared = PreparedProviderRequest(invoke_request, context)
    records = [
        ledger.prepare_invoke(
            f"/do/{index}/invoke_{index:03d}",
            [],
            invoke_prepared,
            created_ms=2000 + index,
        )
        for index in range(100)
    ]
    foreign_request = AgentRequest(
        operation="observe",
        provider="codex",
        target="default",
        target_context=context,
        session=None,
        handle={"thread_id": "thread_foreign_1", "turn_id": "turn_foreign_1"},
        options=None,
        prompt=None,
        response={"format": "text"},
        wait=None,
        timeout_seconds=None,
    )
    ledger.prepare_invoke(
        "/do/100/foreign_observe",
        [],
        PreparedProviderRequest(foreign_request, context),
        created_ms=2100,
    )
    owned = records[-1]
    assert ledger.claim_dispatch(owned.invocation_id, owned.request_sha256)
    ledger.save_dispatch_receipt(
        owned.invocation_id,
        DispatchReceipt.accepted(
            handle={"thread_id": "thread_owned_123", "turn_id": "turn_owned_456"},
            remote_state="in_progress",
        ),
    )

    connection = sqlite3.connect(run_dir.parent / "requests.sqlite")
    try:
        connection.execute(
            "UPDATE workflow_requests SET state='succeeded' WHERE run_id=?",
            (run_dir.name,),
        )
        actual_run_number = int(run_dir.name.removeprefix("run-"), 16)
        assert actual_run_number > 100
        for index in range(101):
            workflow_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO workflow_requests "
                "(client_request_id,workflow_id,run_id,request_digest,state,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    f"legacy-{index}",
                    workflow_id,
                    f"run-{index:032x}",
                    "a" * 64,
                    "succeeded",
                    datetime.now(UTC).isoformat(),
                    datetime.now(UTC).isoformat(),
                ),
            )
        connection.commit()
    finally:
        connection.close()

    turns, complete = _persisted_reserved_strict_agent_turns(
        run_dir.parent,
        allowed_roots=tuple(allowed_roots),
    )
    assert complete is True
    assert turns == {("thread_owned_123", "turn_owned_456")}

    maybe_sent = records[-2]
    assert ledger.claim_dispatch(maybe_sent.invocation_id, maybe_sent.request_sha256)
    turns, complete = _persisted_reserved_strict_agent_turns(
        run_dir.parent,
        allowed_roots=tuple(allowed_roots),
    )
    assert complete is False
    assert turns == {("thread_owned_123", "turn_owned_456")}


def test_shutdown_agent_ownership_reports_a_missing_v2_reservation_binding(
    tmp_path: Any,
) -> None:
    import sqlite3

    from codex_bridge.workflows.manager import _persisted_reserved_strict_agent_turns

    _adapter, run_dir, allowed_roots, _context = _reserved_agent_run(
        tmp_path,
        [_agent_task("invoke")],
    )
    connection = sqlite3.connect(run_dir.parent / "requests.sqlite")
    try:
        connection.execute(
            "DELETE FROM workflow_request_contract_bindings WHERE run_id=?",
            (run_dir.name,),
        )
        connection.commit()
    finally:
        connection.close()

    turns, complete = _persisted_reserved_strict_agent_turns(
        run_dir.parent,
        allowed_roots=tuple(allowed_roots),
    )

    assert turns == set()
    assert complete is False


@pytest.mark.asyncio
async def test_agent_invocation_api_is_read_only_paged_and_metadata_only(tmp_path: Any) -> None:
    import httpx2 as httpx
    from starlette.applications import Starlette

    from codex_bridge.workflows.agent_contracts import AgentRequest, PreparedProviderRequest
    from codex_bridge.workflows.agent_ledger import AgentInvocationLedger
    from codex_bridge.workflows.api import create_workflow_routes
    from codex_bridge.workflows.service import WorkflowRequestError, _agent_invocation_json

    adapter, run_dir, allowed_roots, context = _reserved_agent_run(
        tmp_path,
        [_agent_task("first"), _agent_task("second")],
    )
    ledger = AgentInvocationLedger.open_existing(
        run_dir,
        adapter.checkpoint_identity,
        context,
    )
    request = AgentRequest(
        operation="invoke",
        provider="codex",
        target="default",
        target_context=context,
        session={"mode": "new"},
        handle=None,
        options={},
        prompt="PRIVATE_AGENT_PROMPT",
        response={"format": "text"},
        wait=False,
        timeout_seconds=None,
    )
    ledger.prepare_invoke(
        "/do/0/first",
        [],
        PreparedProviderRequest(request, context),
        created_ms=1000,
    )
    ledger.prepare_invoke(
        "/do/1/second",
        [],
        PreparedProviderRequest(request, context),
        created_ms=1000,
    )

    records = ledger.list_invocations(limit=100)

    class FakeService:
        async def list_agent_invocations(
            self,
            workflow_id: str,
            *,
            limit: int,
            after: tuple[int, str] | None,
        ) -> dict[str, Any]:
            assert workflow_id == adapter.checkpoint_identity["workflow_id"]
            page = ledger.list_invocations(limit=limit, after=after)
            return {
                "invocations": [_agent_invocation_json(record) for record in page],
                "limit": limit,
                "next_after": (
                    (page[-1].created_ms, page[-1].invocation_id) if len(page) == limit else None
                ),
            }

        async def get_agent_invocation(
            self, workflow_id: str, invocation_id: str
        ) -> dict[str, Any]:
            if workflow_id != adapter.checkpoint_identity["workflow_id"]:
                raise WorkflowRequestError("workflow ID is invalid")
            record = next(item for item in records if item.invocation_id == invocation_id)
            return _agent_invocation_json(record, include_digest=True)

    routes = create_workflow_routes(
        FakeService(),
        token="i4b-api-token",
        ui_port=18001,
        authorize=lambda request, token: request.headers.get("authorization") == f"Bearer {token}",
        ready=lambda: True,
    )
    app = Starlette(routes=routes)
    headers = {"authorization": "Bearer i4b-api-token"}
    path = (
        f"/ui-api/control/workflows/{adapter.checkpoint_identity['workflow_id']}/agent-invocations"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1:18001",
    ) as client:
        first_page = await client.get(f"{path}?limit=1", headers=headers)
        assert first_page.status_code == 200, first_page.text
        first_body = first_page.json()
        assert first_body["limit"] == 1
        assert len(first_body["invocations"]) == 1
        first_id = first_body["invocations"][0]["invocation_id"]
        assert first_id in {record.invocation_id for record in records}
        assert first_body["invocations"][0]["status"] == "PREPARED"
        assert "PRIVATE_AGENT_PROMPT" not in first_page.text
        cursor = first_body["next_cursor"]
        assert isinstance(cursor, str)

        second_page = await client.get(f"{path}?limit=1&cursor={cursor}", headers=headers)
        assert second_page.status_code == 200
        assert len(second_page.json()["invocations"]) == 1
        assert second_page.json()["invocations"][0]["invocation_id"] != first_id
        detail = await client.get(f"{path}/{first_id}", headers=headers)
        assert detail.status_code == 200
        assert "result_digest" in detail.json()
        assert "PRIVATE_AGENT_PROMPT" not in detail.text

        invalid_cursor = await client.get(f"{path}?cursor=not-a-cursor", headers=headers)
        assert invalid_cursor.status_code == 400
        invalid_invocation = await client.get(f"{path}/invalid", headers=headers)
        assert invalid_invocation.status_code == 400
        assert invalid_invocation.json()["error"] == "invalid_invocation_id"
        invalid_workflow = await client.get(
            f"/ui-api/control/workflows/not-a-workflow/agent-invocations/{first_id}",
            headers=headers,
        )
        assert invalid_workflow.status_code == 400
        assert invalid_workflow.json()["error"] == "invalid_workflow_id"
        unauthorized = await client.get(path)
        assert unauthorized.status_code == 403

    assert (run_dir / "agent_invocations.sqlite").exists()


def test_service_reader_matches_prepared_target_identity_to_pinned_ledger(
    tmp_path: Any,
) -> None:
    from codex_bridge.workflows.agent_contracts import AgentRequest, PreparedProviderRequest
    from codex_bridge.workflows.agent_ledger import AgentInvocationLedger
    from codex_bridge.workflows.service import _read_verified_agent_invocations

    adapter, run_dir, _allowed_roots, context = _reserved_agent_run(
        tmp_path,
        [_agent_task("invoke")],
    )
    ledger = AgentInvocationLedger.open_existing(
        run_dir,
        adapter.checkpoint_identity,
        context,
    )
    request = AgentRequest(
        operation="invoke",
        provider="codex",
        target="default",
        target_context=context,
        session={"mode": "new"},
        handle=None,
        options={},
        prompt="reader target fixture",
        response={"format": "text"},
        wait=False,
        timeout_seconds=None,
    )
    created = ledger.prepare_invoke(
        "/do/0/invoke",
        [],
        PreparedProviderRequest(request, context),
        created_ms=1000,
    )
    manifest = adapter._verified_manifest
    definition = adapter.pinned_definition
    assert manifest is not None and definition is not None

    records = _read_verified_agent_invocations(run_dir, manifest, definition)

    assert len(records) == 1
    assert records[0].invocation_id == created.invocation_id
    assert records[0].request.target_identity == context.to_dict()


def test_actual_dbos_strict_agent_runtime_handles_wait_observe_and_skipped_branch(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dbos import DBOS, SetWorkflowID

    from codex_bridge.workflows import strict_executor, worker
    from codex_bridge.workflows.agent_contracts import (
        DispatchReceipt,
        Observation,
        PreparedProviderRequest,
        ProviderCapabilities,
        ProviderRegistry,
    )
    from codex_bridge.workflows.agent_ledger import AgentInvocationLedger

    handle = {"thread_id": "thread_12345678", "turn_id": "turn_12345678"}
    active_tasks = [
        {
            "choose": {
                "switch": [
                    {"active": {"when": "${ true }", "then": "active_scope"}},
                    {"skipped": {"then": "skipped_agent"}},
                ]
            }
        },
        {
            "active_scope": {
                "do": [
                    {
                        "set_count": {
                            "call": "codexbridge-variable",
                            "with": {
                                "operation": "initialize",
                                "declaration_id": "count",
                                "scope": "workflow",
                                "value": 7,
                            },
                        }
                    },
                    {
                        "invoke": {
                            "call": "codexbridge-agent",
                            "with": _agent_wire(
                                bindings={
                                    "source": {
                                        "from": "${ $cbvars.workflow.count }",
                                        "format": "json",
                                    }
                                },
                                wait=False,
                            ),
                        }
                    },
                    {
                        "observe": {
                            "call": "codexbridge-agent",
                            "with": {
                                "operation": "observe",
                                "provider": "codex",
                                "target": "default",
                                "handle": "${ .handle }",
                            },
                        }
                    },
                    {
                        "wait": {
                            "call": "codexbridge-agent",
                            "with": {
                                "operation": "wait",
                                "provider": "codex",
                                "target": "default",
                                "handle": "${ .handle }",
                                "timeout_seconds": 30,
                            },
                        }
                    },
                ],
                "then": "exit",
            }
        },
        {
            "skipped_agent": {
                "call": "codexbridge-agent",
                "with": _agent_wire(),
                "then": "exit",
            }
        },
    ]
    adapter, run_dir, allowed_roots, context = _reserved_agent_run(tmp_path, active_tasks)
    assert adapter.pinned_definition is not None

    class FakeProvider:
        def __init__(self) -> None:
            self.observations = [
                Observation("RUNNING", handle=handle, remote_state="in_progress"),
                Observation(
                    "COMPLETED",
                    handle=handle,
                    remote_state="completed",
                    final_text='{"call":"ok"}',
                ),
            ]
            self.dispatches = 0
            self.seen_prompts: list[str] = []

        def capabilities(self, target: str) -> ProviderCapabilities:
            assert target == "default"
            return ProviderCapabilities(
                provider="codex",
                target="default",
                operations=("invoke", "observe", "wait"),
                session_modes=("new",),
                endpoint=context.endpoint,
                workspace=context.workspace,
            )

        def prepare(self, request: Any, target_context: Any) -> PreparedProviderRequest:
            assert target_context == context
            if request.prompt is not None:
                self.seen_prompts.append(request.prompt)
            return PreparedProviderRequest(request, target_context)

        def preflight(self, prepared: Any) -> None:
            assert prepared.operation == "invoke"

        def dispatch(self, prepared: Any, client_request_id: str) -> DispatchReceipt:
            self.dispatches += 1
            assert client_request_id.startswith("cba-")
            return DispatchReceipt.accepted(handle=handle, remote_state="in_progress")

        def observe(self, observed_handle: object, target_context: Any) -> Observation:
            assert observed_handle == handle
            assert target_context == context
            return self.observations.pop(0)

        def interrupt(self, *_args: Any) -> DispatchReceipt:
            raise AssertionError("I4-B has no interrupt operation")

    provider = FakeProvider()
    registry = ProviderRegistry({"codex": provider}, {"codex": "default"})
    monkeypatch.setattr(
        strict_executor,
        "build_codex_agent_runtime",
        lambda **_kwargs: (provider, registry, context),
    )
    interpreter_type = strict_executor.Interpreter
    observed_events: list[tuple[str, str, str | None]] = []
    preparation_results: list[tuple[str | None, bool, Any]] = []
    prepare_agent_request = strict_executor._prepare_agent_request_checkpoint_body

    def capture_agent_preparation(*args: Any, **kwargs: Any) -> dict[str, Any]:
        value = prepare_agent_request(*args, **kwargs)
        variables = args[10] if len(args) > 10 and type(args[10]) is dict else {}
        cbvars = variables.get("cbvars")
        preparation_results.append(
            (value.get("error_code"), isinstance(value.get("request_json"), str), cbvars)
        )
        return value

    monkeypatch.setattr(
        strict_executor, "_prepare_agent_request_checkpoint_body", capture_agent_preparation
    )

    def instrumented_interpreter(definition: Any, **kwargs: Any) -> Any:
        observer = kwargs.pop("observer", None)

        def capture_event(event: Any) -> None:
            observed_events.append(
                (event.event_type, event.task_identity or "", event.branch_class)
            )
            if observer is not None:
                observer(event)

        return interpreter_type(definition, observer=capture_event, **kwargs)

    monkeypatch.setattr(strict_executor, "Interpreter", instrumented_interpreter)
    monkeypatch.setenv("CODEXBRIDGE_WORKFLOW_STRICT_AGENT_ENABLED", "1")
    monkeypatch.setenv("CODEXBRIDGE_WORKFLOW_BRIDGE_PORT", context.endpoint.rsplit(":", 1)[1])
    DBOS(config=worker._dbos_config(run_dir / "dbos.sqlite"))
    DBOS.launch()
    try:
        with SetWorkflowID(str(adapter.checkpoint_identity["workflow_id"])):
            workflow_handle = DBOS.start_workflow(
                strict_executor.execute_strict_ows_workflow,
                run_dir.name,
                str(run_dir.parent),
                list(allowed_roots),
                "authorized_agent",
            )
        result = workflow_handle.get_result()
    finally:
        DBOS.destroy()

    assert result["status"] == "succeeded"
    assert result["result"] == {}
    records = AgentInvocationLedger.open_existing(
        run_dir,
        adapter.checkpoint_identity,
        context,
    ).list_invocations(limit=100)
    assert provider.dispatches == 1
    assert provider.seen_prompts == ["Summarize 7", "Summarize 7"]
    assert len(preparation_results) == 3
    assert all(error_code is None and valid for error_code, valid, _ in preparation_results)
    assert preparation_results[0][2] == {"workflow": {"count": 7}}
    assert any(event_type == "branch_selected" for event_type, _, _ in observed_events)
    assert any("active_scope.do[3].wait" in path for _, path, _ in observed_events)
    assert not any("skipped_agent" in path for _, path, _ in observed_events)
    assert [record.operation for record in records] == ["invoke", "observe", "wait"]
    assert len(records) == 3
    assert all(record.result_json is not None for record in records)
    assert all("skipped_agent" not in record.node_pointer for record in records)


def test_actual_dbos_strict_agent_new_continue_interrupt_then_observe_and_wait(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dbos import DBOS, SetWorkflowID

    from codex_bridge.workflows import strict_executor, worker
    from codex_bridge.workflows.agent_contracts import (
        AgentError,
        DispatchReceipt,
        Observation,
        PreparedProviderRequest,
        ProviderCapabilities,
        ProviderRegistry,
    )
    from codex_bridge.workflows.agent_ledger import AgentInvocationLedger

    new_handle = {"thread_id": "thread_12345678", "turn_id": "turn_12345678"}
    continued_handle = {"thread_id": "thread_12345678", "turn_id": "turn_87654321"}

    def invoke_wire(session: dict[str, Any]) -> dict[str, Any]:
        return {
            "operation": "invoke",
            "provider": "codex",
            "target": "default",
            "session": session,
            "prompt": "Continue the conversation.",
            "bindings": {},
            "options": {},
            "response": {"format": "text"},
            "wait": False,
        }

    tasks = [
        _agent_task("new", invoke_wire({"mode": "new"})),
        _agent_task(
            "continue",
            invoke_wire({"mode": "continue", "thread_id": "${ .handle.thread_id }"}),
        ),
        _agent_task(
            "interrupt",
            {
                "operation": "interrupt",
                "provider": "codex",
                "target": "default",
                "handle": "${ .handle }",
            },
        ),
        _agent_task(
            "observe",
            {
                "operation": "observe",
                "provider": "codex",
                "target": "default",
                "handle": "${ .handle }",
            },
        ),
        _agent_task(
            "wait",
            {
                "operation": "wait",
                "provider": "codex",
                "target": "default",
                "handle": "${ .handle }",
                "timeout_seconds": 30,
            },
        ),
    ]
    adapter, run_dir, allowed_roots, context = _reserved_agent_run(tmp_path, tasks)

    class FakeProvider:
        def __init__(self) -> None:
            self.dispatched_operations: list[str] = []
            self.observed_handles: list[dict[str, str]] = []

        def capabilities(self, target: str) -> ProviderCapabilities:
            return ProviderCapabilities(
                provider="codex",
                target=target,
                operations=("invoke", "observe", "wait", "interrupt"),
                session_modes=("new", "continue"),
                endpoint=context.endpoint,
                workspace=context.workspace,
            )

        def prepare(self, request: Any, target_context: Any) -> PreparedProviderRequest:
            return PreparedProviderRequest(request, target_context)

        def preflight(self, prepared: Any) -> None:
            assert prepared.operation in {"invoke", "interrupt"}

        def dispatch(self, prepared: Any, client_request_id: str) -> DispatchReceipt:
            assert client_request_id.startswith("cba-")
            assert prepared.operation == "invoke"
            self.dispatched_operations.append("invoke")
            if prepared.session["mode"] == "new":
                return DispatchReceipt.accepted(
                    handle=new_handle,
                    remote_state="in_progress",
                )
            assert prepared.session == {
                "mode": "continue",
                "thread_id": new_handle["thread_id"],
            }
            return DispatchReceipt.accepted(
                handle=continued_handle,
                remote_state="in_progress",
            )

        def interrupt(
            self, handle: object, target_context: Any, client_request_id: str
        ) -> DispatchReceipt:
            assert client_request_id.startswith("cba-")
            assert target_context == context
            assert handle == continued_handle
            self.dispatched_operations.append("interrupt")
            return DispatchReceipt.accepted(
                handle=continued_handle,
                remote_state="in_progress",
            )

        def observe(self, observed_handle: object, target_context: Any) -> Observation:
            assert target_context == context
            assert observed_handle == continued_handle
            self.observed_handles.append(dict(observed_handle))
            return Observation(
                "INTERRUPTED",
                handle=continued_handle,
                remote_state="interrupted",
                error=AgentError("remote_interrupted"),
            )

    provider = FakeProvider()
    registry = ProviderRegistry({"codex": provider}, {"codex": "default"})
    monkeypatch.setattr(
        strict_executor,
        "build_codex_agent_runtime",
        lambda **_kwargs: (provider, registry, context),
    )
    monkeypatch.setenv("CODEXBRIDGE_WORKFLOW_STRICT_AGENT_ENABLED", "1")
    monkeypatch.setenv("CODEXBRIDGE_WORKFLOW_BRIDGE_PORT", context.endpoint.rsplit(":", 1)[1])
    DBOS(config=worker._dbos_config(run_dir / "dbos.sqlite"))
    DBOS.launch()
    try:
        with SetWorkflowID(str(adapter.checkpoint_identity["workflow_id"])):
            workflow_handle = DBOS.start_workflow(
                strict_executor.execute_strict_ows_workflow,
                run_dir.name,
                str(run_dir.parent),
                list(allowed_roots),
                "authorized_agent",
            )
        result = workflow_handle.get_result()
    finally:
        DBOS.destroy()

    ledger = AgentInvocationLedger.open_existing(
        run_dir,
        adapter.checkpoint_identity,
        context,
    )
    records = ledger.list_invocations(limit=100)
    assert result["status"] == "succeeded"
    assert provider.dispatched_operations == ["invoke", "invoke", "interrupt"]
    assert provider.observed_handles == [continued_handle, continued_handle]
    assert [record.operation for record in records] == [
        "invoke",
        "invoke",
        "interrupt",
        "observe",
        "wait",
    ]
    assert all(record.result_json is not None for record in records)
    owned_rows = ledger.list_owned_handles()
    owned = {item.handle["turn_id"] for item in owned_rows}
    assert owned == {new_handle["turn_id"], continued_handle["turn_id"]}
    interrupt_record = next(record for record in records if record.operation == "interrupt")
    assert interrupt_record.invocation_id not in {item.invocation_id for item in owned_rows}
    assert interrupt_record.result_json is not None


@pytest.mark.parametrize("failure_kind", ["dynamic_interrupt", "unsupported_model"])
def test_actual_dbos_strict_agent_failure_result_can_select_workflow_branch(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    from dbos import DBOS, SetWorkflowID

    from codex_bridge.workflows import strict_executor, worker
    from codex_bridge.workflows.agent_contracts import (
        AgentContractError,
        DispatchReceipt,
        PreparedProviderRequest,
        ProviderCapabilities,
        ProviderRegistry,
        codex_agent_capabilities,
    )
    from codex_bridge.workflows.agent_ledger import AgentInvocationLedger

    if failure_kind == "dynamic_interrupt":
        failure_task = _agent_task(
            "interrupt",
            {
                "operation": "interrupt",
                "provider": "codex",
                "target": "default",
                "handle": "${ .missing_handle }",
            },
        )
    else:
        failure_task = _agent_task(
            "invoke",
            _agent_wire(
                prompt="Try the selected model.",
                bindings={},
                options={"model": "model-not-in-catalog"},
                wait=False,
            ),
        )
    tasks = [
        failure_task,
        {
            "route": {
                "switch": [
                    {
                        "handled_failure": {
                            "when": '${ .status == "FAILED" }',
                            "then": "handled_failure",
                        }
                    },
                    {"unexpected_result": {"then": "unexpected_result"}},
                ]
            }
        },
        {"handled_failure": {"set": {"handled": True}, "then": "exit"}},
        {"unexpected_result": {"set": {"handled": False}, "then": "exit"}},
    ]
    adapter, run_dir, allowed_roots, context = _reserved_agent_run(tmp_path, tasks)

    class FakeProvider:
        def __init__(self) -> None:
            self.prepare_count = 0
            self.preflight_count = 0
            self.dispatch_count = 0

        def capabilities(self, target: str) -> ProviderCapabilities:
            assert target == "default"
            return codex_agent_capabilities(context)

        def prepare(self, request: Any, target_context: Any) -> PreparedProviderRequest:
            self.prepare_count += 1
            return PreparedProviderRequest(request, target_context)

        def preflight(self, prepared: Any) -> None:
            self.preflight_count += 1
            if failure_kind == "unsupported_model":
                raise AgentContractError("unsupported_option")
            raise AssertionError("dynamic interrupt preparation must fail before preflight")

        def dispatch(self, prepared: Any, client_request_id: str) -> DispatchReceipt:
            self.dispatch_count += 1
            raise AssertionError("failed Agent preparation must not dispatch")

        def observe(self, *_args: Any) -> Any:
            raise AssertionError("failure branch must not observe")

        def interrupt(self, *_args: Any) -> DispatchReceipt:
            raise AssertionError("failure branch must not interrupt")

    provider = FakeProvider()
    registry = ProviderRegistry({"codex": provider}, {"codex": "default"})
    monkeypatch.setattr(
        strict_executor,
        "build_codex_agent_runtime",
        lambda **_kwargs: (provider, registry, context),
    )
    interpreter_type = strict_executor.Interpreter
    events: list[tuple[str, str, str | None, int | None]] = []

    def instrumented_interpreter(definition: Any, **kwargs: Any) -> Any:
        observer = kwargs.pop("observer", None)

        def capture_event(event: Any) -> None:
            events.append(
                (
                    event.event_type,
                    event.task_identity or "",
                    event.branch_class,
                    event.case_index,
                )
            )
            if observer is not None:
                observer(event)

        return interpreter_type(definition, observer=capture_event, **kwargs)

    monkeypatch.setattr(strict_executor, "Interpreter", instrumented_interpreter)
    monkeypatch.setenv("CODEXBRIDGE_WORKFLOW_STRICT_AGENT_ENABLED", "1")
    monkeypatch.setenv("CODEXBRIDGE_WORKFLOW_BRIDGE_PORT", context.endpoint.rsplit(":", 1)[1])
    DBOS(config=worker._dbos_config(run_dir / "dbos.sqlite"))
    DBOS.launch()
    try:
        with SetWorkflowID(str(adapter.checkpoint_identity["workflow_id"])):
            workflow_handle = DBOS.start_workflow(
                strict_executor.execute_strict_ows_workflow,
                run_dir.name,
                str(run_dir.parent),
                list(allowed_roots),
                "authorized_agent",
            )
        result = workflow_handle.get_result()
    finally:
        DBOS.destroy()

    ledger = AgentInvocationLedger.open_existing(
        run_dir,
        adapter.checkpoint_identity,
        context,
    )
    assert result["status"] == "succeeded"
    assert any(
        event_type == "branch_selected" and case_index == 0
        for event_type, _task_identity, _branch_class, case_index in events
    )
    assert not any(
        event_type == "branch_selected" and case_index == 1
        for event_type, _task_identity, _branch_class, case_index in events
    )
    assert provider.dispatch_count == 0
    if failure_kind == "dynamic_interrupt":
        assert provider.prepare_count == 0
        assert provider.preflight_count == 0
        assert ledger.list_invocations(limit=10) == ()
    else:
        records = ledger.list_invocations(limit=10)
        assert provider.prepare_count >= 1
        assert provider.preflight_count == 1
        assert len(records) == 1
        assert records[0].dispatch_state == "PREPARED"
        assert records[0].result_json is not None


@pytest.mark.asyncio
async def test_uncertain_agent_dispatch_is_handled_succeeded_and_readable(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import sqlite3
    import threading
    from pathlib import Path

    import httpx2 as httpx
    from starlette.applications import Starlette

    from codex_bridge.workflows import manager as manager_module
    from codex_bridge.workflows import strict_executor, worker, worker_entry
    from codex_bridge.workflows.agent_contracts import (
        AgentResult,
        DispatchReceipt,
        PreparedProviderRequest,
        ProviderCapabilities,
        ProviderRegistry,
    )
    from codex_bridge.workflows.agent_ledger import AgentInvocationLedger
    from codex_bridge.workflows.api import create_workflow_routes
    from codex_bridge.workflows.manager import (
        WorkflowWorkerManager,
    )
    from codex_bridge.workflows.service import WorkflowService

    tasks = [
        _agent_task(
            "invoke",
            _agent_wire(prompt="uncertain call", bindings={}, wait=True),
        ),
        {
            "route": {
                "switch": [
                    {
                        "handled_unknown": {
                            "when": '${ .status == "UNKNOWN" }',
                            "then": "handled_unknown",
                        }
                    },
                    {"unexpected_status": {"then": "unexpected_status"}},
                ]
            }
        },
        {"handled_unknown": {"set": {"handled": True}, "then": "exit"}},
        {"unexpected_status": {"set": {"handled": False}, "then": "exit"}},
    ]
    adapter, run_dir, allowed_roots, context = _reserved_agent_run(tmp_path, tasks)
    workflow_id = str(adapter.checkpoint_identity["workflow_id"])
    dispatch_entered = threading.Event()
    release_dispatch = threading.Event()
    branch_events: list[Any] = []

    class FakeProvider:
        dispatches = 0

        def capabilities(self, target: str) -> ProviderCapabilities:
            assert target == "default"
            return ProviderCapabilities(
                provider="codex",
                target="default",
                operations=("invoke", "observe", "wait"),
                session_modes=("new",),
                endpoint=context.endpoint,
                workspace=context.workspace,
            )

        def prepare(self, request: Any, target_context: Any) -> PreparedProviderRequest:
            assert target_context == context
            return PreparedProviderRequest(request, target_context)

        def preflight(self, prepared: Any) -> None:
            assert prepared.operation == "invoke"

        def dispatch(self, prepared: Any, client_request_id: str) -> DispatchReceipt:
            assert prepared.operation == "invoke"
            assert client_request_id.startswith("cba-")
            self.dispatches += 1
            dispatch_entered.set()
            if not release_dispatch.wait(timeout=90):
                raise TimeoutError("test dispatch gate expired")
            return DispatchReceipt.uncertain()

        def observe(self, *_args: Any) -> Any:
            raise AssertionError("UNKNOWN invoke must not trigger observe")

        def interrupt(self, *_args: Any) -> DispatchReceipt:
            raise AssertionError("I4-B has no interrupt operation")

    provider = FakeProvider()
    registry = ProviderRegistry({"codex": provider}, {"codex": "default"})
    monkeypatch.setattr(
        strict_executor,
        "build_codex_agent_runtime",
        lambda **_kwargs: (provider, registry, context),
    )
    interpreter_type = strict_executor.Interpreter

    def capture_interpreter(definition: Any, **kwargs: Any) -> Any:
        observer = kwargs.pop("observer", None)

        def capture(event: Any) -> None:
            if event.event_type == "branch_selected":
                branch_events.append(event)
            if observer is not None:
                observer(event)

        return interpreter_type(definition, observer=capture, **kwargs)

    monkeypatch.setattr(strict_executor, "Interpreter", capture_interpreter)
    monkeypatch.setenv("CODEXBRIDGE_WORKFLOW_STRICT_AGENT_ENABLED", "1")
    monkeypatch.setenv("CODEXBRIDGE_WORKFLOW_BRIDGE_PORT", "18001")
    manager = WorkflowWorkerManager(
        state_root=run_dir.parent,
        allowed_roots=tuple(allowed_roots),
        control_token="test-control-token",
        enable_isolated_strict_runtime=True,
        enable_strict_agent=True,
    )
    spawned: dict[str, Any] = {}

    class FakeWorkerProcess:
        pid = 43103

        async def wait(self) -> int:
            return 0

    async def fake_create_subprocess_exec(*args: Any, **kwargs: Any) -> FakeWorkerProcess:
        spawned["args"] = args
        spawned["env"] = kwargs["env"]
        return FakeWorkerProcess()

    monkeypatch.setattr(
        manager_module.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    worker_handle = await manager.start_worker(
        run_dir,
        mode="start",
        workspace_root=Path(context.workspace),
        max_live_codex_jobs=1,
        strict_runtime=True,
        codex_bridge_port=18001,
    )
    config_path = run_dir / f"worker-config-{manager._records[run_dir.name].launch_id}.json"
    for key in (
        worker.CONTROL_TOKEN_ENV,
        "CODEXBRIDGE_WORKFLOW_STRICT_AGENT_ENABLED",
        "CODEXBRIDGE_WORKFLOW_BRIDGE_PORT",
    ):
        monkeypatch.setenv(key, spawned["env"][key])

    service = WorkflowService(
        manager,
        codex_bridge_port=18001,
        enable_isolated_strict_runtime=True,
        enable_strict_agent=True,
    )

    worker_entry_task = asyncio.create_task(
        asyncio.to_thread(
            worker_entry.main,
            [
                "--mode",
                "start",
                "--run-dir",
                str(run_dir),
                "--config",
                str(config_path),
            ],
        )
    )
    try:
        assert await asyncio.to_thread(dispatch_entered.wait, 60)
        in_flight = await service.get_workflow(workflow_id)
        assert in_flight.status in {"starting", "running"}
    finally:
        release_dispatch.set()
        entry_exit_code = await worker_entry_task
        await worker_handle.wait(timeout_seconds=2)
        shutdown_report = await manager.shutdown()

    assert entry_exit_code == 0
    terminal = json.loads((run_dir / "strict-terminal.json").read_text(encoding="utf-8"))
    assert terminal["status"] == "succeeded"
    assert terminal["result"] == {}
    assert provider.dispatches == 1
    assert len(branch_events) == 1
    assert branch_events[0].case_index == 0

    ledger = AgentInvocationLedger.open_existing(
        run_dir,
        adapter.checkpoint_identity,
        context,
    )
    records = ledger.list_invocations(limit=10)
    assert len(records) == 1
    record = records[0]
    assert record.result_json is not None
    logical_result = AgentResult.from_json(record.result_json)
    assert record.dispatch_state == "MAYBE_SENT"
    assert logical_result.status == "UNKNOWN"
    assert logical_result.handle is None
    assert logical_result.error is not None
    assert logical_result.error["code"] == "dispatch_uncertain"
    assert shutdown_report.owned_turns == frozenset()
    assert shutdown_report.ownership_complete is False

    snapshot = await service.get_workflow(workflow_id)
    result_snapshot = await service.get_result(workflow_id)
    assert snapshot.status == "succeeded"
    assert result_snapshot.workflow.status == "succeeded"
    assert result_snapshot.result is not None
    assert result_snapshot.result["result"] == {}

    routes = create_workflow_routes(
        service,
        token="i4b-uncertain-api-token",
        ui_port=18001,
        authorize=lambda request, token: request.headers.get("authorization") == f"Bearer {token}",
        ready=lambda: True,
    )
    app = Starlette(routes=routes)
    headers = {"authorization": "Bearer i4b-uncertain-api-token"}
    prefix = f"/ui-api/control/workflows/{workflow_id}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1:18001",
    ) as client:
        status_response = await client.get(prefix, headers=headers)
        result_response = await client.get(f"{prefix}/result", headers=headers)
        list_response = await client.get(f"{prefix}/agent-invocations", headers=headers)
        assert status_response.status_code == 200, status_response.text
        assert result_response.status_code == 200, result_response.text
        assert list_response.status_code == 200, list_response.text
        assert status_response.json()["status"] == "succeeded"
        assert result_response.json()["status"] == "succeeded"
        listed = list_response.json()["invocations"]
        assert len(listed) == 1
        assert listed[0]["status"] == "UNKNOWN"
        assert listed[0]["handle"] is None
        assert listed[0]["error_code"] == "dispatch_uncertain"
        invocation_id = listed[0]["invocation_id"]
        detail_response = await client.get(
            f"{prefix}/agent-invocations/{invocation_id}",
            headers=headers,
        )
        assert detail_response.status_code == 200, detail_response.text
        assert detail_response.json()["status"] == "UNKNOWN"
        assert detail_response.json()["error_code"] == "dispatch_uncertain"
        assert "result_digest" in detail_response.json()
        assert "uncertain call" not in list_response.text
        assert "uncertain call" not in detail_response.text

    connection = sqlite3.connect(run_dir / "agent_invocations.sqlite")
    try:
        connection.execute(
            "UPDATE invocations SET result_json=NULL,result_sha256=NULL WHERE invocation_id=?",
            (record.invocation_id,),
        )
        connection.commit()
    finally:
        connection.close()
    incomplete_terminal = await service.get_workflow(workflow_id)
    assert incomplete_terminal.status == "needs_reconciliation"


@pytest.mark.asyncio
async def test_agent_run_with_legacy_codex_jobs_database_needs_reconciliation(
    tmp_path: Any,
) -> None:
    import sqlite3

    from codex_bridge.workflows.manager import WorkflowWorkerManager
    from codex_bridge.workflows.service import WorkflowService

    adapter, run_dir, allowed_roots, _context = _reserved_agent_run(
        tmp_path,
        [_agent_task("invoke")],
    )
    workflow_id = str(adapter.checkpoint_identity["workflow_id"])
    codex_jobs_path = run_dir / "codex_jobs.sqlite"
    connection = sqlite3.connect(codex_jobs_path)
    try:
        connection.execute(
            "CREATE TABLE jobs (job_id TEXT, workflow_id TEXT, state TEXT, "
            "thread_id TEXT, turn_id TEXT)"
        )
        connection.commit()
    finally:
        connection.close()
    dbos_connection = sqlite3.connect(run_dir / "dbos.sqlite")
    try:
        dbos_connection.execute("CREATE TABLE workflow_status (workflow_uuid TEXT, status TEXT)")
        dbos_connection.execute(
            "INSERT INTO workflow_status VALUES (?, ?)",
            (workflow_id, "RUNNING"),
        )
        dbos_connection.commit()
    finally:
        dbos_connection.close()

    manager = WorkflowWorkerManager(
        state_root=run_dir.parent,
        allowed_roots=tuple(allowed_roots),
        enable_isolated_strict_runtime=True,
        enable_strict_agent=True,
    )
    service = WorkflowService(
        manager,
        codex_bridge_port=18001,
        enable_isolated_strict_runtime=True,
        enable_strict_agent=True,
    )
    snapshot = await service.get_workflow(workflow_id)
    assert snapshot.status == "needs_reconciliation"


@pytest.mark.parametrize("checkpoint_conflict", ["error_checkpoint", "request_mismatch"])
def test_agent_preparation_checkpoint_conflicts_with_existing_ledger_row(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    checkpoint_conflict: str,
) -> None:
    from dbos import DBOS, SetWorkflowID

    from codex_bridge.workflows import strict_executor, worker
    from codex_bridge.workflows.agent_contracts import (
        AgentRequest,
        DispatchReceipt,
        PreparedProviderRequest,
        ProviderCapabilities,
        ProviderRegistry,
    )
    from codex_bridge.workflows.agent_ledger import AgentInvocationLedger
    from codex_bridge.workflows.contracts import sha256_json

    adapter, run_dir, allowed_roots, context = _reserved_agent_run(
        tmp_path,
        [_agent_task("invoke", _agent_wire(prompt="current request", bindings={}))],
    )
    ledger = AgentInvocationLedger.open_existing(
        run_dir,
        adapter.checkpoint_identity,
        context,
    )
    stale_request = AgentRequest(
        operation="invoke",
        provider="codex",
        target="default",
        target_context=context,
        session={"mode": "new"},
        handle=None,
        options={},
        prompt="different request",
        response={"format": "text"},
        wait=False,
        timeout_seconds=None,
    )
    stale_record = ledger.prepare_invoke(
        "/do/0/invoke",
        [],
        PreparedProviderRequest(stale_request, context),
        created_ms=1000,
    )
    checkpoint_request = AgentRequest(
        operation="invoke",
        provider="codex",
        target="default",
        target_context=context,
        session={"mode": "new"},
        handle=None,
        options={},
        prompt="checkpoint request",
        response={"format": "text"},
        wait=False,
        timeout_seconds=None,
    )
    checkpoint_request_json = checkpoint_request.to_json()

    class FakeProvider:
        dispatches = 0

        def capabilities(self, target: str) -> ProviderCapabilities:
            return ProviderCapabilities(
                provider="codex",
                target=target,
                operations=("invoke", "observe", "wait"),
                session_modes=("new",),
                endpoint=context.endpoint,
                workspace=context.workspace,
            )

        def prepare(self, request: Any, target_context: Any) -> PreparedProviderRequest:
            return PreparedProviderRequest(request, target_context)

        def preflight(self, prepared: Any) -> None:
            return None

        def dispatch(self, prepared: Any, client_request_id: str) -> DispatchReceipt:
            self.dispatches += 1
            return DispatchReceipt.uncertain()

        def observe(self, *_args: Any) -> Any:
            raise AssertionError("error preparation checkpoint must not observe")

        def interrupt(self, *_args: Any) -> DispatchReceipt:
            raise AssertionError("I4-B has no interrupt operation")

    provider = FakeProvider()
    registry = ProviderRegistry({"codex": provider}, {"codex": "default"})
    monkeypatch.setattr(
        strict_executor,
        "build_codex_agent_runtime",
        lambda **_kwargs: (provider, registry, context),
    )
    checkpoint_seen = False

    def error_prepare_step(_step_identity: str) -> Any:
        def make_checkpoint(*args: Any) -> dict[str, Any]:
            nonlocal checkpoint_seen
            checkpoint_seen = True
            (
                _state_root,
                run_id,
                _roots,
                identity,
                _port,
                node_id,
                iteration_vector,
                task_name,
                _task_spec,
                input_data,
                variables,
            ) = args
            assert run_id == run_dir.name
            return {
                "checkpoint_version": 1,
                "kind": "agent_request_preparation",
                "binding_identity": identity,
                "node_id": node_id,
                "iteration_vector": iteration_vector,
                "task_name": task_name,
                "input_sha256": sha256_json(input_data),
                "variables_sha256": sha256_json(variables),
                "invocation_id": ledger.invocation_id_for(node_id, iteration_vector),
                "request_json": (
                    None if checkpoint_conflict == "error_checkpoint" else checkpoint_request_json
                ),
                "error_code": (
                    "invalid_request" if checkpoint_conflict == "error_checkpoint" else None
                ),
            }

        return make_checkpoint

    monkeypatch.setattr(strict_executor, "_prepare_agent_request_step", error_prepare_step)
    monkeypatch.setenv("CODEXBRIDGE_WORKFLOW_STRICT_AGENT_ENABLED", "1")
    monkeypatch.setenv("CODEXBRIDGE_WORKFLOW_BRIDGE_PORT", "18001")

    DBOS(config=worker._dbos_config(run_dir / "dbos.sqlite"))
    DBOS.launch()
    result: Any = None
    workflow_error: Exception | None = None
    workflow_handle: Any = None
    try:
        with SetWorkflowID(str(adapter.checkpoint_identity["workflow_id"])):
            workflow_handle = DBOS.start_workflow(
                strict_executor.execute_strict_ows_workflow,
                run_dir.name,
                str(run_dir.parent),
                list(allowed_roots),
                "authorized_agent",
            )
        try:
            result = workflow_handle.get_result()
        except Exception as error:
            workflow_error = error
            result = None
    finally:
        DBOS.destroy()

    assert checkpoint_seen
    assert result is None or result.get("status") != "succeeded"
    assert isinstance(workflow_error, strict_executor.StrictWorkflowExecutionError)
    assert workflow_error.reason_code == "strict_execution_uncertain"
    assert provider.dispatches == 0
    preserved = ledger.get_invocation(stale_record.invocation_id)
    assert preserved.result_json is None
    assert preserved.dispatch_state == "PREPARED"
