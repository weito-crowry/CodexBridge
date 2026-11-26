# D1-I8-B final isolated acceptance cleanup and go/no-go evidence

- **実装状態:** ACCEPTED with documented caveats by ChatGPT review of `679df0a42929162f9bc7ee01b0e66e0b6db5ebe5`。
- **対象branch:** `codex/workflow-d1-i5-b4-verified-readers`。
- **base:** `afda1d9d05ee126ec1ceb5d81bab9b6a5879ac7f` (I8-A accepted with documented caveats)。
- **範囲:** UI server test-doubleの3 failure修正、D1-22 mock child output contract境界、PD-01..39 traceability、I8 final isolated gateと次段operator rollout計画。
- **全体境界:** I7-C2/I7、I8-A、I8-BはACCEPTED with documented caveats。D1 current development scopeはACCEPTED / isolated GOです。D1詳細設計の文書状態はformal production approval前のDRAFTを維持します。operational migrationとproduction enablementは別途明示承認待ち、I2はDEFERRED / NON-BLOCKING、public/production strict admissionはOFFです。

## Review記録

ChatGPT GitHub review of `afda1d9d05ee126ec1ceb5d81bab9b6a5879ac7f` はI7-C2/I7をACCEPTED with documented caveatsとし、profile-1 identity、pending Codex job reconciliation、ownership照合、およびisolated worker process-death recoveryを含むI8-A integrationも受け入れました。Full suiteは3,955 passed / 24 skipped / 3 failedであり、all-greenではありませんでした。3件は未変更production `server.py`の`drain_final_answer_writes(close=True)`呼出しと`tests/test_ui_server.py`のFakeBridgeの欠落によるtest-only mismatchでした。baselineを実行して同じfailureだったとは主張しません。

I8-Bではレビュー指定どおりtest doubleだけを修正し、production runtimeを変更していません。この証跡の初版作成時点ではChatGPT review待ちでした。後続のreview結果と現在の受入状態を次項に記録します。

ChatGPT GitHub review of `679df0a42929162f9bc7ee01b0e66e0b6db5ebe5` はI8-BおよびI8 isolated acceptanceをACCEPTED with documented caveatsとしました。D1 current development scopeもACCEPTED / isolated GOです。これはproduction acceptanceではなく、operational migrationとproduction enablementは別途明示的なユーザー承認待ちです。review留保はこの文書の全体suite、D1-22、PD crosswalk、rollout節に整理しています。

## UI server test-double修正

`tests/test_ui_server.py`のFakeBridgeに`drain_final_answer_writes(*, close: bool)`を追加し、close引数とeventを記録します。live SSE shutdown、通常runtime shutdown、UI startup failureの3ケースで、既存のapp/UI/Bridge orderとSSE/exception assertionsを維持し、`app.shutdown`の後にdrainが一度呼ばれ`close=True`であることを検証します。

## D1-22 mock child return-contract範囲

D1詳細設計の原文は「子Workflow return境界の契約模擬: child outputをSchema検証し、明示returnのみ親へ渡す」です。`tests/test_workflow_d1_i1_contract_core.py::test_d1_22_mock_child_output_is_validated_at_contract_boundary`は既存`compile_contract` / `validate_output`を使い、mock childのvalid outputを受け入れ、Schema-invalid outputを`ContractOutputInvalid`として拒否します。

現行runtimeにはsubflow childを呼び出す境界、explicit return mapping、親contextへのhandoff primitiveがありません。このtestはcontract validation半分のみです。親へexplicit returnだけを渡す意味はD6実subflow実装時の残件として明示し、D1-22全体のPASSとは扱いません。別のtest-only return semanticsやsubflow実装は追加していません。

## PD-01..39 crosswalk

この表は各Proposed Decisionを既存test pathsへ紐づけ、現行実装test、非実施のoperator gate、DEFERRED項目を分けたものです。suite実行は各PDの個別設計承認ではありません。

