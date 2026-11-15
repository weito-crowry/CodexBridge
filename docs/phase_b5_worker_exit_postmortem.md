# Phase B5 Worker exit 1 postmortem

Issue #142 — read-only調査。調査日: 2026-11-16 (UTC)

## 判定

| 対象 | 判定 | 結論 |
|---|---|---|
| API `needs_reconciliation` の直接契機 | **LIKELY** | single-jobの `MAYBE_SENT` を即 `needs_reconciliation` にする実装があり、該当ledgerは約344 ms後に `ACCEPTED`、さらに約57 ms後に `RUNNING` へ進んだ。runnerがその短い区間を読んだ可能性が最も高い。該当GETの時刻は保存されておらず確定できない。 |
| Worker `exit_code=1` | **CONFIRMED** | runnerが `needs_reconciliation` を受けてBLOCKED終了した後、finally cleanupで隔離Bridgeへshutdownを要求した。Workerはshutdownを観測して `WorkerShutdownRequested` をentryへ伝播し、entryがこれを `error_code` に記録してexit 1にした。 |
| 実Codexの最終結果 | **UNKNOWN** | 最終ledgerは `RUNNING` / `in_progress`、receiptとresult fileはなく、remoteの完了・失敗は判定できない。 |

**B5の最終受入判定はBLOCKEDのまま**とする。#141の既存報告/evidenceは変更していない。

## 調査対象と方法

親commit `3e1030cec376a614d3442e43ed3b7e2f4b2d89ff` から作られた `codex/workflow-phase-b5-readonly-postmortem` で調査した。既存の最終attempt evidence [`phase_b5_workflow_acceptance-6f061e434b3949b889a6b9bb673e70ee.json`](phase_b5_workflow_acceptance-6f061e434b3949b889a6b9bb673e70ee.json)、既存run artifact、当該attemptのソースコードだけを読み取った。隔離TEMP候補は存在し、指定run prefix `run-b18e2ab1` と一致するrun directoryは1件、manifest上のworkflow prefixは `38669007` だった。他attemptや通常rootは探索していない。

manifestは `ui-api` / `codex-job-live`、定義revisionはledger上2、source SHA prefixは `ae431f5f21b6`。definition SHA prefixは `8cf15283a268`。manifest/run/workspaceの対応は一致した。個人ディレクトリ、token/auth、raw definition/input/prompt、Codexの生結果、thread/turn ID、完全なrequest/run/workflow IDは記録していない。

SQLiteは対象3 DBをURI `mode=ro` と `PRAGMA query_only=ON` で開き、読み取りSELECTだけを実行した。明示的なDB書き込み・sidecar削除は行っていない。SQLiteのread-only openでもWAL用sidecarが作られる場合があるため、見つけたsidecarはそのまま保持した。

## 観測された事実と時系列

時刻はartifactが記録したUTC。PID値とパスは省略した。

| UTC | 観測 |
|---|---|
| 21:44:59.949 | `workflow_requests` に対象run 1行を記録。request stateは後に `running`、最終更新時刻は21:45:00.379。`workflow_request_sources` は同じrunにrevision 2を1行記録。 |
| 21:45:01.789 / .894 / 02.040 | worker lifecycleの順に `worker_bootstrap`、`dbos_ready`、`workflow_started`。identity artifactがあり、heartbeatは11件、最終heartbeatは21:45:03.544。別個のworker startup diagnosticファイルはrun directoryに見つからなかった。 |
| 21:45:02.791 / .824 / 03.167 / .224 | job ledger/eventは順に `PREPARED` → `MAYBE_SENT` → `ACCEPTED` → `RUNNING`。最終rowは1件、state `RUNNING`、remote state `in_progress`、receiptなし、errorなし。 |
| 21:45:03.532 | Bridge lifecycleに `shutdown_signal_requested`。対応するshutdown request artifactのmtimeは21:45:03.530。 |
| 21:45:03.580 | worker lifecycleに `shutdown_observed`, reason `shutdown_requested`。worker lifecycleには `worker_failed` eventはない。 |
| 21:45:03.894 / 04.240 | `worker-entry-status.json` のmtime後に、Bridge lifecycleが `worker_exited`, state `stopped` を記録。entry statusの値は `failed`, exit code 1, error code `WorkerShutdownRequested`。 |
| 21:45:04.864 | 既存evidenceの終了時刻。owned Bridgeはidentity確認済みでgracefulにreap、forced terminationなし、orphan 0。Bridge restart gateは `NOT_RUN`。 |

