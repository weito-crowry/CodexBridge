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

## Issue #157 pre-POST ledger correction

`execute_runs` persists the current request as `maybe_sent` before calling `LiveAdapter.start`. The pre-POST readiness check now receives that fixed current case and ID, requires every earlier A/B run to be `succeeded`, the current run alone to be `maybe_sent`, and later runs to remain `not_started` without IDs. It compares only successful prior IDs with `requests.sqlite`, rejects a current-ID row, duplicates, missing or foreign rows, state and counter inconsistencies, and preserves the POST budget burn before transport. `LiveAdapter.start` still increments its in-memory POST count only after readiness passes. B5 identity, listener, app-server, worker, and workspace guards remain in place.

Offline tests use the real `LiveAdapter.start`, `_assert_bridge_ready`, and `execute_runs` with a fake B5 preflight, fake owned process identity, temporary SQLite state, and fake HTTP start function. They verify pre-POST DB row counts of 0/1/2 for A/B/C, budget totals of three initial POSTs and six jobs, rejection cases with zero fake POSTs, and no resend after an unknown transport outcome. No Bridge, Worker, real HTTP POST, or Codex job was started for this correction.

## Issue #158 isolated state-root preflight correction

The B5 context helper returns `temp_root/localappdata/CodexBridge/workflows` before Bridge startup without creating the Bridge-owned state hierarchy. C5 now validates that exact path in two phases: before startup, the hierarchy must be absent and the runner does not create it; after mocked or approved Bridge startup, the exact path must exist as an accessible, non-reparse directory before run execution. The context `LOCALAPPDATA` must resolve to the isolated `temp_root/localappdata`. Both phases retain isolated workspace, root-boundary, production-port, and reparse checks.

The pre-POST check revalidates the strict post-start layout before each run. For prior successful runs, request rows must match the persistent ledger's `client_request_id`, `workflow_id`, and `run_id`; the DB state may be `running` or `succeeded`. Foreign or duplicate rows, identity mismatch, and any other state stop before the fake POST boundary. New offline tests use temporary roots and SQLite, fake B5 context creation/start, and a fake run boundary. They do not start a Bridge or Worker, issue a Workflow POST, or create a Codex job.

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

## Issue #166 Gate 0 state initialization and diagnostics

This section supersedes the Issue #158 pre-start statement above that C5 leaves the state hierarchy absent until Bridge startup.

The earlier live attempt recorded `isolated_root_unavailable`, Gate 0 `BLOCKED`, zero initial POSTs, zero Codex jobs, and runs A/B/C `NOT_RUN`. That evidence did not record the failing stage, so it does not establish whether the strict state-root check failed before or after Bridge startup. The source flow and offline reproduction show that a fresh B5 context intentionally has no `localappdata/CodexBridge/workflows` directory, while C5 had required it immediately after startup. The precise stage of the earlier live failure remains an inference.

C5 now validates the fresh isolated layout and canonical `LOCALAPPDATA`, then creates only the isolated `CodexBridge` and `workflows` directories with separate exclusive `mkdir` calls before starting its Bridge. Each parent and new directory is checked for reparse points, path containment, ownership, and access. Strict layout checks run before Bridge startup, after startup, and again before Workflow POST readiness. The product's lazy `mkdir(parents=True, exist_ok=True)` behavior remains compatible; an offline test exercises the existing service and definition repository helpers against the initialized temporary state root. B5 and product source are unchanged.

Future evidence paths are unique per externally supplied approval ID: `phase_c5_workflow_acceptance-<approval-id>.json` and `phase_c5_p2_live_acceptance-<approval-id>.md`. The prior fixed summary and other approval IDs do not block a new ID; a collision at either target for the same ID blocks without overwrite. This change does not modify or reuse the already consumed attempt marker or the prior failed evidence.

Evidence now records a fixed `gate0_stage` and tri-state `owned_bridge_started` / `owned_bridge_shutdown_confirmed` values. It records no exception text, absolute paths, process identifiers, ports, commands, tokens, or prompts. Ambiguous startup or cleanup remains `UNKNOWN`/`BLOCKED`; cleanup still requires the runner's verified owned-process identity.

Offline verification for Issue #166: the C5 runner suite reported 109 passed and 1 skipped (Windows denied creation of the test symlink, WinError 1314). The selected GUI-offscreen, B5, C2, and C3 related suite reported 272 passed and 2 skipped (Windows denied two test symlink creations). Ruff check and format passed; `mypy src` passed for 132 source files; mypy passed for the runner with `--follow-imports=skip --disable-error-code=import-untyped`; compileall and `git diff --check` passed. This implementation turn made zero real Bridge/Worker starts, zero Workflow POSTs, zero Codex jobs, and zero visible GUI launches. The consumed approval ID was not retried and no new live attempt was authorized or run.

## Issue #167 Codex job event trace and usage accounting

The second approved live attempt completed run A's Workflow, DBOS, Worker, Codex job, receipt, and expected-result checks, then failed because C5 expected only two job-event rows per job. The product `CodexJobAdapter` emits `PREPARED -> MAYBE_SENT -> ACCEPTED -> SUCCEEDED`, with `RUNNING` optional, so the two-row assumption rejected a valid completed job. Run B and C were not started. The previously committed live evidence remains unchanged; its `observed_codex_jobs: 0` represented jobs accepted by the runner's verifier, not actual job usage. The attempt consumed one successful Codex job, as confirmed by its completed receipt.

C5 now validates each job's full event trace independently. It requires positive, strictly increasing, unique integer event IDs; event rows must belong to the verified job set; every job must have exactly `PREPARED, MAYBE_SENT, ACCEPTED, SUCCEEDED` or `PREPARED, MAYBE_SENT, ACCEPTED, RUNNING, SUCCEEDED`. Missing, duplicated, reordered, unknown, failed, reconciliation, or foreign-job events fail closed. The event read is bounded to five rows per job and the six-job maximum. Product Adapter and schema code were not changed.

Offline tests build the event rows by driving the real `CodexJobAdapter` API against temporary SQLite ledgers, then pass 1/2/3-job traces (with and without `RUNNING`) through `_verify_live_run`. Mutation tests reject the legacy two-row trace and malformed or incomplete histories. A fake `execute_live` failure after a job was written verifies the distinction between `verified_completed_job_count` (0 until the runner completes verification) and `read_only_ledger_job_rows` (1 when the safely owned Bridge has shut down, the Worker observer has finished, and the isolated root passes strict validation). The latter is `null` when ownership, cleanup, layout, or readout is uncertain; no zero is inferred. Both fields are bounded and sanitized. The summary labels them separately.

Issue #167 offline verification on Windows native: the C5 runner suite reported 145 passed and 1 skipped (Windows denied a test symlink, WinError 1314). The selected B5/C5 offscreen, Workflow projection/API/MCP/node-event, and multijob regression suite reported 356 passed and 3 skipped (Windows denied test symlink creation). The focused event/digest/accounting regression rerun reported 27 passed. Ruff check and format passed for the runner and its test; `mypy src` passed for 132 source files; the runner passed mypy with `--follow-imports=skip --disable-error-code=import-untyped` because it imports the untyped B5 script helpers; compileall and `git diff --check` passed. This correction turn made zero real Bridge/Worker starts, zero Workflow POSTs, zero Codex jobs, zero live `--execute` calls, and zero visible GUI launches. The two prior attempt IDs, budget markers, and live evidence files were not reused or modified.
