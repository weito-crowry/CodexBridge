# Phase 5F-5 統合受入・隔離実機dogfood

## 結論

**Phase 5F-5の実機統合受入はBLOCKED。R2はBridge起動前にBLOCKED、R3はoffline regression failureのためNOT_RUN。A/B workflowは未送信で、製品動作をPASS判定していない。**

初回のcanonical証跡はstate rootの包含判定誤りでBridge起動前にBLOCKEDとなった。R2は専用証跡pathと新しい隔離rootで実行し、runnerのstate root変数結線誤りでBridge起動前にBLOCKEDとなった。R2原因を修正後、offline Workflow suiteで1件FAILしたためR3 runnerは起動していない。canonical/R2のworkflow POSTと、今回のR3 workflow POST・Codex job・capacity probe・replayは0回である。production checkout、既存Bridge、D:\CodexBridge-PoC、既定ポート18000/18001には接続・書込・停止をしていない。

初回証跡 [phase5f5-integrated-acceptance-evidence.json](phase5f5-integrated-acceptance-evidence.json) とR2証跡 [phase5f5-integrated-acceptance-r2-evidence.json](phase5f5-integrated-acceptance-r2-evidence.json) は変更していない。R2のrunner hashは `b88135f25e470517ca926946340dcf28ba0593e288aa109b576cafc64ca9d044`、source/headは`8e90588d1cf09d0a8a832fdd9cf70f7ef5e7ccf7`。今回の[phase5f5-integrated-acceptance-r3-evidence.json](phase5f5-integrated-acceptance-r3-evidence.json)は`status=NOT_RUN`、理由`offline_workflow_suite_failed_before_r3`。R3 runner hashは`f22fe6dbe8848a27cf49273fb34ec5f21d32757afa43277e0a24b73984a8cfa9`、gate記録のHEADは`ff2dab3538e290d1705552fbed251e3dc4777f87`。R2 rootは調査用に保持している。

## 受入項目と結果

| 項目 | 状態 | 実施結果 |
|---|---|---|
| Windows native / repository venv / workflow依存 | PASS | R2 runtime preflightとPython 3.14.10、依存versionを確認 |
| R2 auth copy、isolated root、A/B/C empty workspace、動的loopback port | BLOCKED | auth copy・root分離・port予約は確認。空state確認に誤ったpathを渡して停止 |
| R3 state root結線回帰 | PASS | env/config pathのtupleからisolated `LOCALAPPDATA\CodexBridge\workflows`を導出。config fileは空state判定対象から分離 |
| R3 isolated real acceptance | NOT_RUN | Workflow suiteで1件FAILしたため、R3 runner/Bridgeを起動していない |
| A: conditional items、alpha→gamma、summary 3/2/1 | NOT_RUN | initial workflow POSTなし |
| B: BRAVO single-job、A/B ledger・thread/turn分離 | NOT_RUN | initial workflow POSTなし |
| A/B同時active中のC=409 | NOT_RUN | A/B未受付のためprobeなし。実機では未観測 |
| DBOS、worker exit、result、認証付きGET/list/detail/result | NOT_RUN | Bridge起動なし |
| same-ID replay | NOT_RUN | 成功確定workflowなし。replayなし |
| lifecycle JSONLのID・timestamp・内容 | NOT_RUN | Bridge/workflowなし |
| Console offscreen readback | NOT_RUN | Bridge/APIなし |
| graceful shutdownとread-only restart | NOT_RUN | 今回所有するBridgeなし |

R2証跡の値は `workflow_post_total_attempts=0`、`unique_initial_attempts=0`、`accepted_initial_workflows=0`、`capacity_probe_attempts=0`、`same_id_replay_attempts=0`。A/B/Cのjob、worker snapshot、Bridge identityはなく、shutdown記録は空、既存processへの操作数は0。`orphan_processes=null` は今回Bridgeを起動しておらず、今回所有するprocessがなかったことを表す。R2のloopback reservationは56922/56923で、18000/18001は使っていない。

R2は `isolated_state_root_is_not_empty`、raw exit code 1でBLOCKED。標準出力・標準エラー・exit codeはOS temp capture `codexbridge-phase5f5-r2-run-8f8ccf55da364c028d58d04504b907c8` に保存した。隔離root labelは `codexbridge-phase5f5-rxcjf_hs`。実行前に試した直接script形式のlaunchはrunner import前に失敗し、stdout空・stderr `scripts` namespaceのImportError・exit 1を別capture `codexbridge-phase5f5-r2-af3821da9ee543b89c1b6c5a9e9535c6` に保存した。その後のmodule import確認はPASSし、R2 runner本体はmodule形式で一度だけ実行した。R3はrunner invocation 0回で、Bridge、workspace、port、request IDを作成していない。

