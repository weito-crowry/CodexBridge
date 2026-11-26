# D1-I7-C2 isolated strict runtime evidence

- **実装状態:** IMPLEMENTED / PENDING_CHATGPT_REVIEW。
- **対象branch:** `codex/workflow-d1-i5-b4-verified-readers`。
- **base:** I7-C1 commit `50d7e54e369301e70492768cac58090bbdebd746` (ChatGPT ACCEPTED with documented caveats)。
- **範囲:** reserved strict v2のinternal isolated admission、manager/worker/DBOS lifecycle、verified status/result/ownership、既存Codex job durable adapter接続。
- **全体境界:** I7はIN_PROGRESS、I2はDEFERRED / NON-BLOCKING、D1はDRAFT。server default/public strict admissionはOFFです。isolated strict実行は明示的なinternal constructor opt-inに限ります。

## I7-C1 review受入

ChatGPT GitHub review of `50d7e54e369301e70492768cac58090bbdebd746` はI7-C1をACCEPTED (with documented caveats)としました。review時aggregateは706 passed / 10 skipped / 0 failedです。実isolated DBOS launch/run/retrieveとI4 commit後・DBOS checkpoint前のinjected exceptionは確認済みです。実worker process-death中のrecovery、full pytest、live worker、CPP/account-auth、実Codex job、WAL metadataは未検証です。I7-B review時の580 passed / 8 skipped / 1 failedは当時の報告として別に保持し、C1の結果へ合算していません。C1の詳細は[D1-I7C1証跡](D1-I7C1-durable-execution-evidence.md)を参照してください。

## 実装

`WorkflowService`と`WorkflowWorkerManager`にinternalのisolated strict opt-inを追加し、どちらも既定値は`False`です。server production compositionはこのopt-inを渡しません。strict saved runはI6-C preflight、pinned revisionとnormalized input、既存request-id lookup後にrun directoryとrun-local variable storeを作ります。I5 prepared-snapshot coordinatorがreserve commit後に再検証し、新規reservationを取得した呼出だけmanager/workerを起動します。同一request-id・同一normalized requestは既存runを検証して返し、input/revision/modeの不一致はconflictになります。予約commit後の不確実な例外ではrun/request IDを保持してreconciliationへ移し、削除や再送をしません。I6-Cのcorrupt request-source row確認では、authoritative request rowからrun/workflow IDを保持してsafe reconciliationを返しました。

managerはstrict v2を明示的なinternal optionでのみ受け付け、allowed rootsとmanifest workspaceを照合してからversioned private worker configを書きます。legacy worker configはversion 1のままです。worker_entryはconfig/manifest/pinned definitionをdependency import、running status、worker dispatch前に検証します。workerはstrict reserved verifierからpinned source/inputを取得し、DBOS workflow IDをmanifest IDに固定します。strict start/recoverは新しいstrict executorのみを使い、legacy control/hash解釈・manifest workflow ID書換えへfallbackしません。isolated pure runは既存DBOS/lifecycle/heartbeat/shutdown経路を使い、completed workflowへのrecovery attachも確認しました。

strict workerはDBOS successのreadback後、contract output validationを通った値だけを`strict-terminal.json`へbound envelopeとしてatomicに書きます。envelopeはrun/workflow、request/binding、source/contract/input identityを含み、raw stdoutやlegacy `result.json`は使いません。service status readerはreserved verifier、worker lifecycle/start identity、read-only DBOS status、terminal bindingを照合し、outputを再検証してから成功resultを返します。欠落・不整合・DBOS errorやexternal jobの不確実状態はsafe reason code付きreconciliation/failureとなります。失敗reason codeは実DBOS retrieveでも維持されることをC1 testで確認しています。

