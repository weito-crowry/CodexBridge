# D1-I8-A integration verification and targeted repair evidence

- **実装状態:** ACCEPTED with documented caveats by ChatGPT review of `afda1d9d05ee126ec1ceb5d81bab9b6a5879ac7f`。後続のI8-B cleanupは別証跡でreview待ちです。
- **対象branch:** `codex/workflow-d1-i5-b4-verified-readers`。
- **開始HEAD:** `98ec2f85e71f28d6eda5c79856bf345193b9f8b5`。
- **範囲:** I7-C2 review follow-up、strict profile task identity/terminal mapping、pinned task identityによるshutdown ownership、isolated HTTP/MCP/DBOS統合、in-flight worker process-death recovery。
- **全体境界:** I7-C2/I7とI8-AはChatGPT reviewでACCEPTED with documented caveatsです。I8-BとI8全体acceptance/go-no-goは未完了です。D1はDRAFT、I2はDEFERRED / NON-BLOCKING、public strict admissionはOFFです。

## Review記録と対象修正

ChatGPT GitHub review of `98ec2f85e71f28d6eda5c79856bf345193b9f8b5` はI7-C2のisolated admission/worker/DBOS coreを受け入れ、具体修正を残してI7最終受入を保留しました。指摘のprofile-1 ledger identity不一致を修正し、同時に未確定Codex job状態をterminal failureと誤認しないことを確認しました。

ChatGPT GitHub review of `afda1d9d05ee126ec1ceb5d81bab9b6a5879ac7f` はI7-C2/I7とI8-A workflow integrationをACCEPTED with documented caveatsとしました。profile-1 identity fix、pending job reconciliation、pinned task ownership、実isolated worker process-death recoveryを含みます。full suiteは3,955 passed / 24 skipped / 3 failedでありall-greenではありませんでした。I8-Bでは指定されたUI server test-double mismatchをtest-onlyで修正しました。修正後の最終suiteは[I8-B evidence](D1-I8B-final-acceptance-evidence.md)に記録し、I8全体の最終受入はpendingです。

profile-1のstatus readerはgraphのJSON Pointer node IDではなくInterpreterと同じ`do[0].scope.do[0].task` identityを期待します。profile-2/3も実行側のtask pathと、profile-3のnormalized OWS input上のenabled item順に一致するshared helperを使います。Codex shutdown ownership readerも、strict reservation/pinned definitionから計算したtask identityをworkflow ID/request keyと合わせて照合します。pinned taskにないledger rowはturn ownershipへ追加せず、ownershipをincompleteとして報告します。legacy reader呼出しは従来引数のままです。

strict failure terminalはDBOS failureだけで外部jobを失敗確定しません。`PREPARED` / `ACCEPTED` / `RUNNING` / `MAYBE_SENT`など未settled ledger stateは既存のrunning/reconciliation経路に残り、実行済みjobが全て`SUCCEEDED`または`FAILED`で、worker/DBOSもfailureを示す場合だけfailureを投影します。

I7-C2の過去の集約値は **333 passed / 2 skippedを複数suiteの合計として報告**したもので、単一pytest再実行結果ではありません。元の内訳はC2専用12 passed、I4-C/I7-B 39 passed、I5/I6-C/I7-C1 52 passed、manager/entry/Codex job/service 122 passed / 1 skipped、HTTP/MCP/task/saved-run 108 passed / 1 skippedです。レビュー時点ではfull pytest、CPP/account-auth、worker subprocess経由のCodex job、in-flight process-death recovery、WAL metadataは未検証でした。C1のreview値706/10/0も別suiteの結果であり、C2 aggregateと合算していません。

## Isolated integration

[`tests/test_workflow_d1_i8a_integration.py`](../../../tests/test_workflow_d1_i8a_integration.py)はWindows native `tmp_path`上のDBだけを使います。

