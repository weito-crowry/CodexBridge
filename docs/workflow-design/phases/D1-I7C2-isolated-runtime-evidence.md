# D1-I7-C2 isolated strict runtime evidence

- **実装状態:** I7-C2 / I7 ACCEPTED with documented caveats by ChatGPT review of `afda1d9d05ee126ec1ceb5d81bab9b6a5879ac7f`。I8-Aも同reviewでACCEPTED with documented caveats。I8-B final cleanup is IMPLEMENTED / PENDING_CHATGPT_REVIEW。
- **対象branch:** `codex/workflow-d1-i5-b4-verified-readers`。
- **base:** I7-C1 commit `50d7e54e369301e70492768cac58090bbdebd746` (ChatGPT ACCEPTED with documented caveats)。
- **範囲:** reserved strict v2のinternal isolated admission、manager/worker/DBOS lifecycle、verified status/result/ownership、既存Codex job durable adapter接続。
- **全体境界:** I7はACCEPTED with documented caveats、I8-Bはreview待ち、I8全体acceptance/go-no-goは未完了です。I2はDEFERRED / NON-BLOCKING、D1はDRAFT。server default/public strict admissionはOFFです。isolated strict実行は明示的なinternal constructor opt-inに限ります。

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
| Full pytest / CPP / account-auth / live API / live Bridge / real external Codex job / WAL metadata | NOT_RUN at C2 review time; I8-A follow-up results below |

全DBとworker処理はWindows nativeのisolated `tmp_path` fixtureを使用しました。C2 review時点では実worker subprocessの起動・完了とcompleted DBOS workflow再接続までで、in-flight process-death recoveryは未実施でした。その後I8-Aで専用のisolated subprocess gateにより検証しています。production/shared DB、live Bridge、live API、実Codex jobは使用していません。

## 残件

- public strict run admissionはOFFのままです。I8 acceptance前にserverのdefault compositionを有効化しません。
- C2 review時点ではin-flight worker process death後のDBOS recovery、full pytest、CPP/account-authは未検証でした。I8-A follow-up結果は後段を参照してください。WAL metadata、live API/Bridge、production/shared migration、dogfood、実Codex jobは引き続き未検証です。
- I5 strict task/event projectionは引き続き静的topology / empty events / partial coverageです。strict runtime node-event journalは別途必要かをI8で扱います。
- manager/worker isolated successとcompleted recoveryは検証済みですが、DBOS/requests/definitions/variables/job ledger間のcross-database atomicityを主張しません。

## I7-C2 review follow-up / I8-A repair

ChatGPT GitHub review of `98ec2f85e71f28d6eda5c79856bf345193b9f8b5` accepted the I7-C2 isolated admission/worker/DBOS core with a remaining concrete fix and kept final I7 acceptance pending that fix. I8-A completed the follow-up; this does not itself record ChatGPT acceptance of the fix.

ChatGPT GitHub review of `afda1d9d05ee126ec1ceb5d81bab9b6a5879ac7f` subsequently accepted I7-C2/I7 with documented caveats and accepted the I8-A workflow integration, including the profile-1 identity fix, pending-job reconciliation, ownership verification, and isolated worker process-death recovery. The reported full suite was 3,955 passed / 24 skipped / 3 failed, so it was not all-green. I8-B records the test-double repair and final suite; I8 overall acceptance remains pending.

レビュー時に報告した`333 passed / 2 skipped`は、C2専用12、I4-C/I7-B 39、I5/I6-C/I7-C1 52、manager/entry/Codex job/service 122/1、HTTP/MCP/task/saved-run 108/1の複数group合算です。単一aggregate rerunではありません。C1 reviewの706 passed / 10 skipped / 0 failedとは別の結果です。

I8-Aでは次を修正・確認しました。

- profile-1 status readerのexpected Codex job identityをJSON Pointer graph node IDからInterpreter task pathへ変更。nested `do` scope、profile-2/3、profile-3 normalized inputのenabled-item orderも同じhelperで実行側と照合します。
- terminal failureは全job ledger rowが`SUCCEEDED`または`FAILED`のsettled stateである場合だけ投影し、pending/uncertain external jobを通常failedにしません。
- strict shutdown ownershipはreserved pinned definitionから得るtask identityも照合し、別task identityのACCEPTED/RUNNING turnを所有turnとして返しません。
- actual isolated HTTP/MCP → service → manager → worker subprocess → DBOS flow、loopback fake Codex API、same-request replay/no duplicate POST、invalid output、I4 mutation commit後・DBOS checkpoint前のworker process terminationとsame workflow ID recoveryを実行しました。詳細は[I8-A evidence](D1-I8A-integration-acceptance-evidence.md)。

I8-A専用integration suiteは7 passed、status/graph/ownership/identity repair targeted casesは9 passedです。Full `uv run --no-sync pytest`は3,955 passed / 24 skipped / 3 failed (3,982 collected)でした。3 failureは未変更`server.py` shutdown/start cleanupが`tests/test_ui_server.py`内の`FakeBridge`にない`drain_final_answer_writes`を呼ぶものです。failureは今回変更外ですが、clean baselineは再実行しておらず、既存baseline failureとは確定していません。CPP/account-auth suitesはfull runでpassしました。Ruff check/format、mypy (156 source files)、compileall、diff checkもpassです。

I8-A full test executionにより、C2時点でNOT_RUNだったfull pytest/CPP/account-authとisolated in-flight process-death recoveryは更新済みです。live Bridge/API、real external Codex job、production/shared DB/migration、WAL metadataは引き続きNOT_RUNです。I7はACCEPTED with documented caveats、I8-B/I8 final acceptanceはreview待ちです。public admissionはOFF、D1はDRAFTです。

I8-Bでは上記3件の`tests/test_ui_server.py` test-double failureを、production codeに触れず修正しました。更新後のWindows native full `uv run --no-sync pytest`は3,959 passed / 24 skipped / 0 failed (3,983 collected)です。I8-A当時の3,955/24/3は当時の結果として保持し、I8-Bの最終値と混同しません。詳細なskip、D1/PD traceability、operator gate、isolated go/no-goは[D1-I8-B evidence](D1-I8B-final-acceptance-evidence.md)に記録しています。I8-BはChatGPT review待ち、I8全体とD1のfinal acceptanceは未確定です。

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