| PD IDs | Existing evidence paths / cases | I8-B status |
| --- | --- | --- |
| 01..03 | `test_workflow_d1_i3a_contract_storage.py`, `test_workflow_d1_i3c_binding.py`, `test_workflow_d1_i6a_definition_repository_http.py`, `test_workflow_d1_i6b_mcp_repository_selection.py` — contract row, digest/binding, legacy-to-strict and mutation replay | 現行隔離CRUD/binding/replay testsを実行 |
| 04..05 | `test_workflow_d1_i1_contract_core.py` — schema dialect, allowed references, `additionalProperties`, defaults and normalization | 現行pure contract testsを実行 |
| 06..10 | `test_workflow_d1_i1_contract_core.py`, `test_workflow_d1_i4b_variable_logic.py`, `test_workflow_d1_i4c_mutations.py`, `test_workflow_d1_i7a_variable_call_validation.py`, `test_workflow_d1_i7b_variable_runtime.py` — declaration/scope/operation, execution identity, secret rejection | 現行validator/runtime/cap testsを実行 |
| 11..17 | `test_workflow_d1_i4a_variable_schema.py`, `test_workflow_d1_i4c_mutations.py`, `test_workflow_d1_i5a_manifest.py`, `test_workflow_d1_i5b4a_verified_readers.py`, `test_workflow_d1_i5b4c_reserved_readers.py`, `test_workflow_d1_i7c1_durable_execution.py`, `test_workflow_d1_i7c2_isolated_runtime.py` — store binding, reservation, replay and uncertainty | 現行isolated testsを実行。複数DB atomicityは主張しない |
| 18..22 | `test_workflow_d1_i3a_contract_storage.py`, `test_workflow_d1_i3b_request_storage.py`, `test_workflow_d1_i4a_variable_schema.py`, `test_workflow_d1_i1_contract_core.py::test_d1_22_mock_child_output_is_validated_at_contract_boundary` — isolated migration/schema checks and mock child output validation | migration/schema testsを実行。D1-22 parent return handoffはD6へdefer |
| 23 | `docs/workflow-design/decisions/0004-sqlite-ddl-and-schema-migration.md` §11 and §§15-16; isolated backup/migration tests above | future shared DB operator gate。実writer/worker stop、shared backup/restore、production migrationは未実施 |
| 24..27 | `test_workflow_d1_i2_windows_guard.py` covers only the current non-Windows gate condition | I2 Global Mutex, old-writer/controller exclusion, explicit DACL/ACE/SID and handle/TOCTOU ideas remain DEFERRED / NON-BLOCKING; no new I2 protection was added |
| 28..29 | `test_workflow_d1_i1_contract_core.py`, `test_workflow_d1_i4c_mutations.py`, `test_workflow_d1_i7b_variable_runtime.py` — existing configured caps and rollback before mutation | existing logical caps are tested. Hard physical WAL/SHM byte guarantee and WAL metadata invariance remain DEFERRED / NOT_TESTED |
| 30..34 | `test_workflow_d1_i6a_definition_repository_http.py`, `test_workflow_d1_i6b_mcp_repository_selection.py`, `test_workflow_definition_api.py`, `test_workflow_definition_mcp.py` — contract wire, validate/create/update, ETag and legacy field sets | 現行HTTP/MCP regression testsを実行 |
| 35..38 | `test_workflow_d1_i6c_saved_run_contract.py`, `test_workflow_d1_i7c2_isolated_runtime.py`, `test_workflow_d1_i8a_integration.py`, `test_workflows_api.py`, `test_workflow_mcp.py` — normalized saved-run replay, output/result/status, projections, permissions and caps | 現行HTTP/MCP/isolated worker testsを実行。task/event projectionはstatic partial/emptyのまま |
| 39 | `test_workflow_d1_i7a_variable_call_validation.py`, `test_workflow_d1_i7b_variable_runtime.py`, `test_workflow_d1_i7c1_durable_execution.py`, `test_workflow_d1_i7c2_isolated_runtime.py` — strict variable call allowlist/ledger/DBOS replay | 現行strict-only validator/runtime testsを実行 |

PD-23の隔離DB testは将来の共有DB運用承認を代替しません。PD-24..27とPD-29のphysical WAL/SHM要件は明示的にdeferredで、I8 blockerではありません。D1-22のexplicit parent handoffもD6へ残します。

## 全体suiteとcheck結果