- `test_isolated_strict_saved_run_http_and_mcp_replay_use_one_worker_and_one_fake_post`はHTTP saved-run startから実service、manager subprocess、DBOS workerへ接続し、loopback fake Codex APIを使います。HTTP detail/list/result、MCP同一request replay/get/result/list、output schema違反のHTTP/MCP結果を確認し、replayによるworker再起動やfake POST重複がないことを確認します。成功とinvalid outputを含めfake POSTは2件で、実Codex APIへの送信はありません。
- 同じintegration caseに、strict definition v2/requests v1のprepared DBでlegacy saved-runをstart/replayし、HTTP/MCP resultが従来の同一payloadでstrict metadataを含まないことを追加しました。この追加後のcaseをfull pytest後に単独再実行し、`1 passed`を確認しました。全体pytestのassertion時点にはこの追加確認は含まれません。
- `test_strict_worker_recovers_post_commit_process_death_before_dbos_checkpoint`はworker subprocess内だけでI4 mutation commit直後・DBOS apply checkpoint前に停止markerを出し、その子processだけを終了して同一workflow IDでrecoveryします。I4 historical replay後の値は`[0, 1, 2]`、終端resultは`{"count": 2}`で、二重適用や新しいrunはありません。
- `test_service_job_identities_match_interpreter_paths_and_profile_three_input_order`はprofile-1 nested `do`、profile-2、profile-3のtask identity/skip順を実行側規則と比較します。
- `test_strict_terminal_failure_with_pending_codex_job_remains_reconciliation`はpending job stateがDBOS failureを通常terminal failureへ変えないことを確認します。

実worker process death/recoveryはisolatedなmanager/worker/DBOS経路で実行しました。外部Codex jobはfake loopback transportだけで、production/shared DB、production migration、live Bridge、live API、real external Codexは使っていません。既定/public server compositionとadmission flagは変更していません。

## D1-01..156 evidence crosswalk

次表は既存D1 IDを実在するtest paths/casesへ結ぶtraceabilityです。範囲内の全ケースが個別に再監査・再承認されたという意味ではありません。full pytestで各suiteは実行されましたが、未実施gate、DEFERRED項目、失敗suiteは下に区別しています。