DBOSはmanifestのworkflow IDと一致する1行が `PENDING`、errorなし、started/completed timestampなし。operation outputは7件、error 0件で、最初の開始は21:45:02.772、最後の完了は21:45:03.992。workflow events/historyは0件。DBOSの非terminal状態やstep outputはremote Codexの最終結果を示さない。`result.json` とCodex job receiptは存在しない。

API `needs_reconciliation` は既存runner evidenceに記録され、runner reasonは `workflow_did_not_reach_confirmed_success`、result gateは `NOT_OBSERVED`。runner sourceでは `_wait_workflow_success` がstatus GETで `needs_reconciliation` を受けた時点でBLOCKEDをraiseする。その呼び出しを抜けた後、外側のfinallyがowned Bridge cleanupを行う。したがって、記録順とコード経路の両方から、**cleanup shutdownは先行する `needs_reconciliation` 判定の原因ではない**。

## 原因の切り分け

### Worker exit 1 — 確定

workerは `workflow_wait` 中にshutdown signalを観測し、`shutdown_observed` を記録して `WorkerShutdownRequested` をraiseした。worker本体はこの例外を `worker_failed` の一般例外処理に通さず再raiseする。entryはworker呼出しの一般例外として例外class名を `error_code` に書き、exit 1を返す。Bridge managerはshutdown要求済みの子が非zeroで終了した場合、manager stateを `stopped` と記録する。今回の `worker_exited=stopped` と一致する。

これにより、最終 `worker-entry-status=failed/1` はWorker内部の未処理業務エラーを示すものではなく、runner cleanupで要求された停止の結果と確定できる。DBOS destroy失敗を示す `worker_failed/dbos_destroy_failed` もなく、statusのerror codeは別例外ではなくshutdown例外そのものだった。

### API `needs_reconciliation` — 最有力経路は未確定

保存されたjob eventでは `MAYBE_SENT` が21:45:02.824、`ACCEPTED` が03.167で、その間は約344 ms。`service.py` のsingle-live-job projectionは `MAYBE_SENT` を直接 `needs_reconciliation` として返す。一方、`RUNNING` とDBOS `PENDING`が揃う状態は通常 `running` に射影される。runnerはstatusを約0.5秒間隔でpollし、`needs_reconciliation` を一度読むと待ち続けずBLOCKEDにする。

以上から、pollがこの短い `MAYBE_SENT` 区間に当たったことが**最有力**。ただしrunnerは各GETの時刻・応答本文を保存していない。source/manifestの一時的なread/validation問題やDBOS status read errorもコード上は `needs_reconciliation` になり得るため、GET時点の実経路は断定しない。cleanup後のworker status `failed` はAPI判定より後に作られたので、最初のBLOCKED判定を説明しない。

## 関連コード位置

- [`phase_b5_workflow_isolated_smoke.py`](../scripts/phase_b5_workflow_isolated_smoke.py:1247): `needs_reconciliation` を受けて即BLOCKEDにするpoll。
- [`phase_b5_workflow_isolated_smoke.py`](../scripts/phase_b5_workflow_isolated_smoke.py:1857): wait呼び出しとobserver終了の順序。
- [`phase_b5_workflow_isolated_smoke.py`](../scripts/phase_b5_workflow_isolated_smoke.py:2303): BLOCKED例外処理後のfinally cleanup。
- [`service.py`](../src/codex_bridge/workflows/service.py:1595): single-job `MAYBE_SENT` のneeds-reconciliation射影。read/validationとDBOS read failure分岐は同ファイル1534、1603-1611付近。
- [`service.py`](../src/codex_bridge/workflows/service.py:1685): worker/DBOS/jobが進行中の場合の `running` 射影。
- [`codex_job_adapter.py`](../src/codex_bridge/workflows/codex_job_adapter.py:779): `MAYBE_SENT` の永続化。`mark_accepted` と `RUNNING` 遷移は897、1078付近。
- [`worker.py`](../src/codex_bridge/workflows/worker.py:1150): shutdown観測。1198-1235でshutdown例外を再raiseし、DBOS destroy例外だけを別のworker failure eventとして記録。
- [`worker_entry.py`](../src/codex_bridge/workflows/worker_entry.py:83): running status後に例外classを記録してexit 1にする経路。
- [`manager.py`](../src/codex_bridge/workflows/manager.py:345): shutdown signal。512付近のexit observerはshutdown要求済みの子を `stopped` と記録。

