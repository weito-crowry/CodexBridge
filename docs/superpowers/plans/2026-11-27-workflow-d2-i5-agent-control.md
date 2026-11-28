# Workflow D2-I5 Implementation Plan

> **For agentic workers:** Use this plan task-by-task in the current checkout. Steps use checkbox (`- [x]`) syntax for tracking. The approved workflow requires one agent working sequentially.

**Goal:** Complete I5 support for continuing an exact existing thread, interrupting an explicit exact turn, validating new-session Codex options, and documenting whether exact-turn token usage is available.

**Architecture:** Reuse the existing authorized UI control routes and schema-v1 Agent ledger. Add bounded methods to the Agent-specific HTTP client, implement the I5 behavior in the Codex provider, and connect interrupt receipts to the existing claim/replay executor path without creating ownership or polling.

**Tech Stack:** Python, pytest, SQLite schema v1, DBOS, Windows PowerShell, `uv`, Ruff, mypy.

**Spec:** `docs/workflow-design/phases/D2-I0-agent-runtime-contract.md` (authoritative §§2, 4–10), with `docs/workflow-design/requirements.md` and `docs/workflow-design/phases/D2-agent-nodes.md`.

## Global Constraints

- Implement I5 only; do not advance D5 loop policy, D8 GUI/MCP, or I6 production acceptance.
- Keep the I4-B base `c627e29a50a76926e25de05d9e98ef078e865df4`; keep strict Agent disabled by default.
- Use the existing checkout and `codex/workflow-d2-i5-agent-control`; do not create a worktree or use delegation.
- Preserve all pre-existing uncommitted and untracked files; stage only files created or changed for I5.
- Do not access or modify a real DBOS database, live Bridge, or real configuration. Use isolated SQLite/DBOS and loopback fixtures.
- Keep UI control routes, authorization, workspace path checks, inactive checks, and per-thread serialization authoritative.
- Keep body cap 64 KiB, prompt cap 32,000 Unicode characters, request ID `cba-` plus 64 lowercase hex, quoted path IDs, loopback pinning, and existing Agent transport cap.
- Preserve old D1 HTTP payloads, default timeout, response cap, old Codex job path, DDL/schema v1, and I3/I4 replay/checkpoint contracts.
- Do not add issue-specific behavior, fixed execution/poll limits, budgets, deadlines, retries, automatic interrupts, new routes, or dependencies.
- Never classify arbitrary HTTP 4xx or ambiguous 409 as definitely unsent; never retry after MAYBE_SENT.

## Review Focus

1. Missing, malformed, unavailable, duplicate, or stale model catalog; unknown model/effort and effort without model must fail before claim and POST zero times.
2. Continue with a literal or jq-derived thread ID must send only `client_request_id` and `prompt`, preserve the exact thread ID, omit `cwd`, and own only a newly accepted exact turn.
3. Continue options are unsupported before POST; malformed, mismatched, active, unauthorized, or ambiguous responses must not create ownership or reuse an older turn.
4. Interrupt is scoped to one explicit thread/turn; accepted ACK stores the exact handle and response state but does not claim terminal completion, create an owner, or poll.
5. Crash after interrupt receipt persistence and before checkpoint must replay the saved receipt/result without a second POST; uncertain POST and ambiguous 409 remain UNKNOWN.

---

### Task 1: Agent HTTP transport and exact route payloads

**Files:**
- Modify: `src/codex_bridge/workflows/http_client.py`
- Test: `tests/test_workflows_http_client.py`

**Interfaces:**
- Produces `CodexAgentHTTPClient.continue_agent_thread(thread_id, payload)`, `interrupt_agent_turn(thread_id, turn_id, payload)`, and an authenticated control-capabilities read used only by the Agent provider.
- Continues using `validate_agent_thread_payload` for the 64 KiB new-session request check; does not change the legacy `CodexHTTPClient.create_thread` contract.

- [x] Add tests for exact new-options payload validation, continue body/path/query quoting, interrupt body/path, request-size enforcement, and preserved legacy create payload.
- [x] Run those tests and confirm the new I5 assertions fail against the current client.
- [x] Add only the Agent transport methods and validators; all POST methods preserve HTTP status for certainty classification and retain the existing timeout/response limits.
- [x] Run the focused HTTP client tests and confirm they pass.