## BLOCKED原因とR2停止判断

初回canonical attemptのBLOCKEDは `roots_are_separate(state_root, temp_root)` がstate rootの正しい包含関係を拒否したrunner誤判定だった。この包含判定は今回のbranchでは修正済みであり、専用testがtemp root配下stateを確認する。

R2では `_write_runtime_config()` が `(child_environment, config_path)` を返す一方、callerが `environment, state_root = ...` と受け取っていた。このため `state_root` はisolated `LOCALAPPDATA\CodexBridge\workflows` ではなく、既に作成済みの `bridge.toml` を指し、`_ensure_empty_isolated_state()` が `isolated_state_root_is_not_empty` で停止した。保持rootのread-only inspectionでは本来のisolated workflows state pathは存在せず、workflow dataもなかった。R2証跡の `isolated_auth_config_roots_and_empty_state` はNOT_RUNのため、empty-state checkをPASSとは扱わない。

R2はこのpreflight blockerで停止し、A/B POSTは0回だった。runnerでは`_runtime_paths_from_config_result()`が`(environment, config_path)`を明示的に分離し、`LOCALAPPDATA`をstrict resolveしてtemp root内であることを検証してから`CodexBridge/workflows`を導出する。config fileがtemp内にあること、state rootや親がsymlinkでないことも検証する。temp外LOCALAPPDATAとstate/workspace重複の回帰テストを追加した。R3実機はoffline regression failureで起動していない。

R3 runner evidence pathにはNOT_RUN gate記録を一度だけO_EXCLで作成した。以後この固定pathは上書きしない。R3のrunner/Bridge/ports/request IDs/POSTは0であり、別のdogfood attemptは行っていない。R1/R2 evidence、canonical evidence、過去run/temp rootは保持する。

## Offline regressionと静的検証

| 検証 | 結果 |
|---|---|
| Phase 5F-5 runner専用pytest | 14 passed |
| Workflow/Console workflow/Phase 5F smoke pytest | 527 passed, 1 failed, 5 skipped |
| Skip理由 | Windowsでsymlink作成権限が利用できないケースのみ |
| PoC OWS pytest | 155 passed, 0 skipped |
| Ruff check | PASS |
| Ruff format --check | PASS、338 files formatted |
| mypy src | PASS、121 source files |
| compileall src/scripts/tests | PASS |
| uv lock --check | PASS、66 packages resolved |
| git diff --check | PASS |

Workflow suiteは`tests/test_workflows_service.py::test_two_service_instances_enforce_capacity_two_atomically`でFAILした。2件がacceptedとなった一方、期待した`WorkflowConcurrencyConflict`数が1ではなく0だった。実際の3件目の例外種別はassertion outputに残らず、root causeはUNKNOWN。テストは再実行せず、service/製品sourceも変更していない。このFAILがR3実機をNOT_RUNにした。runner tests 14件、PoC OWS 155件、Ruff check/format、mypy 121 files、compileall、uv lock 66 packages、git diff checkはPASS。

### 2026-11-14 follow-up diagnosis (starting HEAD `8ccdaed4b7ad300e1f6c81b8c3afeabd6bc26b70`)

Focused testは初回1回と追加5回の計6回実行し、すべてPASSした。各回の結果は`WorkflowSnapshot` accepted 2件と`WorkflowConcurrencyConflict: workflow capacity is occupied` 1件で、順序だけが異なった。SQLite接続の`execute`を一時的に観測した6回ではSQLite例外は0件。診断用の一時テスト変更は戻し、製品sourceは変更していない。

関連するWorkflow・workflow Console・Phase 5Fの20テストファイルも一度実行し、`631 passed, 6 skipped`。skipはWindows symlink privilege不足によるもの。以前のFAILは再現していないが、その3件目の例外型・発生箇所は元のassertion出力に保存されておらず、今回の成功実行から遡って確定できない。したがってroot causeは依然UNKNOWNで、SQLite競合と断定せず、修正も加えていない。R3 runner、Bridge、workflow API POST、実Codex jobは今回もNOT_RUN/0件。R3 evidenceを含む過去証跡は不変。

## Phase 5F-1〜5F-4の既知範囲と今回の未確認点

