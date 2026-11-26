from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from codex_bridge.workflows import strict_executor
from codex_bridge.workflows.strict_variable_runtime import (
    StrictWorkflowVariableRuntimeAdapter,
)
from tests.test_workflow_d1_i7b_variable_runtime import (
    _WORKFLOW_ID,
    _call,
    _reserved_adapter,
)


def _durable_tasks() -> list[dict[str, Any]]:
    return [
        {
            "initialize_count": {
                "call": "codexbridge-variable",
                "with": _call("initialize", "count", value=3),
            }
        },
        {
            "loop": {
                "for": {"in": "${ [0, 1] }", "each": "item", "at": "index"},
                "do": [
                    {
                        "choose": {
                            "switch": [
                                {
                                    "increment_count": {
                                        "when": "${ $cbvars.workflow.count >= 3 }",
                                        "then": "increment_count",
                                    }
                                },
                                {"default": {"then": "exit"}},
                            ]
                        }
                    },
                    {
                        "increment_count": {
                            "call": "codexbridge-variable",
                            "with": _call("increment", "count", by=1),
                        }
                    },
                ],
            }
        },
        {
            "read_count": {
                "input": {"from": '${ {"input_seen": $cbvars.workflow.count} }'},
                "set": "${ $cbvars.workflow.count }",
                "output": {"as": "${ . + $cbvars.workflow.count }"},
                "export": {"as": '${ {"seen": $cbvars.workflow.count, "task": .} }'},
            }
        },
    ]


def _run_direct(adapter: StrictWorkflowVariableRuntimeAdapter, run_directory: Path) -> Any:
    return strict_executor._execute_verified_run(
        adapter,
        state_root=str(run_directory.parent),
        run_id=run_directory.name,
        allowed_roots=[str(run_directory.parent.parent / "workspace")],
    )


def test_strict_executor_rejects_external_codex_target_before_variable_effects(
    tmp_path: Path,
) -> None:
    tasks = [
        {
            "external": {
                "call": "codex-job",
                "with": {"prompt": "must-not-run"},
            }
        }
    ]
    adapter, run_directory = _reserved_adapter(tmp_path, tasks=tasks, output="${ {} }")

    with pytest.raises(strict_executor.StrictWorkflowExecutionError) as error:
        _run_direct(adapter, run_directory)

    assert error.value.reason_code == "strict_execution_target_unavailable"
    assert "must-not-run" not in str(error.value)
    assert adapter._store.inspect_ledger().mutation_count == 0


def test_strict_output_violation_is_not_returned_as_a_success_result(tmp_path: Path) -> None:
    adapter, run_directory = _reserved_adapter(tmp_path, output="${ 7 }")

    with pytest.raises(strict_executor.StrictWorkflowExecutionError) as error:
        _run_direct(adapter, run_directory)

    assert error.value.reason_code == "output_contract_violation"
    assert adapter._store.inspect_ledger().mutation_count == 0


def test_reserved_identity_mismatch_is_reconciliation_not_a_run_override(
    tmp_path: Path,
) -> None:
    adapter, run_directory = _reserved_adapter(tmp_path, tasks=_durable_tasks(), output="${ {} }")
    identity = dict(adapter.checkpoint_identity)
    identity["source_sha256"] = "0" * 64

    with pytest.raises(strict_executor.StrictWorkflowExecutionError) as error:
        strict_executor._verified_adapter(
            str(run_directory.parent),
            run_directory.name,
            [str(run_directory.parent.parent / "workspace")],
            identity,
        )

    assert error.value.reason_code == "strict_execution_uncertain"


def test_prepared_apply_step_replay_after_commit_returns_historical_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, run_directory = _reserved_adapter(tmp_path, output="${ {} }")
    node_id = "/do/0/initialize_count"
    prepared = adapter.prepare_mutation(_call("initialize", "count", value=8), node_id=node_id)
    payload = adapter.serialize_prepared_mutation(prepared)
    execution_key = str(payload["execution_key"])
    original_apply = StrictWorkflowVariableRuntimeAdapter.apply_prepared
    attempts = 0

    def commit_then_interrupt(
        runtime: StrictWorkflowVariableRuntimeAdapter, mutation: Any
    ) -> dict[str, Any]:
        nonlocal attempts
        result = original_apply(runtime, mutation)
        attempts += 1
        if attempts == 1:
            raise OSError("private interruption detail")
        return result

    monkeypatch.setattr(
        StrictWorkflowVariableRuntimeAdapter, "apply_prepared", commit_then_interrupt
    )
    step_arguments = (
        str(run_directory.parent),
        run_directory.name,
        [str(run_directory.parent.parent / "workspace")],
        adapter.checkpoint_identity,
        payload,
        node_id,
        [],
        execution_key,
    )

    with pytest.raises(strict_executor.StrictWorkflowExecutionError) as interrupted:
        strict_executor._apply_mutation_checkpoint_body(*step_arguments)
    assert interrupted.value.reason_code == "strict_execution_uncertain"
    assert "private interruption detail" not in str(interrupted.value)

    later = adapter.prepare_mutation(
        _call("increment", "count", by=1), node_id="/do/1/increment_count"
    )
    assert adapter.apply_prepared(later) == {"present": True, "value": 9}
    checkpoint = strict_executor._apply_mutation_checkpoint_body(*step_arguments)
    assert checkpoint["result"] == {"present": True, "value": 8}
    assert adapter.projection_for_phase("after-replay", "/final")["workflow"]["count"] == 9
    assert adapter._store.inspect_ledger().mutation_count == 2


