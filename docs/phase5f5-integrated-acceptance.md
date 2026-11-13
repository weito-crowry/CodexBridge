# Phase 5F-5 統合受入・隔離実機dogfood

## 結論

**Phase 5F-5の実機統合受入はBLOCKED。A/B workflowは未送信で、製品動作をPASS判定していない。**

初回のcanonical証跡はstate rootの包含判定誤りでBridge起動前にBLOCKEDとなった。今回のR2は専用証跡pathで新しい隔離rootを用いて実行したが、別のrunner変数結線誤りを検出し、Bridge起動前にBLOCKEDとなった。両方の実機attemptでworkflow POST、Codex job、capacity probe、replayは0回である。production checkout、既存Bridge、D:\CodexBridge-PoC、既定ポート18000/18001には接続・書込・停止をしていない。

初回証跡 [phase5f5-integrated-acceptance-evidence.json](phase5f5-integrated-acceptance-evidence.json) は変更していない。今回のR2証跡は [phase5f5-integrated-acceptance-r2-evidence.json](phase5f5-integrated-acceptance-r2-evidence.json) に新規作成した。R2のrunner hashは `b88135f25e470517ca926946340dcf28ba0593e288aa109b576cafc64ca9d044`、source/headは `8e90588d1cf09d0a8a832fdd9cf70f7ef5e7ccf7`。R2 rootは調査用に保持している。

## 受入項目と結果

| 項目 | 状態 | 実施結果 |
|---|---|---|
| Windows native / repository venv / workflow依存 | PASS | R2 runtime preflightとPython 3.14.10、依存versionを確認 |
| R2 auth copy、isolated root、A/B/C empty workspace、動的loopback port | BLOCKED | auth copy・root分離・port予約は確認。空state確認に誤ったpathを渡して停止 |
| A: conditional items、alpha→gamma、summary 3/2/1 | NOT_RUN | initial workflow POSTなし |
| B: BRAVO single-job、A/B ledger・thread/turn分離 | NOT_RUN | initial workflow POSTなし |
| A/B同時active中のC=409 | NOT_RUN | A/B未受付のためprobeなし。実機では未観測 |
| DBOS、worker exit、result、認証付きGET/list/detail/result | NOT_RUN | Bridge起動なし |
| same-ID replay | NOT_RUN | 成功確定workflowなし。replayなし |
| lifecycle JSONLのID・timestamp・内容 | NOT_RUN | Bridge/workflowなし |
| Console offscreen readback | NOT_RUN | Bridge/APIなし |
| graceful shutdownとread-only restart | NOT_RUN | 今回所有するBridgeなし |

R2証跡の値は `workflow_post_total_attempts=0`、`unique_initial_attempts=0`、`accepted_initial_workflows=0`、`capacity_probe_attempts=0`、`same_id_replay_attempts=0`。A/B/Cのjob、worker snapshot、Bridge identityはなく、shutdown記録は空、既存processへの操作数は0。`orphan_processes=null` は今回Bridgeを起動しておらず、今回所有するprocessがなかったことを表す。R2のloopback reservationは56922/56923で、18000/18001は使っていない。

R2は `isolated_state_root_is_not_empty`、raw exit code 1でBLOCKED。標準出力・標準エラー・exit codeはOS temp capture `codexbridge-phase5f5-r2-run-8f8ccf55da364c028d58d04504b907c8` に保存した。隔離root labelは `codexbridge-phase5f5-rxcjf_hs`。実行前に試した直接script形式のlaunchはrunner import前に失敗し、stdout空・stderr `scripts` namespaceのImportError・exit 1を別capture `codexbridge-phase5f5-r2-af3821da9ee543b89c1b6c5a9e9535c6` に保存した。その後のmodule import確認はPASSし、実際のR2 runner本体はmodule形式で一度だけ実行した。

## BLOCKED原因とR2停止判断