- legacy issues loopの実Codex逐次3件成功は過去の受入結果であり、今回の証跡では再実行していない。
- Phase 5F-3 items条件分岐・結果集約とPhase 5F-4の独立2 workflowはofflineで確認済み。R4ではA/Bの2初回POSTと3実Codex jobの結果を隔離rootで確認したが、runner全体はworker identity照合でFAILしており、統合受入PASSとはしていない。
- R4でA/Bのjob ledger/result/DBOS/worker完了はread-only照合できた。A/Bが同時activeである条件下のC=409はNOT_OBSERVED、same-ID replay、effective API statusの最終照合、Qt readback、restart GETは実施していない。
- R3は`NOT_RUN`。offline容量競合テストのFAIL原因を確定できておらず、R3のPOST/worker/processは0件。
- 実クラッシュ復旧、あらゆるrace、無制限のfault injectionは今回の範囲外であり実施していない。
- Phase 5F-4 GitHub review PASSと基準commit `9cdb00d2332a781e33387fffc1d237804a6ee4a4` は今回の開始条件として扱った。今回のbranch/commit自体のレビュー結果を意味しない。

## 本番統合前 checklist

- [ ] Phase 5F-5差分をGitHubでレビューし、最終commitのCIを確認する。
- [ ] Workflow容量競合テストのFAIL原因を、追加実Codex POSTなしで特定する。
- [ ] BLOCKED/NOT_RUN証跡と隔離temp rootを保全する。canonical/R2/R3 pathは使用済みなので上書きしない。
- [ ] 実機受入を行う場合、A/B実行数、optional C=409の観測可否、結果digest、workflow/worker identity、lifecycle event counts、shutdown/restartを別々のPASS/NOT_OBSERVED/NOT_RUNとして確認する。
- [ ] 実機結果がPASSになるまでは、Phase 5F-5のitemsと独立workflow統合をproduction-readyと扱わない。
- [ ] 本番統合時は承認済みの通常review/CI/merge手順を使う。本作業ではPR作成、merge、rebase、tag、release、production反映を行わない。

## R4 隔離実機受入（2026-11-14、1回限定）

R4固定証跡 [phase5f5-integrated-acceptance-r4-evidence.json](phase5f5-integrated-acceptance-r4-evidence.json) はrunnerがO_EXCLで新規作成した。runner invocationは1回、終了コード1、`status=FAIL`、reason=`workflow_A_worker_identity_mismatch`。source/headは`fedbb995fc75f0c51dff41e70ea9de8164f991c0`、実行runner SHA-256は`053e67b83888e3d53dcb7d3e3835577e7e1863959429e1fba76274446d07f3c2`。stdout/stderr/raw exit codeはOS temp capture label `codexbridge-phase5f5-r4-capture-790cba0bbf664f5ebfbb7752813272cf` に保持した。R1/R2/R3/canonical evidenceは変更していない。

隔離preflightはPASSし、専用temp root labelは`codexbridge-phase5f5-lu1dbn2m`。新規workspace A/B/C、isolated auth copy、空state、GitHub MCP disabled、LAN disabledを確認した。専用loopback portはMCP 53870 / UI 53871で、18000/18001は使っていない。

| 項目 | R4結果 |
|---|---|
| Workflow A初回POST | 1回、HTTP 202 accepted。request rowは`running`。run/workflow identityとrequest digestはR4 evidenceに記録 |
| Workflow B初回POST | 1回、HTTP 202 accepted。request rowは`running`。Aとは独立したrun/workflow identityとdigest |
| Workflow C capacity probe | POST 0回、`NOT_OBSERVED`。同時active owned worker条件をrunnerのreadback window内で確認できなかった |
| 実Codex jobs | 合計3件。Aは2、Bは1。全件job ledger `SUCCEEDED`、receiptあり、distinct thread/turn identityは各workflow内で2/2と1/1 |
| DBOS / worker | A/BともDBOS `SUCCESS`、worker `completed`, exit 0。durable request rowは`running`のまま保存 |
| A items result | read-only result.json照合で`alpha=SUCCEEDED`, `skip=skipped`, `gamma=SUCCEEDED`、順序・期待値一致。SHA-256 `44053897ad72512a290199a98513cf70a5332893517d5361e70a555fc1d598d7` |
| B single-job result | read-only result.json照合でBRAVO結果一致。SHA-256 `5b7dd8ed14d73bcfdba4f023c2574c845ea6b65aa1f42b9ebd034136ecf009ae` |
| effective API status / final API result assertion | `NOT_RUN`扱い。runnerはworker identity照合で停止し、最終成功判定を完了していない。row=`running`を成功へ人工更新していない |
| same-ID replay / Console offscreen / restart GET | すべて`NOT_RUN`。再POSTや再起動はしていない |
| graceful shutdown / process | 今回所有するBridgeへshutdown HTTP 202、process reaped、forced terminationなし。証跡`orphan_processes=0`。終了後のPID/creation-time/owner照合でもBridge、appserver、workerの一致プロセス0 |
| lifecycle/redaction | formal runner checkは`NOT_RUN`。read-only scanでA/B各bridge logは`request_reserved=1`, `worker_launch_requested=1`, `worker_spawn_succeeded=1`, `worker_exited=1`, `workflow_state_changed=2`。各worker logは`worker_bootstrap=1`, `dbos_ready=1`, `workflow_started=1`, `result_persisted=1`。既知prompt本文、`final_text`、Authorization/Bearer markerは検出しなかった。tokenは停止後の比較値が保存されていないため、token値との照合は未確認 |