| D1 IDs | Test path / case mapping | I8-A記録 |
| --- | --- | --- |
| 01..06 | `test_workflow_d1_i1_contract_core.py` — schema dialect/type/required/null/default/additionalProperties/enum | Full suite実行。契約coreの個別再承認ではない |
| 07..08 | `test_workflow_d1_i3b_request_storage.py`, `test_workflow_d1_i5a_manifest.py`, `test_workflow_d1_i6c_saved_run_contract.py` — normalized identity/replay | Full suite実行。新HTTP/MCP replayはI8-A統合testで確認 |
| 09..10 | `test_workflow_d1_i0_baseline.py`, `test_workflow_definitions.py`, `test_workflow_d1_i6a_definition_repository_http.py` — legacy golden/immutable revision | Full suite実行。旧responseの個別再承認ではない |
| 11..16 | `test_workflow_d1_i4b_variable_logic.py`, `test_workflow_d1_i4c_mutations.py`, `test_workflow_d1_i7a_variable_call_validation.py`, `test_workflow_d1_i7b_variable_runtime.py` — declarations/operations/execution key | Full suite実行 |
| 17..21 | `test_workflow_d1_i4c_mutations.py`, `test_workflow_d1_i7c1_durable_execution.py`, `test_workflow_d1_i8a_integration.py::test_strict_worker_recovers_post_commit_process_death_before_dbos_checkpoint` — CAS/replay/crash | isolated DBOS process-death recovery確認。cross-database atomicityは主張しない |
| 22 | `test_workflow_d1_i1_contract_core.py::test_d1_22_mock_child_output_is_validated_at_contract_boundary` — mock child outputを既存`validate_output`で検証 | child output schema境界のみを検証。parentへのexplicit return routingはsubflow primitiveがないためD6へdeferし、D1-22全体をPASSとはしない |
| 23 | `test_workflow_d1_i6c_saved_run_contract.py`, `test_workflow_d1_i7c1_durable_execution.py`, `test_workflow_d1_i7c2_isolated_runtime.py`, `test_workflow_d1_i8a_integration.py` — output validation/result | valid resultとinvalid outputのHTTP/MCP 409を確認 |
| 24..26 | `test_workflow_d1_i1_contract_core.py`, `test_workflow_d1_i6a_definition_repository_http.py`, `test_workflow_d1_i6b_mcp_repository_selection.py` — secret/ref/resource caps | Full suite実行。hard WAL quotaは115で別扱い |
| 27 | `test_workflow_d1_i7b_variable_runtime.py`, `test_workflows_api.py`, `test_workflows_multi_api.py`, `test_workflow_saved_execution.py` — OWS/legacy behavior | Full suite実行 |
| 28..30 | `test_workflow_d1_i3a_contract_storage.py`, `test_workflow_d1_i3c_binding.py`, `test_workflow_d1_i6a_definition_repository_http.py`, `test_workflow_d1_i6b_mcp_repository_selection.py` — contract row/binding/CRUD | Full suite実行 |
| 31..37 | `test_workflow_d1_i1_contract_core.py`, `test_workflow_d1_i3a_contract_storage.py`, `test_workflow_d1_i6a_definition_repository_http.py`, `test_workflow_d1_i6b_mcp_repository_selection.py` — strict/legacy/ETag/normalization | Full suite実行 |
| 38..41 | `test_workflow_d1_i4b_variable_logic.py`, `test_workflow_d1_i4c_mutations.py`, `test_workflow_d1_i7a_variable_call_validation.py`, `test_workflow_d1_i7b_variable_runtime.py` — scope/object mutation/secret | Full suite実行 |
| 42..50 | `test_workflow_d1_i0_baseline.py`, `test_workflow_d1_i3a_contract_storage.py`, `test_workflow_d1_i3b_request_storage.py`, `test_workflow_d1_i3c_binding.py`, `test_workflow_d1_i5a_manifest.py`, `test_workflow_d1_i5b4a_verified_readers.py` — isolated migration/reservation/binding | Full suite実行。shared DB operator migrationは未実施 |
| 51..62 | `test_workflow_d1_i4a_variable_schema.py`, `test_workflow_d1_i4c_mutations.py`, `test_workflow_d1_i5b4a_verified_readers.py`, `test_workflow_d1_i5b4c_reserved_readers.py`, `test_workflow_d1_i7b_variable_runtime.py`, `test_workflow_d1_i7c1_durable_execution.py` — missing/corrupt store, replay, reconciliation | Full suite実行 |
| 63..65 | `test_workflow_d1_i3a_contract_storage.py::test_online_backup_can_be_restored_into_a_new_isolated_root`, `test_workflow_d1_i3b_request_storage.py::test_wal_online_backup_restore_and_v0_migration_preserve_legacy_identity` | isolated backup/restore/migration testを実行。運用backup手順やshared DB restore承認ではない |
| 66..78 | `test_workflow_d1_i3a_contract_storage.py`, `test_workflow_d1_i3b_request_storage.py` — backup/migration/rollback/checkpoint failure | Full suite実行。legacy binary/operator gateは別途 |
| 79..92 | `test_workflow_d1_i3a_contract_storage.py`, `test_workflow_d1_i3b_request_storage.py`, `test_workflow_d1_i3c_binding.py`, `test_workflow_d1_i4a_variable_schema.py`, `test_workflow_d1_i4c_mutations.py` — schema/binding/ledger/transaction | Full suite実行。WAL metadata不変性は主張しない |
| 93..107 | I2 history; `test_workflow_d1_i2_windows_guard.py`は現行の非Windows gate条件のみ | DEFERRED / NON-BLOCKING。Global Mutex/RDP/ACL/ACE/SID実装を追加せず、現行gateから除外 |
| 108..120 | `test_workflow_d1_i1_contract_core.py`, `test_workflow_d1_i4c_mutations.py`, `test_workflow_d1_i6a_definition_repository_http.py`, `test_workflow_d1_i8a_integration.py` — existing caps/privacy/rollback | 通常のresource/privacy回帰を実行。D1-115 hard WAL/SHM byte guaranteeはDEFERRED |
| 121..132 | `test_workflow_d1_i6a_definition_repository_http.py`, `test_workflow_d1_i6b_mcp_repository_selection.py`, `test_workflow_definition_api.py`, `test_workflow_definition_mcp.py` — HTTP/MCP contract/CRUD/ETag/legacy | Full suite実行 |
| 133..138 | `test_workflow_d1_i5b4b_verified_projection.py`, `test_workflow_d1_i6c_saved_run_contract.py`, `test_workflow_d1_i7c1_durable_execution.py`, `test_workflow_d1_i7c2_isolated_runtime.py`, I8-A HTTP/MCP integration | isolated start/replay/result/output-invalid/process-death/status確認 |
| 139..146 | `test_workflow_d1_i6a_definition_repository_http.py`, `test_workflow_d1_i6b_mcp_repository_selection.py`, `test_workflow_d1_i6c_saved_run_contract.py`, `test_workflow_definition_api.py`, `test_workflow_definition_mcp.py`, `test_workflow_saved_api.py`, `test_workflow_saved_mcp.py`, `test_workflow_task_api.py`, `test_workflow_task_mcp.py` | Full suite実行。CPP/account-auth regressionも全体suite内でpass |
| 147..156 | `test_workflow_d1_i7a_variable_call_validation.py`, `test_workflow_d1_i7b_variable_runtime.py`, `test_workflow_d1_i7c1_durable_execution.py`, `test_workflow_d1_i7c2_isolated_runtime.py`, `test_workflow_d1_i8a_integration.py` | strict validator/runtime/replay regressionを含むFull suite実行 |

