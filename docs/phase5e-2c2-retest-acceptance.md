# Phase 5E-2C2-R 受入結果

実施日: 2026-11-11

総合状態: **FAIL**。初回workflow POSTはHTTP 409で受理されず、request ledgerとlist readbackに`needs_reconciliation`が残りました。後続検証は未実施で、隔離Bridgeとstateを保持しています。

専用証跡: [phase5e-2c2-retest-evidence.json](phase5e-2c2-retest-evidence.json)

証跡SHA-256: `0e43c88c188ce8efd2aaf8b43872e1ea0e7d06ad9529c2668d57a0ea1ac6b745`

## 事前検査

- 開始branch/HEAD: `codex/dbos-poc` / `9d218d6bcbaee08af277a69cbd61d92af14c8d77`。
- 旧5E-2C2証跡の内容はretest前提と一致し、SHA-256は`5eb3e3bb66471bd1e7d18e83ce09e1dab8774752fc3ba4936787eb9bdea82844`。今回終了時も同じ値で、旧証跡は変更していません。
- retest証跡pathは未使用で、`--retest-once`だけが固定pathを選びます。新規記録には`retry_of`、retest理由、旧証跡SHA、新runの識別子が記録されます。
- focused回帰: **183 passed / 1 skipped**。skipはWindows symlink作成権限不足（`WinError 1314`）で、PASSには数えていません。
- Ruff check / format、mypy、compileall、`uv lock --check`、`git diff --check`: **PASS**。
- 実行前にWindows native、Pinned fixture、承認prompt hash、認証ファイルの存在/サイズ、Codex executableを確認しました。

## 隔離実行

- 新規workflow初回POST: **1回**。HTTP **409**、response statusは`needs_reconciliation`。
- read-only reconciliation: request ledger 1行が`needs_reconciliation`。workflow listもreadableで、対応するworkflow/runが各1件確認されました。
- read-only local state確認ではCodex job ledger fileとworker status fileは見つからず、Codex job/threadは確認されていません。ただしrequestはterminal stateではないため、最終的なjob状態は確定していません。
- replay POST: **0回 / NOT_RUN**。Console Qt実GET: **NOT_RUN**。Bridge restart readback: **NOT_RUN**。最大active数は未確定です。
- production side effect: **0**。本番Bridge、Console、AppServer、18000/18001、本番stateには接続していません。
- cleanupは`PRESERVED_ACTIVE_OR_UNKNOWN`。強制終了は0回。未知状態のため隔離Bridgeを通常shutdownせず、隔離temp/stateと限定コピーしたauthを保持しています。

## 判定と次の確認

初回POSTが409となった理由はこの実行では確定できません。request ledgerとworkflow listに同一の`needs_reconciliation`状態があるため、再POST、同一ID replay、追加job、shutdown、state削除は行っていません。以後の対応はこの証跡と保持中の隔離stateをレビューしてから判断してください。

前回の[5E-2C2受入記録](phase5e-2c2-acceptance.md)は史実として保持しています。
