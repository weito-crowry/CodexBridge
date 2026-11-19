# D1-I4-A variables.sqlite v1 schema evidence

**Status: IMPLEMENTED / PENDING_CHATGPT_REVIEW**
**Date:** 2026-11-19
**Branch:** `codex/workflow-d1-i4-variable-store`

## Scope and current phase state

I4-A implements the physical variables.sqlite v1 schema in ADR 0004 §4, explicit
initialization in an existing run directory, atomic creation of the initial
RunBinding and workflow root ScopeFrame, and a read-only strict inspector for
that initial state. The implementation is opt-in and is not connected to a
runtime, worker, interpreter, API, MCP surface, or DBOS checkpoint.

I4-B declarations, typed variable operations, and frame lifecycle are not
implemented. I4-C mutation ledger semantics, CAS, and DBOS replay safety are
not implemented. The inspector rejects non-empty variable tables and any frame
other than the initial root, so it does not attest to unimplemented histories.
I4-A implementation does not constitute acceptance of I4 as a whole.

Current D1 state recorded for this slice: I0, I1, and I3 are ACCEPTED (with
caveats); I2 is DEFERRED / NON-BLOCKING; I4 is IN_PROGRESS; I5-I8 are
NOT_STARTED. D1 remains DRAFT and strict Workflow run admission remains OFF.

## Implemented behavior

- `StrictWorkflowVariableStore` accepts an existing run directory and allows
  unrelated files such as input snapshots to remain in that directory.
- Initialization validates caller metadata, refuses any pre-existing
  variables.sqlite or related WAL/SHM file, and creates four tables and all
  five named indexes from ADR 0004 §4. This includes the declared CHECK,
  composite foreign key, primary key, partial unique root index, and unique
  constraints for variable names and mutation versions.
- Foreign keys, WAL, FULL synchronous mode, and a 10-second busy timeout are
  configured before one `BEGIN IMMEDIATE` transaction. Schema creation,
  RunBinding, the deterministic `workflow-root` frame, and `user_version=1`
  are validated and committed together.
- The root frame has no parent, scope path `workflow`, iteration vector `[]`,
  ACTIVE status, no close timestamp, and the same UTC timestamp as RunBinding.
- Private fault checkpoints cover schema creation, post-binding, post-root,
  and pre-commit rollback. Failed initialization removes only the newly
  created database artifacts; a pre-existing run-directory snapshot remains.
- `inspect` opens SQLite with `mode=ro` and `query_only`, checks exact schema
  objects/columns/keys/indexes, `user_version`, `integrity_check`, and
  `foreign_key_check`, then validates caller-expected identity and the initial
  root state. Missing databases are not created.
- Caller-provided binding metadata is only an identity candidate. This slice
  does not prove it against definitions.sqlite, requests.sqlite, a manifest,
  or an input snapshot. No secrets, input values, or contract bodies are
  stored.

SQLite read-only inspection can still cause SQLite to create or adjust WAL
sidecar metadata while reading an existing WAL database. Tests compare the
database rows and initial state across inspection rather than treating
sidecar-only changes as application-data writes.

## Verification

The focused pytest invocation covered the new I4-A tests, I3-A/B/C, I1, I0,
and focused legacy definition/request paths:

```text
274 passed, 4 skipped, 0 failed
```

All four skips were Windows symlink-creation tests skipped because the current
Windows account lacks `SeCreateSymbolicLinkPrivilege` (WinError 1314). The two
new I4-A symlink cases were skipped for the same reason. The schema tests also
exercise SQLite CHECK/FK/unique constraints directly, integrity and foreign
key checks, identity tampering, unknown schema objects, transactional fault
rollback, and read-only missing-database behavior.

| Check | Result |
| --- | --- |
| `uv run --no-sync ruff check src/codex_bridge tests/test_workflow_d1_i4a_variable_schema.py` | PASS |
| `uv run --no-sync ruff format --check src/codex_bridge/workflows/strict_variables.py tests/test_workflow_d1_i4a_variable_schema.py` | PASS |
| `uv run --no-sync mypy src/codex_bridge` | PASS |
| `uv run --no-sync python -m compileall -q src/codex_bridge` | PASS |
| `git diff --check` | PASS |
| Full pytest suite | NOT_RUN |
| Live Bridge/Worker, Workflow POST, DBOS checkpoint, or production/shared database | NOT_RUN |

Existing legacy implementation files were not changed. The five user-owned
untracked workspace entries were preserved. The implementation and evidence
still require ChatGPT GitHub review; no I4-B/C acceptance is implied.
