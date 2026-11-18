# D1-I3-A Isolated Contract Storage Evidence

**Status:** ACCEPTED (with documented caveats) — ChatGPT GitHub review 2026-11-19
**Date:** 2026-11-19
**Branch base:** d1f80d0f7de88016e14ec531c3f426300fc2a2ae
**Scope:** definitions.sqlite v1→v2 migration, opt-in v2 initialization, revision-bound contract storage/read validation, and internal strict/legacy CRUD core.

## Implemented surface

The new module strict_definitions.py contains StrictWorkflowDefinitionStore. It is not constructed or called by the existing GUI, HTTP API, MCP server, service, manager, worker, DBOS, OWS, or legacy WorkflowDefinitionRepository.

Its explicit operations are:

- initialize_new_database() creates an empty v2 definitions database only when called.
- migrate_v1_to_v2(backup_path=...) validates a v1 database, creates and verifies an online backup, then performs the migration in one BEGIN IMMEDIATE transaction.
- create_verified_backup() and restore_verified_backup() use SQLite's online backup API and verify isolated v1 snapshots.
- create(..., contract=None) stores a legacy revision; a non-null contract stores a strict revision.
- update(..., contract=...) adds or updates strict revisions. A strict latest revision rejects omitted or null contract input. A legacy revision may be upgraded by explicitly supplying a contract.
- get() verifies the source revision, canonical contract JSON, contract SHA-256, binding digest, and strict ETag.

Strict create/update writes the definition row, immutable revision, optional contract row, and mutation ledger in one definitions.sqlite transaction. Legacy mutations retain their existing digest and v1 ETag calculation. Strict mutations use a separate domain and include mode, canonical contract, operation, definition identity, expected revision, YAML, and job limit. Strict ETags use the ADR 0006 domain-separated v2 payload. Removing a strict contract row is detected from its v2 mutation identity and fails closed.

The v1-to-v2 migration checks exact SQLite object inventory, table definitions, table_info, primary-key indexes, foreign keys, user_version, integrity_check, foreign_key_check, revision hashes/ETags, and mutation ledger identities before and after migration. Three private migration checkpoints cover rollback after table creation, after user_version update, and before commit. No public fault-injection API was added.

The existing definitions.py and contracts.py behavior was not changed. WorkflowDefinitionRepository continues to require schema v1 and does not initialize or migrate v2. Strict run admission remains OFF. I3-B requests.sqlite migration and I3-C cross-database integration were not implemented.

## Review caveat

The isolated strict_definitions.py module is approximately 1,500 lines. When connecting I3-B and I3-C, reuse its schema, validation, and transaction responsibilities to avoid unnecessary duplicate logic. This is a maintainability caveat for follow-up work; this docs-only review record makes no refactoring or implementation changes.

## Isolated validation

All test databases were created under pytest tmp_path. No production database, shared state root, running Bridge/Worker, or real Codex job was used.

| Check | Result |
| --- | --- |
| pytest tests/test_workflow_d1_i3a_contract_storage.py | PASS — 24 passed |
| pytest tests/test_workflow_definitions.py tests/test_workflow_d1_i0_baseline.py tests/test_workflow_d1_i1_contract_core.py tests/test_workflow_definition_api.py tests/test_workflow_definition_mcp.py | PASS — 87 passed, 2 skipped |
| Combined requested tests | PASS — 111 passed, 2 skipped, 0 failed |
| Ruff check src/codex_bridge tests/test_workflow_d1_i3a_contract_storage.py | PASS |
| Ruff format --check src/codex_bridge tests/test_workflow_d1_i3a_contract_storage.py | PASS — 136 files already formatted |
| mypy src/codex_bridge | PASS — 135 source files, no issues |
| compileall src/codex_bridge | PASS |
| git diff --check | PASS |
| Full product pytest | NOT_RUN |
| Ruff check over the entire workspace | FAIL — 95 findings were reported; output included protected untracked staging/tmp assets. Those assets were not changed. The scoped Ruff check above passed. |
| Ruff format --check over the entire workspace | NOT_RUN |

The two skipped legacy definition tests require Windows symlink creation and were skipped by the existing test code with WinError 1314. They are not counted as passes.

## State and review boundary

D1-I3-A is ACCEPTED (with documented caveats) following ChatGPT GitHub review on 2026-11-19. I3 remains IN_PROGRESS: I3-B requests.sqlite migration is next, and I3-C cross-database integration follows. I0/I1 remain ACCEPTED, I2 remains DEFERRED / NON-BLOCKING, and D1 remains DRAFT. Production routes remain unconnected and no production/shared database was changed.

This review-record update was docs-only. Pytest, Ruff, mypy, and compileall were NOT_RUN in this turn; the isolated implementation results above are retained unchanged.
