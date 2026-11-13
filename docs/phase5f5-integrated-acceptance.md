# Phase 5F-5 統合受入・隔離実機dogfood

## 結論

**実機受入はBLOCKED。実Codex workflowは未送信で、Phase 5F-5の製品動作はPASS判定していない。**

Workflow/OWSのoffline regressionはPASSした。隔離runnerは一時ディレクトリ、3 workspace、認証コピー、動的loopback port、Bridge設定を用意した後、state rootの包含判定を誤って拒否した。Bridgeを起動する前に停止したため、A/B/Cのworkflow POST、Codex job、capacity probe、replayはすべて0回である。production checkout、既存Bridge、既定ポート18000/18001には接続・書込・停止をしていない。

証跡 [phase5f5-integrated-acceptance-evidence.json](phase5f5-integrated-acceptance-evidence.json) は最初に生成されたBLOCKED記録としてそのまま保存した。既存ファイル上書き禁止に従い変更しない。証跡の `runner_sha256` はBLOCKEDを記録した時点のrunner（`392af7e966dabea530eb594474452e3cb0ea9e0ea0f8230b6c422da88017424b`）を指す。誤判定を修正したrunnerはoffline検証したが、その版での実機受入は実施していない。

## 受入項目と結果

| 項目 | 状態 | 実施結果 |
|---|---|---|
| Windows native / repository venv / workflow依存 | PASS | runtime preflightを通過 |
| A/B/C workspace、isolated auth/state、動的loopback port | BLOCKED | 専用temp root内のstate rootを誤って重複と判定。Bridge起動前に停止 |
| A: conditional items、alpha→gamma、summary 3/2/1 | NOT_RUN | POSTなし |
| B: BRAVO single-job、A/B ledger・thread/turn分離 | NOT_RUN | POSTなし |
| A/B同時active中のC=409 | NOT_RUN | A/Bが未受付。probeなし。NOT_OBSERVEDとは別に未実施 |
| DBOS、worker exit、result、認証付きGET/list/detail/result | NOT_RUN | Bridge起動なし |
| same-ID replay | NOT_RUN | 成功確定前。replayなし |
| lifecycle JSONLのID・timestamp・内容 | NOT_RUN | workflowなし |
| Console offscreen readback | NOT_RUN | Bridge/APIなし |
| graceful shutdownとread-only restart | NOT_RUN | runner所有Bridgeなし |

実機runnerの保存値は `workflow_post_total_attempts=0`、`unique_initial_attempts=0`、`accepted_initial_workflows=0`、`capacity_probe_attempts=0`、`same_id_replay_attempts=0`。`bridge_initial` と `bridge_restart` はnull、shutdown記録は空、既存processへの操作数は0。`orphan_processes=null` はBridge起動前で終了確認の対象がなかったことを表す。

実行時のBLOCKED理由は `isolated_temp_root_overlaps_state_or_repository`、runnerのraw exit codeは1。標準出力・標準エラー・終了コードはOS tempのcapture `phase5f5-runner-capture-ac503bd15eab4330b1a65a71b190ef66` に保存した。隔離temp rootの識別子は `codexbridge-phase5f5-kcejntv1`。調査用rootは保持している。

## BLOCKED原因とrunner修正

state rootは隔離 `LOCALAPPDATA` の下、つまり専用temp root内に置く設計である。旧判定は `roots_are_separate(state_root, temp_root)` を要求していたため、この正しい包含関係を常に拒否していた。製品機能や外部環境の障害ではなくrunnerのpreflight誤判定である。

runnerを、state rootがtemp rootの配下にあり、workspace群およびrepositoryとは分離していることを確認する判定へ修正した。state rootがtemp root外にある場合やtemp/state rootがsymlinkの場合は引き続きBLOCKEDとする。専用テストで「temp root内のstate root」を確認した。**修正版runnerでの実機受入は未実施のため、この修正を実機PASSの根拠にしない。**

canonical証跡pathは今回のBLOCKED記録で使用済みである。既存証跡を変更・削除せず同じpathへ再実行しない。将来、追加受入を明示的に進める場合は、コードレビュー後に別の新規証跡pathと新しい隔離root/request IDsを割り当てて実施する。

## Offline regressionと静的検証

| 検証 | 結果 |
|---|---|
| Workflow/Console workflow/Phase 5F smoke pytest | 528 passed, 5 skipped |
| Skip理由 | Windowsでsymlink作成権限が利用できないケースのみ |
| PoC OWS pytest | 155 passed, 0 skipped |
| Phase 5F-5 runner専用pytest | 6 passed |
| Ruff check | PASS |
| Ruff format --check | PASS、338 files formatted |
| mypy src | PASS、121 source files |
| compileall src/scripts/tests | PASS |
| uv lock --check | PASS、66 packages resolved |
| git diff --check | PASS |

大きなworkflow/OWS suiteは実機前に実行した。state root包含判定修正後はrunner専用6 tests、全体Ruff、format、compileallを再実行してPASS。製品sourceは変更していないため、mypyとlock結果への影響はない。

## Phase 5F-1〜5F-4の既知範囲と今回の未確認点

- legacy issues loopの実Codex逐次3件成功は過去の受入結果であり、今回の証跡では再実行していない。
- Phase 5F-3 items条件分岐・結果集約とPhase 5F-4の独立2 workflowはこれまでofflineで確認済み。**今回の実Codex統合では確認できていない。**
- 実機のA/B真の並走、3件目409、実result、DBOS/worker/lifecycle関連、同一ID replay、永続state restart readbackはすべて未確認。
- 実クラッシュ復旧、あらゆるrace、無制限のfault injectionは今回の範囲外であり実施していない。
- Phase 5F-4 GitHub review PASSと基準commit `9cdb00d2332a781e33387fffc1d237804a6ee4a4` は今回の開始条件として扱った。今回のbranch/commit自体のレビュー結果を意味しない。

## 本番統合前 checklist

- [ ] Phase 5F-5差分をGitHubでレビューし、最終commitのCIを確認する。
- [ ] BLOCKED証跡と隔離temp rootを保全したまま、追加dogfoodを行うか判断する。行う場合は新しい証跡pathを確保し、修正版runnerのhashと実行結果を対応づける。
- [ ] 実機受入を行う場合、A/B実行数、optional C=409の観測可否、結果digest、workflow/worker identity、lifecycle event counts、shutdown/restartを別々のPASS/NOT_OBSERVED/NOT_RUNとして確認する。
- [ ] 実機結果がPASSになるまでは、Phase 5F-5のitemsと独立workflow統合をproduction-readyと扱わない。
- [ ] 本番統合時は承認済みの通常review/CI/merge手順を使う。本作業ではPR作成、merge、rebase、tag、release、production反映を行わない。
