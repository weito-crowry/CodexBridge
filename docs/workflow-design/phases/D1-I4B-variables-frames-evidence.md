# D1-I4-B: Typed variable candidates and scope frames evidence

- **Status:** IMPLEMENTED / PENDING_CHATGPT_REVIEW
- **Date:** 2026-11-19
- **Slice state:** I4-B only; I4 overall remains IN_PROGRESS.
- **D1 state:** DRAFT; strict run admission remains OFF.

## Implemented boundary

- `strict_variable_logic.py` resolves declarations from the canonical
  `CompiledContract` snapshot and calculates candidates for initialize,
  assign, increment, decrement, append, object_set, object_delete, and
  variable_delete.
- Candidate calculation is pure. It does not open SQLite and does not write
  `variable_states` or `variable_mutations`. Missing rows, present JSON null,
  and tombstones remain distinct through explicit state arguments and
  `state_exists` / `present` result fields.
- `strict_variable_frames.py` attaches only to an existing I4-A run-local
  `variables.sqlite`. It persists deterministic ScopeFrame enter/close
  lifecycle operations and provides a read-only complete frame inspector.
- Frame write transactions use the existing schema, connection, integrity,
  foreign-key, timestamp, and RunBinding validators. Shared validation rejects
  nonempty state or mutation tables for enter, close, and inspect.
- No runtime, Worker, Workflow POST, Codex job, shared/production database,
  schema migration, or production state root was used.

## Verification results

| Check | Result |
| --- | --- |
| New I4-B tests | PASS — 28 passed |
| Focused I0/I1/I3-A/I3-B/I3-C/I4-A/I4-B regression | PASS — 231 passed; SKIP — 2 existing Windows symlink privilege cases |
| `ruff check src/codex_bridge tests/test_workflow_d1_i4b_variable_logic.py tests/test_workflow_d1_i4b_frames.py` | PASS |
| `ruff format --check` on the two new modules and two new tests | PASS |
| `mypy src/codex_bridge` | PASS — 140 source files |
| `python -m compileall -q src/codex_bridge` | PASS |
| `git diff --check` | PASS |
| Full test suite | NOT_RUN |
| Runtime / DBOS / live service acceptance | NOT_RUN |

The two skips are the existing I4-A symlink tests at lines 701 and 719; they
require the Windows `SeCreateSymbolicLinkPrivilege`. The first attempt to
populate 1024 frames through repeated full validation was stopped after it
exceeded 30 seconds. It was replaced with a boundary test that monkeypatches
`_MAX_FRAMES` to 5 only inside the test; the production limit remains 1024.
The final focused regression completed in 179.20 seconds.

## Review caveats and next slice

- I1 remains the authority for declaration and JSON Schema validation. This
  slice reuses its compiler, canonical JSON, JSON tree limits, and validator;
  it does not add a second Schema implementation.
- Pure candidate calculation cannot establish that a caller's frame is truly
  ACTIVE. I4-C must verify the RunBinding, exact frame identity, and ACTIVE
  status inside the transaction that applies the state and ledger rows.
- RunBinding comparison verifies equality with caller-supplied expected
  metadata; it does not prove the source or truth of those caller claims.
- Scope paths are validated as explicit parent/child metadata. Their mapping
  to future I7 OWS nodes remains to be confirmed at that adapter boundary.
- This slice does not write variable state or mutation ledger rows and does not
  implement execution keys, CAS/version checks, or historical replay. Those
  are I4-C work.
- No atomicity across variables.sqlite, requests.sqlite, dbos.sqlite, or DBOS
  checkpoints is claimed.

## Current phase state

I4-A is ACCEPTED (with documented caveats); I4-B is IMPLEMENTED /
PENDING_CHATGPT_REVIEW; I4-C is NOT_STARTED. I4 overall is IN_PROGRESS. I5-I8
remain NOT_STARTED. D1 remains DRAFT and strict run admission remains OFF.
