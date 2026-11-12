# Phase 5F-2A オフライン障害境界・復旧安全性

## 実施境界

対象は隔離temp root、SQLite、APIモックを使うpytestのみ。実Codex job/thread、実workflow POST、実OS process crash/killは発生させていない。稼働中の本番Bridge、隔離Bridgeのプロセス、既存R1には接続・書き込みしていない。

各境界のPASSは記載したオフラインテストの範囲に限る。実機crash、実HTTP応答消失、実Codex側の同一ID処理は未実証である。

## 障害境界

| 境界 | 既存・追加テスト | 期待結果 | 実行結果 |
| --- | --- | --- | --- |
| A. request予約後、manager呼び出し前の中断 | `test_queued_reservation_after_service_interruption_is_not_respawned`（追加）、`test_same_request_id_reuses_durable_run_after_service_recreation` | 永続化後・manager呼び出し前の中断では状態は`queued`で残る。同一IDの再受付はworkerをspawnせず、現在の`queued`再生規則に従い`failed_to_start`へ投影する。 | **PASS**。`queued`をSQLiteで確認し、再生成serviceで再spawnがないことを確認。 |
| B. manager起動の成否が不確定 | `test_uncertain_worker_start_is_persisted_and_never_retried`、`test_startup_exception_diagnostic_preserves_http_classification_and_no_retry` | 不確定例外は`needs_reconciliation`として永続化し、service再生成後も再spawnしない。明示的manager起動失敗は`failed_to_start`として再spawnしない。 | **PASS**。両分類と再spawnなしを確認。 |
| C. `PREPARED`と`MAYBE_SENT`、送信応答消失 | `test_prepared_job_posts_once_after_recreation_and_replay_does_not_post`（追加）、`test_maybe_sent_post_is_never_retried_after_recreation`（追加、3ケース）、`test_worker_never_creates_second_job_when_first_is_not_succeeded` | `PREPARED`は送信claim後に1回だけPOSTする。claim後の`MAYBE_SENT`、応答消失、thread/turn ID不明は再POSTせず、`needs_reconciliation`を保持する。 | **PASS**。モックで`PREPARED`の単発送信と3種類の`MAYBE_SENT`再開を確認。 |
| D. 保存済みthread/turn IDの再観測 | `test_observe_job_does_not_retry_http_identity_mismatches`、`test_observe_job_rechecks_only_transient_status_turn_mismatches`、`test_observe_job_keeps_reconciliation_when_transient_mismatch_persists`、`test_observe_job_still_requires_exact_completed_turn_and_final_answer`、`test_worker_completes_three_sorted_jobs_and_replays_without_new_posts` | ACCEPTED/RUNNINGは保存済みIDに対するGETで観測し、新規POSTしない。識別子不一致、観測不整合、exact turn/final answer欠落をsuccessに昇格させない。 | **PASS**。GET経路、永続的不整合のreconciliation、再生時のPOST増加なしを確認。 |
| E. worker/DBOS完了とresult保存の中断 | `test_completed_worker_without_valid_result_is_not_reported_as_success`（追加、欠落/破損2ケース）、`test_interrupted_atomic_result_write_preserves_existing_result`（追加）、`test_status_and_result_use_read_only_sqlite_and_verify_saved_result`、`test_success_projects_running_until_worker_entry_completes` | worker/DBOS完了だけではsuccessにせず、欠落・破損resultは`unknown`とする。resultのtemp書き込み中断で既存resultを破壊しない。 | **PASS**。欠落/破損を`unknown`とし、失敗したJSON直列化後も既存resultが保たれることを確認。 |
| F. 3 jobループの2件目不確定/失敗 | `test_worker_does_not_dispatch_third_job_after_second_is_not_succeeded`（追加、needs_approval/failed/post_errorの3ケース）、`test_v2_adapter_allows_three_jobs_with_stable_replay_and_capacity_limit`、`test_worker_completes_three_sorted_jobs_and_replays_without_new_posts`、`test_three_job_loop_stops_after_second_job_requires_reconciliation` | 2件目が未成功なら3件目を作成・送信しない。同じworkloadの再生は既存request keyを使い、新たなPOSTを増やさない。 | **PASS**。各2件目ケースでPOSTは2件まで、通常再生でPOST増加なしを確認。 |

## 検証結果

- focused pytest: **16 passed**。追加A/C/E/Fと既存B/D・再生テストを選択実行。
- workflow回帰: `uv run --no-sync pytest -q tests/test_workflows_service.py tests/test_workflows_manager.py tests/test_workflows_codex_job.py tests/test_workflows_multijob.py tests/test_workflows_multi_api.py` — **252 passed, 2 skipped**。
- SKIP: `test_product_codex_job_rejects_symlink_escape`と`test_multi_job_symlinked_ledger_is_not_followed`。Windowsのsymlink作成に必要な権限がなく、pytestがSKIP。PASSには読み替えていない。
- Ruff: `uv run --no-sync ruff check` — **PASS**。
- format: `uv run --no-sync ruff format --check` — **PASS**、329ファイル確認。
- mypy: `uv run --no-sync mypy src` — **PASS**、120 source files。
- compileall: `uv run --no-sync python -m compileall -q src` — **PASS**。
- lock: `uv lock --check` — **PASS**、66 packages resolved。
- diff whitespace: `git diff --check` — **PASS**。

`uv run`は環境変数`VIRTUAL_ENV=D:\CodexBridge\.venv`がcheckoutの`.venv`と異なる警告を出し、project環境を選択して実行した。コマンドは終了コード0。

## 未実証・据え置き

- 本物のOS crash、worker/DBOSプロセスkill、起動中processの再開順は未実証。Phase 5F-2B以降のdogfood範囲であり、本Phaseでは実施していない。
- 実CodexへのPOST、実通信応答消失、実Bridge上のthread/turn ID不明挙動は未実証。POST境界はSQLite状態とmock clientで検証した。
- R1初回E2EとR1の過去BLOCKED、R1C同一`client_request_id`再POSTのUNKNOWN判定は変更しない。いずれも遡及してPASSにしていない。
- Windows symlink権限に依存する上記2テストはSKIPのまま。