初回canonical attemptのBLOCKEDは `roots_are_separate(state_root, temp_root)` がstate rootの正しい包含関係を拒否したrunner誤判定だった。この包含判定は今回のbranchでは修正済みであり、専用testがtemp root配下stateを確認する。

R2では `_write_runtime_config()` が `(child_environment, config_path)` を返す一方、callerが `environment, state_root = ...` と受け取っていた。このため `state_root` はisolated `LOCALAPPDATA\CodexBridge\workflows` ではなく、既に作成済みの `bridge.toml` を指し、`_ensure_empty_isolated_state()` が `isolated_state_root_is_not_empty` で停止した。保持rootのread-only inspectionでは本来のisolated workflows state pathは存在せず、workflow dataもなかった。R2証跡の `isolated_auth_config_roots_and_empty_state` はNOT_RUNのため、empty-state checkをPASSとは扱わない。

R2はこのpreflight blockerで停止した。A/B POSTを送らず、R2を再実行していない。局所修正候補はcallerで`state_root`を`Path(environment["LOCALAPPDATA"]) / "CodexBridge" / "workflows"`から導出することと、その戻り値結線をrunner testで固定すること。今回の一度限りの実機attempt後に追加dogfoodはしていない。初回canonical evidence、R2 evidence、過去run/temp rootは保持する。

## Offline regressionと静的検証

| 検証 | 結果 |
|---|---|
| Workflow/Console workflow/Phase 5F smoke pytest | 528 passed, 5 skipped |
| Skip理由 | Windowsでsymlink作成権限が利用できないケースのみ |
| PoC OWS pytest | 155 passed, 0 skipped |
| Phase 5F-5 runner専用pytest | 11 passed |
| Ruff check | PASS |
| Ruff format --check | PASS、338 files formatted |
| mypy src | PASS、121 source files |
| compileall src/scripts/tests | PASS |
| uv lock --check | PASS、66 packages resolved |
| git diff --check | PASS |

Workflow/OWS suite、runner tests、静的検証はR2実機runner起動前にPASSした。Ruff format整形後にrunner専用11 tests、Ruff check/format、mypy、compileall、uv lock、git diff checkを再実行してPASS。今回の差分に製品source変更はない。

## Phase 5F-1〜5F-4の既知範囲と今回の未確認点

- legacy issues loopの実Codex逐次3件成功は過去の受入結果であり、今回の証跡では再実行していない。
- Phase 5F-3 items条件分岐・結果集約とPhase 5F-4の独立2 workflowはこれまでofflineで確認済み。**今回の実Codex統合では確認できていない。**
- 実機のA/B真の並走、3件目409、実result、DBOS/worker/lifecycle関連、同一ID replay、永続state restart readbackはすべて未確認。
- 実クラッシュ復旧、あらゆるrace、無制限のfault injectionは今回の範囲外であり実施していない。
- Phase 5F-4 GitHub review PASSと基準commit `9cdb00d2332a781e33387fffc1d237804a6ee4a4` は今回の開始条件として扱った。今回のbranch/commit自体のレビュー結果を意味しない。

## 本番統合前 checklist

- [ ] Phase 5F-5差分をGitHubでレビューし、最終commitのCIを確認する。
- [ ] callerの`state_root`をisolated `LOCALAPPDATA\CodexBridge\workflows`へ結線し、その対応をrunner testで固定する。
- [ ] BLOCKED証跡と隔離temp rootを保全する。R2 pathは使用済みなので、別の受入を行う場合はレビュー後に別pathと新しいIDs/rootを割り当て、R2を再利用しない。
- [ ] 実機受入を行う場合、A/B実行数、optional C=409の観測可否、結果digest、workflow/worker identity、lifecycle event counts、shutdown/restartを別々のPASS/NOT_OBSERVED/NOT_RUNとして確認する。
- [ ] 実機結果がPASSになるまでは、Phase 5F-5のitemsと独立workflow統合をproduction-readyと扱わない。
- [ ] 本番統合時は承認済みの通常review/CI/merge手順を使う。本作業ではPR作成、merge、rebase、tag、release、production反映を行わない。