task/event projectionはstrict runtimeのnode journalがないため静的pinned topologyと`coverage=partial / workflow_not_confirmed`を維持し、eventsは空、cursorを保ちます。variable values、input、prompt、contract正文、legacy result/job journalをstrict public projectionへ読み込みません。shutdown ownershipではstrict reservationとpinned definitionを確認し、pure variable runをexternal-job ownershipから除外します。Codex job runはreserved strict definition由来のexplicit internal authorized modeで既存durable adapterに接続し、request key/ledger identityと`MAYBE_SENT` reconciliationを維持します。fake HTTP transportによるadapter testはstrict DBOS executorをtest process内で実行し、manager/worker subprocess経由ではありません。pure strict workflowのmanager→worker subprocess経路は別testで検証しました。いずれも実Codex APIには接続していません。

## 検証

| 検査 | 結果 |
| --- | --- |
| I7-C2専用suite `test_workflow_d1_i7c2_isolated_runtime.py` | 12 passed。実manager→worker subprocess→DBOS pure workflow、同一request replay、completed workflow recovery attach、fake Codex transportを含む |
| I4-C mutation ledger + I7-B direct variable runtime | 39 passed |
| I5 reader + I6-C saved-run + I7-C1 durable execution focused regression | 52 passed |
| manager / worker_entry / Codex job / service legacy regression | 122 passed / 1 skipped。skipはWindows hostのdirectory symlink privilege不足 |
| workflow HTTP/MCP / task / saved-run regressionとstrict result envelope | 108 passed / 1 skipped。skipはWindows hostのdirectory symlink privilege不足 |
| Ruff check / Ruff format check（変更Python paths） | PASS |
| mypy `src/codex_bridge` | PASS — 156 source files |
| `python -m compileall -q src/codex_bridge` | PASS |
| `git diff --check` | PASS |
| Full pytest / CPP / account-auth / live API / live Bridge / real external Codex job / WAL metadata | NOT_RUN |

全DBとworker処理はWindows nativeのisolated `tmp_path` fixtureを使用します。実worker subprocessの起動・完了と、別worker processでのcompleted DBOS workflow再接続を実行しました。in-flight process-death recovery用の既存DBOS kill-gate harnessはtest treeで確認できず、今回独自の停止用test infrastructureは追加していません。実in-flight crash/recoveryはI8で確認します。production/shared DB、live Bridge、live API、実Codex jobは使用していません。

## 残件

- public strict run admissionはOFFのままです。I8 acceptance前にserverのdefault compositionを有効化しません。
- in-flight worker process death後のDBOS recovery、full pytest、CPP/account-auth、WAL metadata、live API/Bridge、production/shared migration、dogfood、実Codex jobは未検証です。
- I5 strict task/event projectionは引き続き静的topology / empty events / partial coverageです。strict runtime node-event journalは別途必要かをI8で扱います。
- manager/worker isolated successとcompleted recoveryは検証済みですが、DBOS/requests/definitions/variables/job ledger間のcross-database atomicityを主張しません。

## 変更path

- `src/codex_bridge/workflows/api.py`
- `src/codex_bridge/workflows/codex_job_adapter.py`
- `src/codex_bridge/workflows/interpreter.py`
- `src/codex_bridge/workflows/manager.py`
- `src/codex_bridge/workflows/mcp.py`
- `src/codex_bridge/workflows/service.py`
- `src/codex_bridge/workflows/strict_executor.py`
- `src/codex_bridge/workflows/strict_requests.py`
- `src/codex_bridge/workflows/strict_variable_mutations.py`
- `src/codex_bridge/workflows/strict_variable_runtime.py`
- `src/codex_bridge/workflows/task_projection.py`
- `src/codex_bridge/workflows/worker.py`
- `src/codex_bridge/workflows/worker_entry.py`
- `tests/test_workflow_d1_i7c1_durable_execution.py`
- `tests/test_workflow_d1_i7c2_isolated_runtime.py`
- `tests/test_workflows_api.py`
- `tests/test_workflow_mcp.py`
- `docs/workflow-design/phases/D1-I7C1-durable-execution-evidence.md`
- `docs/workflow-design/phases/D1-I7C2-isolated-runtime-evidence.md`
- `docs/workflow-design/phases/D1-implementation-plan.md`
