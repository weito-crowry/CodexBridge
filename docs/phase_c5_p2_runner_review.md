# Phase C5-P2A runner review preparation

This document describes the offline implementation prepared for Issue #155. It is a review aid; no isolated Bridge or Worker was started, no Workflow start POST was issued, and no real Codex job was started during implementation.

## Scope and runtime contract

- The only implementation artifacts are `scripts/phase_c5_workflow_isolated_acceptance.py`, `tests/test_phase_c5_workflow_isolated_acceptance.py`, and this document.
- `python -m scripts.phase_c5_workflow_isolated_acceptance` prints help and exits without starting a process or making a request.
- `python -m scripts.phase_c5_workflow_isolated_acceptance --prepare` validates the fixed source/test inventory, OWS 1.0.3 fixtures, run counts, and CLI formats offline. It has no Bridge, Worker, network, or Workflow POST call path.
- Live mode requires all three values: `--execute`, `--approved-head=<exact-reviewed-40-character-SHA>`, and `--approval-id=<externally-supplied-32-character-lowercase-hex>`.
- Live preflight requires an exact HEAD match, the C5-P1 parent as an ancestor, clean `src/`, `scripts/`, and `tests/` paths, and byte equality between the checked out runner and the file committed at that SHA. It also checks Windows native Python from the repository `.venv`, workflow/UI dependencies, the Codex executable and authentication source, isolated roots, and ports.
- The runner does not invoke B5 `_main` or `_run_execution_gate`. It reuses only the isolated context, runtime environment, Bridge startup, read-only verification, and owned-process helpers.

## One-shot workflow and budget

Run A contains one fixed `codex-job` call, run B contains two sequential fixed calls, and run C contains one fixed call inside a three-iteration input loop with an enabled switch path. The C input is fixed to three unique values with `enabled=true`; all three definitions are validated with `validate_workflow(..., codex_job_live=True, max_live_codex_jobs=N)` and their static graph node IDs are checked before each start.

Each externally supplied approval ID is claimed with an exclusive persistent marker. Immediately before each initial POST, the marker is atomically advanced to `maybe_sent` and the run's client request ID and POST budget are persisted. A transport exception, malformed/non-202 response, timeout, reconciliation outcome, missing ledger, inconsistent evidence, or failed result stops all later runs. The runner contains no POST retry, same-ID replay, resume, or internally generated replacement approval ID. The total cap is three initial Workflow POSTs and six Codex jobs.

After all three runs succeed, the runner stops only the identity-verified Bridge it started, reserves a different safe loopback pair, restarts with the same isolated state, and performs HTTP/MCP/offscreen read-only checks. It compares task/event coverage, snapshot versions, DBOS status, job ledger, receipts, manifest/result hashes, and workflow request rows. No POST or new job is used for this readback. Gate 1 remains `NOT_RUN` until a person checks the visible isolated GUI; offscreen Qt readback does not count as visual acceptance.

The live evidence writer uses a fixed-field sanitizer and refuses secret-bearing values, raw prompts/results, and absolute paths. It writes `docs/phase_c5_workflow_acceptance-<approval-id>.json` and `docs/phase_c5_p2_live_acceptance.md` only when an explicitly approved live command is run. The runner does not perform Git operations.

## Offline verification targets

The runner-only suite uses fixed fake adapters and temporary marker files to exercise the 1/2/3 budget, duplicate approval rejection, pre-POST persistent consumption, transport-unknown stop, transient reconciliation polling, fail-fast status handling, result/job count checks, graph/event node identity, root/port guards, owner mismatch shutdown refusal, evidence sanitization, and harmless CLI paths. The zero-iteration and false-switch cases use the OWS interpreter with a fake call handler and start no Codex process.

## Issue #156 review correction

The first review found that `_verify_live_run` hashed only `workflow`, `input`, and `workspace_root`. Product `WorkflowService._prepare_request` also includes `max_live_codex_jobs` for limits 2 and 3. The runner now builds an explicit digest payload and adds that key only for the two multi-job cases; the one-job digest keeps the original three fields.

The new offline digest tests call the real `WorkflowService._prepare_request` with each fixed 1/2/3-job definition, input, and a temporary canonical workspace, then compare its digest with the helper used by `_verify_live_run`. They also prepare deliberately different job limits and verify their digests do not match. A fake `requests.sqlite`, manifest, Worker status, DBOS status, job ledger, and task projection exercise `_verify_live_run`: matching request-row and manifest digests pass, while either mismatch is rejected. These tests do not call `start_workflow` and use no network, Bridge, Worker, or Codex process.

The C5 final visible GUI inspection remains unperformed. Existing B5 `_qt_readback` covers list, detail, and result, but does not directly verify the C4 Execution graph GUI/API integration. This correction does not expand that integration; Gate 1 remains `NOT_RUN` until a separate human visual check.

Related offline regression tests selected for the implementation turn:

- `tests/test_phase_b5_workflow_isolated_smoke.py`
- `tests/test_phase_c5_gui_offline_acceptance.py`
- `tests/test_workflow_task_projection.py`
- `tests/test_workflow_task_api.py`
- `tests/test_workflow_task_mcp.py`
- `tests/test_workflow_node_events.py`
- `tests/test_workflows_multijob.py`

## Review and later execution boundary

The user has approved a maximum of six real Codex jobs across runs A/B/C (1+2+3), but live execution remains at zero until the runner receives a passing GitHub code review and a later explicit command names the exact reviewed SHA and externally supplied approval ID. The example below is documentation only and was not executed:

```powershell
python -m scripts.phase_c5_workflow_isolated_acceptance --execute --approved-head=<reviewed-40-hex-sha> --approval-id=<externally-issued-32-lowercase-hex>
```

After Gate 2 and Gate 3, the user still needs to inspect the visible isolated GUI and record that human decision separately. No PR, merge, rebase, force-push, tag, release, or production deployment is part of this runner task.
