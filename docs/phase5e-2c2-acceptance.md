# Phase 5E-2C2 受入結果

実施日: 2026-11-11

総合状態: **BLOCKED**。隔離実行の2 jobは成功しましたが、後続の冪等replay・Bridge restart・Console Qt実GETは未実行です。

## Consoleと自動検証

- 新規実行ダイアログは1件を初期値とし、2件を明示選択した場合のみ`max_live_codex_jobs: 2`を送ります。同一ID・同一payloadでのretry挙動を維持しました。
- 2-job workflowの詳細に、最大2件のサニタイズ済み読み取り専用job行を表示します。日英表示を追加しました。
- E2E runnerは既存の固定fixtureから1件目を読み、2件目の固定promptは実行時環境変数から取得してSHA-256を照合します。新規ファイルにはprompt本文を保存しません。
- Console、C1 API、C2 runner、multijobのfocused suite: **193 passed / 1 skipped**。skipはこのWindowsアカウントでsymlinkを作成できないためです（`WinError 1314`）。
- Ruff check/format、mypy、compileall、`uv lock --check`、`git diff --check`: **PASS**。

## 隔離Windows実行

専用証跡: [phase5e-2c2-evidence.json](phase5e-2c2-evidence.json)

- Workflow API POSTは1回だけで、隔離loopback port `58885` / `58886`に`202`で受理されました。
- Codex job/threadは各2件、順次実行、同時active最大 **1**。job 1は`4`、job 2は`5`を返しました。
- 2行の台帳はともに`SUCCEEDED`、DBOSは`SUCCESS`、workerはexit 0でした。読み取り専用Service照合でlist/detail/result、ordered jobs、job/thread/turn IDを台帳と照合しました。
- 両jobのcwdは隔離workspaceと一致しました。本番Bridge・本番port/state・`poc/ows/.state`は使っていません。production side-effectは**0**です。
- Bridgeは通常shutdown（HTTP 202）し、所有する11 childを強制終了なしでreapしました。runnerの初回temp削除は失敗したため、照合後に固有prefixと親を確認して隔離tempを削除しました。

## BLOCKEDの理由と未実施項目

runnerのdefinition hash照合が、保存済みworkflow textに改めて改行を加えていました。この誤判定により、2 job完了後、同一ID replay、同じstateでのBridge restart、Console Qtの実GET確認へ進む前に停止しました。保存済みfile bytesはmanifest hashと一致することを読み取り専用で確認し、hash計算を修正して回帰テストを追加しました。

今回の実Codex runは再実行していません。追加のworkflow POSTもreplay POSTも行っていません。evidenceではreplay、restart、Console Qt実GETを`NOT_RUN`としています。fake/Qt回帰テストはPASSです。

旧Phase 5E-0 evidenceは変更していません。
