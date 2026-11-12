# Phase 5F-2B: Workflow 診断ログ

## 保存場所と追跡

各run directory直下に、BridgeとWorkerが別々に追記するJSONLを保存します。

- `bridge-lifecycle.jsonl`: request受付・状態遷移、worker起動、停止要求、終了観測。
- `worker-lifecycle.jsonl`: worker bootstrap、DBOS起動、workflow開始/復旧、結果保存、shutdown観測、Worker例外。
- 既存の`requests.sqlite`、`codex_jobs.sqlite`、`job_events`、`job_reconciliation`、`task-events.jsonl`、`startup-diagnostic.json`、`worker-entry-status.json`、`result.json`は引き続き根拠データとして扱います。既存ファイルの形式やDB schemaは変更しません。

まず`requests.sqlite`でrequestからrunを特定します。

```sql
SELECT workflow_id, run_id, state, request_digest
FROM workflow_requests
WHERE client_request_id = ?;
```

その`run_id`のディレクトリで2つのlifecycle logを読み、各ファイル内の行順と`timestamp_utc`を確認します。次に`workflow_id`でCodex jobを絞り、`jobs`と`job_events`を`job_id`で結びます。

```sql
SELECT j.workflow_id, j.job_id, j.request_key, j.task_identity,
       e.event_id, e.state, e.timestamp
FROM jobs AS j
JOIN job_events AS e USING (job_id)
WHERE j.workflow_id = ?
ORDER BY e.event_id;
```

`job_events.event_id`は同じDB内の順序確認に使います。`payload_json`や結果本文をlifecycle logへ複製しません。Bridge/Worker間のミリ秒単位の完全順序やexactly-once配送は保証しないため、異なるファイルはtimestamp、両ファイル内の順序、durable ledgerのIDを合わせて読みます。

## Lifecycle event一覧

すべてのeventに`version: 1`、UTCミリ秒の`timestamp_utc`、`source`、固定`event`名、`run_id`を記録します。確定後は`workflow_id`、起動ごとには`launch_id`を加えます。各eventに必要な値だけを記録します。

| 発行側 | Event | 記録タイミングと主な追加項目 |
| --- | --- | --- |
| Bridge service | `request_reserved` | request ledgerへのcommit後。`workflow_id`, `request_digest` |
| Bridge service | `request_replayed` | 同じrequest ID・digestを再受付した判定後。現在`state`、固定`reason_code`。再spawnしない |
| Bridge service | `request_conflict` | 既存request IDとdigestが異なると判定した時。既存`state`、試行digest、固定`reason_code`。新規runやspawnは行わない |
| Bridge service | `workflow_state_changed` | durable state update後。`previous_state`, `state`、必要時`reason_code` |
| Bridge service | `request_start_failed` | 起動/readiness失敗の分類確定時。`state`, 固定`reason_code`、必要時`exception_class` |
| Bridge manager | `worker_launch_requested` | validation/config確定後、spawn直前。`launch_id`, `mode` |
| Bridge manager | `worker_spawn_succeeded` | subprocess生成後。`launch_id`, `mode`, `pid` |
| Bridge manager | `worker_spawn_failed` | spawn失敗確定時。`launch_id`, `mode`, `exception_class`, 固定`reason_code` |
| Bridge manager | `worker_spawn_outcome_unknown` | spawn待機中のcancel等で成否不明時。同上。成功/終了とは推定しない |
| Bridge manager | `shutdown_signal_requested` / `shutdown_signal_failed` | shutdown signalの書込成功/失敗時。`launch_id`, `pid`、失敗時の分類 |
| Bridge manager | `worker_terminate_requested` / `worker_terminate_failed` | 必要時のterminate要求と失敗時。`launch_id`, `pid`、失敗時の分類 |
| Bridge manager | `worker_kill_requested` / `worker_kill_failed` | 必要時のkill要求と失敗時。`launch_id`, `pid`、失敗時の分類 |
| Bridge manager | `worker_shutdown_failed` | kill後もexit waitが終わらない時。`launch_id`, `pid`, 固定`reason_code` |
| Bridge manager | `worker_exited` | `process.wait()`が返った時のみ。`launch_id`, `mode`, `pid`, `returncode`, `shutdown_requested`, 終了`state` |
| Worker | `worker_bootstrap` | run-dir、manifest、identityを確認・保存した後。`mode`, `pid`, 利用可能なら`launch_id` |
| Worker | `dbos_ready` | `DBOS.launch()`成功後。`mode`, `pid` |
| Worker | `workflow_started` / `workflow_recovery_attached` | workflow ID確定後。`workflow_id`, `mode`, `pid` |
| Worker | `result_persisted` | 既存`result.json`の保存後。`workflow_id`, `mode`, `pid` |
| Worker | `shutdown_observed` | Workerがshutdown要求を検知した時。`workflow_id`, `mode`, `pid`, 固定`reason_code` |
| Worker | `worker_failed` | Worker内例外時。`workflow_id`、`mode`, `pid`, `exception_class`, 固定`reason_code` |

同じrunでstartとrecoverが行われた場合、`launch_id`で各起動を分けます。shutdown signalの後に`worker_exited`がなくManagerも終了している場合、終了結果は不明です。exit行がないことを「worker終了」と解釈しません。

## 記録内容、上限、失敗時

field名、event名、state、mode、reason code、識別子の文字種と長さをallowlistで制限します。`exception_class`はクラス名だけです。自由形式の例外メッセージ、token/Authorization/header、prompt、workflow YAML、input、結果本文/final_text、cwdや個人絶対パス、環境変数、traceback、argvは記録しません。`request_digest`だけを記録し、元のrequest内容は記録しません。

1レコードは改行を含め2KiB以下、ファイルは1 run/sourceあたり2MiBまでです。上限到達後のレコードは追加せず、既存行のrotate/deleteはしません。ログはrun directory直下だけを使い、新規directoryは作りません。既存のsymlink/reparse point、通常ファイルでない占有先、上限超過、書込エラーは記録失敗として扱います。

writerはbest-effortです。書込失敗時はpathや例外内容を出さない短いwarningだけを既存loggerへ送り、正常な受付、state update、worker実行/終了結果を失敗に変えません。診断logがない・途中で止まる場合、記録対象eventが発生しなかった、書込できなかった、またはprocessが記録前に落ちた可能性があります。欠落から状態を推定せず、既存ledgerとstatus/resultを確認してください。`startup-diagnostic.json`の失敗時のみ記録と`O_EXCL`契約は維持します。

## Codex jobの解釈

`workflow_id`から`jobs`の`job_id`、`request_key`、`task_identity`を求め、`job_events`を`job_id`と`event_id`で追います。`job_reconciliation`のmarker/reasonも同じjob IDで確認します。

`MAYBE_SENT`はPOST送信権を得るdurable claimであり、リモートが実際に受理した証拠ではありません。POST応答やthread/turn IDが不明ならUNKNOWN / `needs_reconciliation`を維持します。同じ`job_id`を再POSTする根拠にはせず、再POST禁止は変更していません。R1 BLOCKEDとR1C再POST UNKNOWNも従来どおりです。

## 未実施の確認

pytestはtemp run-dir、mock process、fake DBOSを使います。稼働中Bridge接続、実Codex POST、旧R1 replay、既存隔離processへの操作、OS process killによる障害実機試験は行っていません。実運用のfault-injection/dogfoodは未実施です。
