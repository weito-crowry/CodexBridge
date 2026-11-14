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
- Phase 5F-3 items条件分岐・結果集約とPhase 5F-4の独立2 workflowはこれまでofflineで確認済み。**今回の実Codex統合では確認できていない。**
- 実機のA/B真の並走、3件目409、実result、DBOS/worker/lifecycle関連、同一ID replay、永続state restart readbackはすべて未確認。
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