def test_tampered_prepared_arguments_stop_before_the_i4_apply(tmp_path: Path) -> None:
    adapter, run_directory = _reserved_adapter(tmp_path, output="${ {} }")
    prepared = adapter.prepare_mutation(
        _call("initialize", "count", value=8), node_id="/do/0/initialize_count"
    )
    payload = adapter.serialize_prepared_mutation(prepared)
    payload["operation_args"]["value"] = 9
    execution_key = str(payload["execution_key"])

    with pytest.raises(strict_executor.StrictWorkflowExecutionError) as error:
        strict_executor._apply_mutation_checkpoint_body(
            str(run_directory.parent),
            run_directory.name,
            [str(run_directory.parent.parent / "workspace")],
            adapter.checkpoint_identity,
            payload,
            "/do/0/initialize_count",
            [],
            execution_key,
        )

    assert error.value.reason_code == "strict_execution_uncertain"
    assert adapter._store.inspect_ledger().mutation_count == 0


def test_durable_projection_checkpoint_binds_phase_and_reserved_identity(
    tmp_path: Path,
) -> None:
    adapter, run_directory = _reserved_adapter(tmp_path, output="${ {} }")
    node_id = "/do/0/initialize_count"
    phase = "operation"
    vector: list[int] = []
    phase_id = _phase_id(phase, node_id, vector)

    checkpoint = strict_executor._projection_checkpoint_body(
        str(run_directory.parent),
        run_directory.name,
        [str(run_directory.parent.parent / "workspace")],
        adapter.checkpoint_identity,
        phase,
        node_id,
        vector,
        phase_id,
    )

    assert checkpoint["kind"] == "phase_projection"
    assert checkpoint["binding_identity"] == adapter.checkpoint_identity
    assert checkpoint["projection"] == {}


def _phase_id(phase: str, node_id: str, iteration_vector: list[int]) -> str:
    from codex_bridge.workflows.contracts import canonical_json

    return f"{node_id}#{phase}:{canonical_json(iteration_vector)}"


def test_actual_dbos_launch_executes_only_verified_reserved_strict_snapshot(
    tmp_path: Path,
) -> None:
    from dbos import DBOS, SetWorkflowID

    from codex_bridge.workflows.worker import _dbos_config

    adapter, run_directory = _reserved_adapter(
        tmp_path,
        tasks=_durable_tasks(),
        output="${ if $cbvars.workflow.count == 5 then {} else 7 end }",
    )
    dbos_config = _dbos_config(run_directory / "dbos.sqlite")
    DBOS(config=dbos_config)
    DBOS.launch()
    try:
        with SetWorkflowID(_WORKFLOW_ID):
            handle = DBOS.start_workflow(
                strict_executor.execute_strict_ows_workflow,
                run_directory.name,
                str(run_directory.parent),
                [str(run_directory.parent.parent / "workspace")],
                "isolated",
            )
        assert handle.workflow_id == _WORKFLOW_ID
        try:
            result = handle.get_result()
        except strict_executor.StrictWorkflowExecutionError as error:
            pytest.fail(f"DBOS strict run returned safe reason: {error.reason_code}")
    finally:
        DBOS.destroy()

    DBOS(config=dbos_config)
    DBOS.launch()
    try:
        recovered = DBOS.retrieve_workflow(_WORKFLOW_ID)
        recovered_result = recovered.get_result()
    finally:
        DBOS.destroy()

    assert result["status"] == "succeeded"
    assert result["run_id"] == run_directory.name
    assert result["workflow_id"] == _WORKFLOW_ID
    assert result["result"] == {}
    assert recovered_result == result
    assert adapter._store.inspect_ledger().mutation_count == 3


def test_executor_requires_explicit_isolated_mode_before_opening_run(tmp_path: Path) -> None:
    adapter, run_directory = _reserved_adapter(tmp_path, output="${ {} }")
    original_dbos = strict_executor.DBOS

    class _NoWorkflowDBOS:
        workflow_id = _WORKFLOW_ID

    strict_executor.DBOS = _NoWorkflowDBOS  # type: ignore[assignment]
    try:
        with pytest.raises(strict_executor.StrictWorkflowExecutionError) as error:
            strict_executor.execute_strict_ows_workflow.__wrapped__(
                run_directory.name,
                str(run_directory.parent),
                [str(run_directory.parent.parent / "workspace")],
                "production",
            )
    finally:
        strict_executor.DBOS = original_dbos

    assert error.value.reason_code == "strict_execution_mode_invalid"
    assert adapter._store.inspect_ledger().mutation_count == 0


def test_executor_checks_dbos_workflow_id_against_verified_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, run_directory = _reserved_adapter(tmp_path, output="${ {} }")

    class _WrongWorkflowDBOS:
        workflow_id = "wrong-db-os-workflow"

    monkeypatch.setattr(strict_executor, "DBOS", _WrongWorkflowDBOS)
    with pytest.raises(strict_executor.StrictWorkflowExecutionError) as error:
        strict_executor.execute_strict_ows_workflow.__wrapped__(
            run_directory.name,
            str(run_directory.parent),
            [str(run_directory.parent.parent / "workspace")],
            "isolated",
        )

    assert error.value.reason_code == "strict_execution_uncertain"
    assert adapter._store.inspect_ledger().mutation_count == 0