## 検証結果

| 検査 | 結果 |
| --- | --- |
| I8-A suite `test_workflow_d1_i8a_integration.py` | 7 passed (Windows native, loopback fake API, isolated DBOS/worker) |
| Full suite後に拡張したHTTP/MCP+prepared-v2 legacy integration case | 1 passed。実HTTP saved-runの起動/完了、legacy result wireのHTTP/MCP一致、strict metadata不在を確認 |
| 修正後identity/status/ownership targeted tests | 9 passed |
| 初期cross-slice focused aggregate | 567 passed / 6 skipped / 3 failed。3 failureはC2後のstatus/artifact semanticsとshutdown ownership期待が古いI5 reader testsと判明し、該当期待を更新。更新後の対象testは上行でpass |
| Full `uv run --no-sync pytest` | 3,955 passed / 24 skipped / 3 failed、3,982 collected (Python 3.14.10 / Windows native) |
| Full suiteの3 failure | `tests/test_ui_server.py::test_control_shutdown_closes_live_sse_without_uvicorn_errors`, `::test_runtime_starts_ui_after_app_server_and_stops_it_before_app_server`, `::test_runtime_cleans_up_app_server_when_ui_start_fails`。tracebackはいずれも未変更`server.py` shutdown/start cleanupがtest double `FakeBridge`にない`drain_final_answer_writes`を呼ぶもの。今回変更path外。clean baselineでの再実行はしていないため「既存baseline」とは確定していない |
| CPP/account-auth | Full suite内の`test_cpp_mcp_auth.py`とaccount-auth suitesを含めpass。live authenticationは未実施 |
| Ruff check / Ruff format check（変更Python paths） | PASS |
| mypy `src/codex_bridge` | PASS — 156 source files |
| `python -m compileall -q src/codex_bridge` | PASS |
| `git diff --check` | PASS |

このfull pytestの3 failureは未変更production `server.py`のdrain呼出しとtest-only `FakeBridge`の不一致でした。このrun自体はall-greenではありません。I8-Bでtest doubleとshutdown event順序を修正し、その後のfull runはI8-B証跡に記録しました。

## 残件と明示的defer

- D1-93..107のI2 protectionsはDEFERRED / NON-BLOCKINGのままです。
- D1-115のhard WAL/SHM byte quota、WAL metadata不変性は検査していません。既存のoperator migration/backup/compatibility/legacy identity gateはpytestで置換せず、共有/production DBに触れていません。
- D1-22のchild output schema境界はI8-B mock testで確認します。explicit parent return routingと実subflowは現行runtimeにprimitiveがないためD6へdeferします。
- Static task projectionはpinned topologyのみ、task/event coverageはpartial、event listは空のままです。新しいruntime UI/event schemaは実装していません。
- Public strict admission OFF、server既定composition変更なし。production migration/enablement、live Bridge/API、real external Codex job、real DBOS remote behaviorは未実施です。
- D1全体の最終受入/go-no-goは未実施です。I8-B cleanupのreview後に残存gateを別途判断します。

## I8-B後続記録

I8-Bで`tests/test_ui_server.py`の`FakeBridge`に`drain_final_answer_writes(*, close: bool)`の記録を追加し、通常shutdownとUI startup failureの両方で`app.shutdown`後に`close=True`のdrainが呼ばれる順序をassertしました。production `server.py`は変更していません。3件はI8-A full run時の結果として保持し、baseline実行済みとは扱いません。

I8-B追加後の全体pytest・PD crosswalk・operator rollout/go-no-goは[D1-I8-B evidence](D1-I8B-final-acceptance-evidence.md)に記録します。

## 変更path

- `src/codex_bridge/workflows/codex_job_adapter.py`
- `src/codex_bridge/workflows/interpreter.py`
- `src/codex_bridge/workflows/manager.py`
- `src/codex_bridge/workflows/service.py`
- `tests/test_workflow_d1_i5b4b_verified_projection.py`
- `tests/test_workflow_d1_i5b4c_reserved_readers.py`
- `tests/test_workflow_d1_i7b_variable_runtime.py`
- `tests/test_workflow_d1_i7c2_isolated_runtime.py`
- `tests/test_workflow_d1_i8a_integration.py`
- `docs/workflow-design/phases/D1-I7C2-isolated-runtime-evidence.md`
- `docs/workflow-design/phases/D1-I8A-integration-acceptance-evidence.md`
- `docs/workflow-design/phases/D1-implementation-plan.md`
