# D1-I3-B request storage evidence

- **状態:** IMPLEMENTED / PENDING_CHATGPT_REVIEW
- **I3-B受入:** PENDING_CHATGPT_REVIEW
- **I3全体:** IN_PROGRESS。I3-Cは未着手。
- **既存状態:** I0/I1/I3-Aは受入済み、I2はDEFERRED、D1はDRAFT。
- **実装日:** 2026-11-19

## 範囲

`StrictWorkflowRequestStore` は `strict_requests.py` の内部向けopt-in APIです。既存runtimeからimport/constructせず、通常のopen/read/writeによる初期化やmigrationも行いません。明示APIは、新規v1 DB初期化、v0 online backup作成と検証済みrestore、v0→v1 migration、readonly inspect、strict request reservation、binding readです。

新規DB初期化は空のstate rootだけに許可します。readonly inspectは `mode=ro` を使い、欠落DBを作成しません。v0→v1ではSQLite online backup APIでbackupを作り、backup側のschema・integrity・foreign key・legacy row identityを検証します。その後 `BEGIN IMMEDIATE` 内でlive v0 schema/dataをbackupと再照合し、binding tableとuser_version=1を追加します。COMMIT前にv1 schema・data・legacy identityを再検証します。WAL中の主DBファイル単独コピーは使いません。

v0/v1検査は `user_version`、user schema object inventory、table_info、named/auto index構造、foreign keys、保存SQL/CHECK、`integrity_check`、`foreign_key_check` と全legacy rowを確認します。request/sourceのID・UUID・digest・state・UTC日時・source参照を検証し、legacy rowをrowid順のidentity digestでbackup前後・migration前後に照合します。v1の全bindingについてrequest/sourceとのrun/workflow/digest/definition/revision/version/日時整合性とlowercase SHA-256形式を確認します。

strict reservationは、事前正規化済みのID・digest・definition/source/contract/input hash・version・日時を受け、`BEGIN IMMEDIATE` 内でrequest/source/bindingの3行を一度に予約します。衝突・異payload retryは変更なしで拒否し、同一内容retryは保存済みreservationをreplayします。成功時もrun directory、manifest、worker、Codex jobは作成しません。legacy rowにはbindingを追加しません。bindingを伴わない既存rowをstrict read対象として指定した場合はfail closedです。

変更は指定された4ファイルのみです。`service.py`、既存runtime、共有/production DBは変更していません。definitions.sqliteとのcross-DB真正性検証、manifest v2、worker/reader/GUI/HTTP/MCP接続はI3-C以降です。複数DB atomicityや旧binaryとの共存は保証しません。

## テストと品質ゲート

新規fixtureは実際の `service._initialize_write_connection` でtmp_path内にlegacy v0 DBを作成し、service/worker runtimeは起動しません。WALにcommit済みdataがある状態でonline backup/restore、複数legacy row/source有無/state、index保持、migration後のrowid/data/digest/UUID保持を確認しました。

| 検証 | 結果 |
| --- | --- |
| 新規 `tests/test_workflow_d1_i3b_request_storage.py` | PASS 45 / FAIL 0 / SKIP 0 |
| I3-B + I0/I1/I3-A dedicated suites + 選定legacy service/request回帰 | PASS 124 / FAIL 0 / SKIP 0 |
| Ruff check (`src/codex_bridge` + 新規testのみ) | PASS |
| Ruff format-check (`src/codex_bridge` + 新規testのみ) | PASS |
| mypy (`src/codex_bridge`) | PASS、136 source files |
| compileall (`src/codex_bridge`) | PASS |
| `git diff --check` | PASS |
| full product pytest | NOT_RUN（指定focused suiteで検証） |
| 実環境MCP dogfood / 外部Codex job / production migration | NOT_RUN / scope-out |

Windows Nativeのpytest実行は `uv run --no-sync pytest -q -p no:cacheprovider` です。focused regressionにはI0の `tests/test_workflow_d1_i0_baseline.py::test_legacy_saved_run_manifest_binding_replay_and_restart`、I1 module全体、I3-A module全体、次のlegacy request系テストを含みます。

- `tests/test_workflows_service.py::test_same_request_id_reuses_durable_run_after_service_recreation`
- `tests/test_workflows_service.py::test_request_id_with_different_payload_conflicts_without_second_spawn`
- `tests/test_workflows_service.py::test_concurrent_identical_requests_start_exactly_one_worker`
- `tests/test_workflow_saved_execution.py::test_saved_execution_commits_source_mapping_and_replays_fixed_revision`
- `tests/test_workflow_saved_execution.py::test_saved_reservation_rolls_back_both_rows_when_mapping_insert_fails`
- `tests/test_workflow_saved_execution.py::test_legacy_request_database_reads_without_creating_source_table`
- `tests/test_workflow_saved_execution.py::test_saved_source_replay_second_check_is_atomic_across_services`
- `tests/test_workflow_saved_execution.py::test_saved_source_identity_is_rechecked_inside_reservation_transaction`

pytest全fixtureは `tmp_path` を使います。静的検査コマンドは `uv run --no-sync ruff check src/codex_bridge tests/test_workflow_d1_i3b_request_storage.py --no-cache`、`uv run --no-sync ruff format --check src/codex_bridge tests/test_workflow_d1_i3b_request_storage.py`、`uv run --no-sync mypy src/codex_bridge`、`uv run --no-sync python -m compileall -q src/codex_bridge`、`git diff --check` です。

## 受入状態

実装とfocused verificationは完了しました。I3-B受入はChatGPT GitHub review待ちであり、Codex実装完了はreview完了を意味しません。I3全体はIN_PROGRESS、I3-Cは未着手、strict saved-run admissionはOFFです。共有DB移行時には別途運用判断が必要で、この実装では共有/production DBを開いていません。