失敗箇所は`_verify_saved_workflow()`のworker identity確認である。worker identity JSONのPID/parent PIDはR4 process snapshotsの記録と一致したが、`image_path.resolve(strict=True)`はrunnerの`sys.executable.resolve(strict=True)`と一致せず、AでFAILとなった。read-only照合ではA/B双方のworker image pathはrunnerの`sys._base_executable`へ解決し、venvの`sys.executable`とは異なっていた。R4実行時点ではこの二段起動モデルの意味を確定できず、runner変更や製品コード変更は加えなかった。後続の読み取り専用postmortem結果を次節に記録する。

追加の読み取りでAのjob2件、Bのjob1件が成功状態でresult/receiptを保存したことは確認したが、runner全体の結果は**FAIL**のまま扱う。A/Bとも初回POST以外のworkflow POSTはなく、C、replay、restartの追加送信は0回。R4 rootは調査用に保持し、R3のNOT_RUNと以前のoffline capacity failureのUNKNOWNはR4実機結果と混同しない。Phase 5F-5のproduction統合は未承認であり、ChatGPTレビュー後に次の判断を行う。

### R4 postmortem: Windows venv launcher と実workerの識別

R4の保存済みprocess snapshot、`start-worker-identity.json`、両lifecycle JSONL、manifestを読み取り専用で突き合わせた。run/workflow/launch IDは各A/B内で一致し、identity recordのPID・parent PID・`creation_time_100ns`は該当process snapshotに対応していた。Bridge Popen PID `51852` の子に実Bridge PID `43504` があり、appserver PID `51372` とA/Bのworker launcherは実Bridge PIDの子だった。各実workerは自分のlauncherの子だった。

| Workflow | spawn/exitのlauncher PID | launcher parent | bootstrap/identityの実worker PID | 実worker parent | OS image |
|---|---:|---:|---:|---:|---|
| A | 41932 | 43504 | 42868 | 41932 | runner venvのbase Python |
| B | 49004 | 43504 | 27744 | 49004 | runner venvのbase Python |

この結果はvenv起動時の二段構造と整合する。Bridgeのspawn記録はvenv `sys.executable`で起動したlauncher PIDを記録し、worker identityはWindowsが報告した実OS worker PIDとそのbase interpreter image pathを記録する。R4のimage pathはrunnerの`sys._base_executable`と一致し、`sys.executable`とは異なっていた。したがってR4の`workflow_A_worker_identity_mismatch`はrunnerが二つの有効な実行ファイルとPIDを同一であることまで要求した誤判定であり、保存された結果から製品workerの不具合は示されていない。

R4 snapshot上のworker-entry processは4個で、A/Bそれぞれのlauncher+実workerの組だった。旧capacity判定は生process数が2であることとspawn PIDがworker PIDと同じであることを要求していたため、R4のC=409は`NOT_OBSERVED`となった。このpostmortem修正では、完全一致する許可済みvenv/base executable、PID・parent・creation time・run directory、owned Bridgeの子孫関係を各launcher/実workerで照合する。直接起動ではlauncherと実worker PIDが一致し、venv child起動では実workerのparent PIDがlauncher PIDになる。capacity probeはA/B各1組の完全な照合ができた場合だけCを送信し、4 process/2組の形を認識する。重複、未知process、別run identity、実行ファイル不一致は照合失敗とし、Cは送信しない。保存証跡には実行ファイルの絶対pathを含めず、照合結果とprocess ID情報だけを記録する。