`worker.py` が実際に開くCodex ledgerは `codex_jobs.sqlite`。`job_adapter.py` の `DurableJobAdapter` は別の汎用 `jobs.sqlite` を扱い、今回の4段階Codex ledger記録とは別物である。Workerとisolated Bridgeはstdout/stderrをDEVNULLへ接続するため、生ログ・stack traceは保存されていない。entry statusの安全な例外classだけを確認できた。

## 影響と次の安全な確認

- 実Codex jobは1件だけ開始され、追加の初回requestはない。既存evidenceに記録されたHTTP initial acceptanceは202。今回のpostmortemでのHTTP/MCP POST、新規Workflow start、Codex実行、Bridge start/restartはすべて0。
- Gate 1のMCP/Control定義受入は既存evidence上PASS。Gate 2はBLOCKED、result `NOT_OBSERVED`。MCP same-ID replayとGate 3 restartは既存evidence上 `NOT_RUN`。この調査では再実施していない。
- runnerがcleanupで停止したのは所有確認済みの隔離Bridgeだけ。既存evidenceでBridgeのreapとorphan 0を確認。worker自身のlive PID/creation-time ancestry観測はrunnerが成功経路でのみ行うため `NOT_OBSERVED`。identity artifactの値は報告に含めていない。
- 次はChatGPTレビューで、B4 single-job `MAYBE_SENT` のstatus semanticsとB5の約0.5秒poll/即BLOCKED方針の意図を確認する。必要なら次段階はfake transportとsynthetic ledgerを使うオフライン試験で状態射影を再現する。今回の保存済みrequest IDは `MAYBE_SENT` を経たため、同一ID再送を含め再送しない。
- この調査だけでは実Codex remote outcomeが不明のまま。root/DBOS/Codex状態のreconciliation、再起動、再送で推測を補わない。

## 実施した確認・未実施

実行したコマンド/確認:

```text
git status --short --branch
git branch --show-current
git rev-parse HEAD
git remote -v
uv run --no-sync python -c "tempfile.gettempdir() の指定候補について exists/is_dir/is_symlink のみ表示"
uv run --no-sync python - "既存runのallowlist JSON読取、およびSQLite URI mode=ro + PRAGMA query_only=ON + SELECT"
rg -n "worker_failed|shutdown|MAYBE_SENT|ACCEPTED|RUNNING|needs_reconciliation|observe_exit" <限定したsource/test paths>
Get-Content <限定したsource file> | Select-Object -Skip <限定範囲> -First <限定行数>
git add -- docs/phase_b5_worker_exit_postmortem.md
git diff --cached --check
```

実施した読取: 指定prefixの単一run artifact一覧、status/lifecycle/heartbeat/manifestのallowlist読取、対象SQLiteのread-only queryとschema確認、runner/worker/manager/service/adapterの限定ソース読取、sanitize済み時刻・count・status照合。

未実施: pytest/ruff/mypy/compileall再実行（製品・runnerコードを変更していないため）、HTTP/MCP readまたはwrite、Workflow start/POST/replay、Codex CLI/app-server、DBOS launch/step/recovery/reconciliation、Bridge起動/再起動、live process kill、認証原本読取、全pytest。#141の既存Gate 0結果は既存evidenceの記録として保持し、この調査の再実行結果とは扱わない。
