# D1-I7-C1 legacy reader repair and isolated durable strict executor evidence

- **実装状態:** IMPLEMENTED / PENDING_CHATGPT_REVIEW。
- **対象branch:** `codex/workflow-d1-i5-b4-verified-readers`。
- **base:** I7-B commit `ee3bd96749db6e37ad260297ed6b55e0036d450c` (ChatGPT ACCEPTED with documented caveats)。
- **範囲:** legacy v0 optional request-source reader regression fixと、予約済みstrict v2専用のisolated DBOS executor/checkpoint path。
- **全体境界:** I7はIN_PROGRESS、I2はDEFERRED / NON-BLOCKING、D1はDRAFT、public strict run admissionはOFFです。

## I7-B reviewと既知regression

ChatGPT GitHub review of `ee3bd96749db6e37ad260297ed6b55e0036d450c` はI7-B direct variable runtimeをACCEPTED (with documented caveats)としました。review時のI4〜I7B/graph/saved execution/service/manager/codex-job focused aggregateは580 passed / 8 skipped / 1 failedと報告され、all-greenではありません。既知failureは`test_legacy_request_database_reads_without_creating_source_table`です。I7-C1はこのreader境界を修正し、strict claimや不正schemaをlegacyとして扱わないテストを追加しました。

## 実装

`service._strict_binding_claim`はlegacy v0 metadata確認時に限り、`workflow_request_sources`が欠落した正確なlegacy table setを許容します。`workflow_requests`のschemaは従来どおり照合され、余分なtable、欠落した別table、malformed schemaはlegacyとして通しません。実strict binding claim、strict manifest claim、未知version/不正DBはstrict candidateのままです。legacy replayはsource tableを作成せず、read-onlyを維持します。strict reader gateの共通checkは既定で従来の完全table setを要求し、緩和はこのv0 reader判定の明示呼出しにだけ適用されます。

新しい`strict_executor.execute_strict_ows_workflow`は既存legacy `execute_ows_workflow`とは別のDBOS workflowです。入力はrun ID、state root、allowed roots、`isolated` modeのみで、manifest claimから予約済みstrict snapshot、pinned definition/contract、normalized inputを再検証します。callerからsource/input/contractを受け取らず、manifestを書き換えません。DBOS workflow IDと検証済みmanifestのworkflow IDを一致させ、isolation mode不一致・identity不一致は値を含まないエラーにします。manager、worker_entry、direct `worker.main`のstrict runtime拒否はこのsliceでは維持し、新executorを通常起動経路へ接続していません。

strict call targetを実行前に検査し、I7-C1が扱わない`codex-job`等の非variable callはeffect前に`strict_execution_target_unavailable`へ収束します。strict variable operationは、各利用phaseのroot projectionをstable phase ID由来のDBOS stepでcheckpointし、そのprojectionから評価したmutation argumentsとbefore-CASを別prepare stepで保存した後、別apply stepからI4 ledgerへ渡します。checkpointにはreserved binding/definition/input identity、phaseまたはmutation contextを含め、replay時に形・identityを再検証します。pure OWS task actionもpinned nodeと入力から名前付きstepでcheckpointします。I4 historical replayは同じprepared argumentsの再適用時にhistorical after valueを返し、DBOS cached resultとの一致を確認します。後続variable stateから過去のjq payloadを再評価しません。

outputは既存`validate_output`で検証してからのみ`status=succeeded` envelopeへ入ります。output violation、resource limit、invalid operation、uncertain/reconciliation等は値非露出のreason codeへ写像し、失敗envelopeを成功resultとして返しません。legacy Interpreter、legacy DBOS workflow名/signature、StepRunner behavior、public DTO/schemaは変更していません。

## 検証

| 検査 | 結果 |
| --- | --- |
| I7-C1 durable executor専用suite（実DBOS launch/run/retrieveを含む） | 9 passed |
| I7-C1 executor + saved execution回帰 | 25 passed。legacy missing optional source table、strict claim/missing manifest/forced-state、不正schemaを含む |
| I7-B direct variable runtime regression | 15 passed |
| I4〜I7C1、graph、task projection、saved execution、service/manager/codex-job focused aggregate | 706 passed / 10 skipped / 0 failed (716 collected; 1415.96s) |
| Ruff check（変更Python paths） | PASS |
| Ruff format check（変更Python paths） | PASS — 8 files |
| mypy `src/codex_bridge` | PASS — 156 source files |
| `python -m compileall -q src/codex_bridge` | PASS |
| `git diff --check` | PASS |
| Full pytest / CPP / account-auth / live API / live Bridge/Worker / external Codex job / WAL metadata | NOT_RUN |

全testはWindows nativeの隔離`tmp_path` DB/test fixtureを使用します。10 skipはhostでsymlink privilegeがないtestです。実DBOS launch/runとcompleted workflow destroy/relaunch後のretrieveは実施しましたが、外部Codex job、共有/production DB、live Bridge/workerは使わず、productionや通常workerへ新executorを接続していません。

## 残件とI7-C2境界

- testでは、I4 mutation commitの直後、DBOS checkpoint記録の前に例外を注入し、後続のvariable mutation後も同じprepared payloadのapply step body再実行がhistorical after値を返し、最新値を巻き戻さず二重適用もしないことを確認しました。これは実worker processの突然停止・再起動を用いたin-flight DBOS recovery testではありません。completed workflowのdestroy/relaunch後readbackと分けて記録し、実in-flight recoveryはI8 acceptanceで検証します。
- I7-C2でmanager/worker_entry/worker.mainからisolated runに限ったstrict lifecycle接続、request preparation/reservation順序とidempotent replay、verified status/result/lifecycle/ownership、既存codex-job durable adapter、isolated end-to-end executionを実装・検証します。
- I7-C1ではstrict public admissionを有効化せず、productionや外部Codex jobを実行しません。strict admissionはI8受入までOFFです。
- full pytest、CPP/account-auth regression、live API、live/real worker、external job、WAL metadata、およびactual in-flight DBOS crash/recoveryは未検証です。
- I6-Cのcorrupt request-source ledger replayがID保持前にunavailableとなり得るreview caveatは、C2のadmission integration時に再確認します。

## 変更path

- `src/codex_bridge/workflows/interpreter.py`
- `src/codex_bridge/workflows/service.py`
- `src/codex_bridge/workflows/strict_reader_gate.py`
- `src/codex_bridge/workflows/strict_variable_runtime.py`
- `src/codex_bridge/workflows/strict_executor.py`
- `tests/test_workflow_saved_execution.py`
- `tests/test_workflow_d1_i7c1_durable_execution.py`
- `docs/workflow-design/phases/D1-I7B-variable-runtime-evidence.md`
- `docs/workflow-design/phases/D1-I7C1-durable-execution-evidence.md`
- `docs/workflow-design/phases/D1-implementation-plan.md`