### Task 2: Codex capabilities, new-session options, continue, and interrupt provider

**Files:**
- Modify: `src/codex_bridge/workflows/agent_contracts.py`
- Modify: `src/codex_bridge/workflows/codex_agent_provider.py`
- Test: `tests/test_workflow_d2_i3_codex_provider.py`

**Interfaces:**
- Keeps the public capability helper and saved-definition registry as the shared source for implemented operations/options.
- `preflight(prepared)` validates a requested model/effort against the current authorized model catalog before the ledger claim; `dispatch` uses the Task 1 transport.

- [x] Add failing provider tests for new model/effort/title/sandbox payloads, catalog valid/unknown/malformed/unavailable cases, effort-only rejection, default options, full-access confirmation, continue exact identity/no `cwd`/unsupported options, and interrupt exact ACK/mismatch/ambiguous response.
- [x] Run the focused provider tests and confirm each new I5 behavior fails before implementation.
- [x] Synchronize live and static capabilities; implement new-only options, exact continue dispatch, exact-handle interrupt, and conservative post-POST certainty mapping.
- [x] Confirm provider tests pass and preflight rejection produces zero POSTs.

### Task 3: Schema-v1 interrupt ledger and dispatch-only executor path

**Files:**
- Modify: `src/codex_bridge/workflows/agent_ledger.py`
- Modify: `src/codex_bridge/workflows/agent_executor.py`
- Test: `tests/test_workflow_d2_i3_agent_ledger.py`
- Test: `tests/test_workflow_d2_i3_agent_executor.py`

**Interfaces:**
- Reuses the existing `operation='interrupt'` and `dispatch_state` values; no migration or schema change.
- Accepted interrupt receipt/result retain the exact request handle, while `owned_handles` remains invoke-only.

- [x] Add failing SQLite tests for interrupt PREPARED/MAYBE_SENT/ACCEPTED/REJECTED relationships, exact receipt handle, no owner, and schema-v1 reopen compatibility.
- [x] Add failing executor tests for one POST, no observe/poll, ACK-as-ACCEPTED, uncertain-as-UNKNOWN, and receipt-before-checkpoint replay without repost.
- [x] Run the targeted tests and confirm they fail for the intended missing interrupt path.
- [x] Extend operation-specific validation and replay handling; preserve invoke ownership/results and read-only observe/wait behavior.
- [x] Run focused ledger/executor tests, including the isolated DBOS receipt/checkpoint rewind case, and confirm they pass.

### Task 4: Strict runtime preparation failure and Workflow compatibility

**Files:**
- Modify if needed: `src/codex_bridge/workflows/strict_executor.py`
- Test: `tests/test_workflow_d2_i4b_runtime.py`
- Test: `tests/test_workflow_d2_i3_agent_executor.py`

**Interfaces:**
- Keeps I4-B admission, worker, ownership, metadata, strict flag, and terminal-result semantics unchanged; permits the already-supported `interrupt` operation in the existing dynamic-prepare failure result path.

- [x] Add a failing strict test for a dynamically evaluated interrupt that is rejected before dispatch and can be handled as a Workflow result.
- [x] Run the test and verify the expected failure is operation admission, not fixture or DBOS setup.
- [x] Make the smallest operation allow-list adjustment only if the test confirms it is needed.
- [x] Run I5 strict/fake Workflow cases and relevant I4-B/worker regressions.

### Task 5: Usage evidence, final verification, commit, and push

**Files:**
- Create: `docs/workflow-design/phases/D2-I5-agent-control-evidence.md`
- Include this implementation plan in the I5 commit.

- [x] Record the exact-turn usage source inspection: rate-limit snapshots are account-window aggregates associated with turns by the Console, while status/turn/item history exposes no verified exact-turn token counts; keep `usage_supported=false` and `usage=null`.
- [x] Run the I1–I5 target pytest set, related UI control/HTTP/D1 strict/worker regressions, Ruff check, Ruff format check, mypy, compileall, and `git diff --check`.
- [x] Review the full diff against this plan, ensure protected paths are absent, then commit only I5 source/tests/docs.
- [x] Push normally to `origin/codex/workflow-d2-i5-agent-control`; do not create a PR, merge, rebase, or enable the feature.