今回のpostmortem修正はrunnerとrunner回帰テストに限定した。直接起動とvenv child起動、PID/parent/creation timeの不一致、第三者Python実行ファイル、owned Bridge外のlauncher、2 run/4 process、未知extra processをfake snapshotで検査する。R4 evidenceは変更しておらずstatusは引き続き`FAIL`。R4で未実施の最終API readback、same-ID replay、Console readback、restart readbackは未確認のままであり、今回もR5 runner、Bridge、実Codex POSTは実行していない。

| 今回のoffline検証 | 結果 |
|---|---|
| Phase 5F-5 runner tests | 29 passed in the latest combined run |
| Capacity concurrency focused + runner tests | 29 passed, 1 failed |
| Workflow/Console/Phase 5F関連suite | 557 passed, 5 skipped |
| PoC OWS suite | 155 passed |
| Ruff check / format --check | PASS |
| mypy src | PASS、121 source files |
| compileall src/scripts/tests | PASS |
| uv lock --check | PASS、66 packages resolved |
| git diff --check | PASS |

5件のskipはWindows symlink作成権限がない環境によるもの。直近の容量競合focused実行では、acceptedが2件だった一方で`WorkflowConcurrencyConflict`型の例外が0件としてassertionがFAILした。3件目のoutcome型・メッセージはこのtestのassertionに出力されず、原因は`UNKNOWN`。その直前の関連suiteでは同じtestを含めて557件PASSだったため、容量競合判定は実行間で不安定なままである。これはrunnerのプロセス識別修正とは独立した未解決事項であり、製品sourceは変更していない。静的検査とrunner testsはPASSだが、容量focused failureを含むためoffline検証全体は完全PASSではない。実機R5はChatGPTの差分レビュー後に別途判断し、今回実行していない。

## SQLite並行受付FAILの診断と限定修正（2026-11-14）

容量focused testに、assertion失敗時の3 outcomesそれぞれの型・状態・短い例外メッセージ・例外chain・stack frameと、SQL値を含めないSQLite stage traceを加えた。focused testは修正前に最大80回の順次実行を開始し、run 7で初回FAILを観測したため停止した。結果は最初の6回PASS、run 7 FAILである。

run 7の3 outcomesは、`WorkflowSnapshot(status=running)`が2件、`WorkflowServiceUnavailable`が1件だった。例外メッセージは`workflow request could not be persisted`、例外contextは`sqlite3.OperationalError: database is locked`。tracebackは`WorkflowService.start_workflow → _reserve_request → _connect_write`を示し、失敗frameは`_connect_write`内だった。SQLite traceでは該当接続の最後の実行stageが`PRAGMA_JOURNAL_MODE`（直前は`PRAGMA_BUSY_TIMEOUT`）だった。他接続ではDDL、`BEGIN_IMMEDIATE`、SELECT/UPDATE/INSERT、COMMITまで進んでいた。connect自体の失敗は0件。

**root causeは、各write connectionで毎回`PRAGMA journal_mode = WAL`を書き込み実行し、同じledgerへの並行初期化・write transactionと競合して`database is locked`になったことと確定した。** 失敗stageとSQLite exceptionは観測済み。どの特定connectionがlockを保持していたかまではtraceから特定していない。

修正は`service.py::_connect_write`のtransaction開始前に限定した。現在のjournal modeを読み、既にWALならmode変更PRAGMAを実行しない。未初期化時またはidempotent schema/connection初期化中にBUSY/LOCKEDが発生した場合だけ、初期化を最大5秒の有限deadlineで再試行する。初期化中のbusy timeoutは100ms、正常な接続を返す前に従来相当の5000msへ戻す。retry対象はSQLite BUSY/LOCKEDのみで、他のSQLite異常は即時にfail-closedする。`BEGIN IMMEDIATE`後、request rowのINSERT後、COMMIT後のretryはない。容量2、workspace検証、同ID replay順序、schema、API、worker、diagnostic logは変更していない。

| 検証 | 結果 |
|---|---|
| 修正後capacity focused | 30/30 PASS、run 31は実施せず |
| Workflow/Console/Phase 5F関連suite | 528 PASS, 5 SKIP |
| PoC OWS suite | 155 PASS |
| Phase 5F-5 runner tests | 29 PASS |
| Ruff check / format --check | PASS |
| mypy src | PASS |
| compileall src/scripts/tests | PASS |
| uv lock --check / git diff --check | PASS |

5件のSKIPはWindows symlink作成権限による。実Codex POST、Phase 5F-5 runner実行、隔離Bridge起動はすべて`NOT_RUN`。新規R5 evidenceは作成していない。canonical/R2/R3/R4 evidenceと未追跡の2件は保全した。