| 検査 | 結果 |
| --- | --- |
| `tests/test_ui_server.py` | 9 passed。3 drain callは`app.shutdown`の後、`close=True` |
| `test_d1_22_mock_child_output_is_validated_at_contract_boundary` | 1 passed。child output Schema boundaryのみ |
| Full `uv run --no-sync pytest` | 3,959 passed / 24 skipped / 0 failed、3,983 collected (Python 3.14.10 / Windows native、54:38) |
| Skips | 24。ほとんどはWindows symlink privilege unavailable。追加で既存I2 non-Windows fail-closed pathはWindows上でskip |
| CPP/account-auth | `test_cpp_mcp_auth.py`とaccount-auth suitesを含めfull pytest内でpass。live auth/serviceは未実施 |
| `uv run --no-sync ruff check tests/test_ui_server.py tests/test_workflow_d1_i1_contract_core.py` | PASS |
| `uv run --no-sync ruff format --check tests/test_ui_server.py tests/test_workflow_d1_i1_contract_core.py` | PASS — 2 files already formatted |
| mypy `src/codex_bridge` | PASS — 156 source files |
| `python -m compileall -q src/codex_bridge` | PASS |
| `git diff --check` | PASS |

## 次段のoperator rollout plan（未実施）

本sliceでは実state rootやproduction/shared DBを照会・変更しません。将来のmigration/enablementは別途ユーザー承認後に行います。

1. 実state rootを使う前に、SQLite read-only接続（`mode=ro`）で`definitions.sqlite`と`requests.sqlite`の`PRAGMA user_version`、tables/indexes、`integrity_check`、`foreign_key_check`を記録します。停止前は予備preflightに留め、known writers/workersを止めた後に同じread-only inventoryを再確認します。既知schema/version以外は移行せず停止します。
2. Bridge、全Workflow worker、その他known writersの停止を確認します。DB lockや`user_version`だけで旧writer停止の証明とは扱いません。停止中の一組としてdefinitions/requestsのSQLite online backupと関連run manifest metadataを採取し、backup側でschema、integrity/FK、legacy revisions/ETags、request/source identityを照合します。
3. 承認された場合にのみ既存explicit facilitiesを使います。definitions v1→v2は`StrictWorkflowDefinitionStore.migrate_v1_to_v2(backup_path=...)`、requests legacy v0→v1は`StrictWorkflowRequestStore.migrate_v0_to_v1(backup_path=...)`です。各DB内transactionは独立です。片方だけ成功/失敗した場合はstrict runを許可せず状態を調査し、自動downgradeしません。既にtarget versionのDBは再migrationしません。
4. migration後に両DBのversion/object inventory、integrity/FK、legacy revision/hash/ETagとrequest/source identitiesを再検査します。run-local variables DB/manifest/binding/reader互換も検証します。server wiringおよびstrict enablementは既定OFFを保ち、実施する場合は別途ユーザー承認を受けます。
5. strict dataを書き込む前のrollback候補は、全writer停止中に対応する検証済みpaired backup setへoffline restoreする手順です。strict writes後に旧schemaへ自動downgradeしません。以後はforward repair、またはdata-loss範囲を明示した別途承認済みoffline restoreを検討します。

## Isolated go/no-go

ChatGPT reviewはI8-BおよびI8 isolated acceptanceをACCEPTED with documented caveatsとし、D1 current development scopeをACCEPTED / isolated GOとしました。full pytest 3,959/24/0、UI server 9/9、D1-22 schema mock 1/1、Ruff、mypy、compileall、diff checkが根拠です。この判定はproduction/public strict admissionの承認ではありません。D1詳細設計はDRAFT、public admission OFF、I2 DEFERREDのままです。PD-23 operator gate、live Bridge/API、real external Codex job、production/shared migration、hard WAL/SHM guarantee、D1-22 parent return routingは未実施または後続sliceです。

## 変更path

- `tests/test_ui_server.py`
- `tests/test_workflow_d1_i1_contract_core.py`
- `docs/workflow-design/phases/D1-I8A-integration-acceptance-evidence.md`
- `docs/workflow-design/phases/D1-I7C2-isolated-runtime-evidence.md`
- `docs/workflow-design/phases/D1-implementation-plan.md`
- `docs/workflow-design/phases/D1-I8B-final-acceptance-evidence.md`
