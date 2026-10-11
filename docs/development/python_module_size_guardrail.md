# Python モジュールサイズ guardrail

`src/**/*.py` と `scripts/**/*.py` の増大、および大きなテストモジュールのレビュー漏れを、依存追加なしで検知します。既存コードやテストの分割はこのルールの導入条件ではありません。

## 適用ポリシー

- Git index に登録された `src/`・`scripts/` 配下の全 `*.py`（`__init__.py` を含む）は、物理行数 1,500 以下かつ raw bytes 65,536 以下です。物理行数は raw blob の `bytes.splitlines()` の件数です。
- Git index に登録された `tests/**/test_*.py` は、50,000 bytes 以上で責務レビューが必要です。50000 bytes に達した時点で、承認済みの exact-byte baseline が必要になります。
- 100,000 bytes 以上のテストは `REVIEW` として強く表示します。これは自動的な分割要求ではなく、レビュー時の明示確認です。
- `tests/**/test_*.py` 以外の test support Python ファイルには、このサイズ gate を適用しません。

### 現在の例外 baseline

Issue #45 / PR #46 で承認した Phase 3 前の初期 baseline は、`main_window.py` が 2,588 physical LOC / 109,534 bytes、`test_bridge.py` が 76,784 bytes、`test_console_main_window.py` が 135,064 bytes、`test_console_widgets.py` が 60,460 bytes でした。これらは履歴値であり、以下の現在の承認値とは区別します。

Phase 3 では、本番 2 モジュールの抽出、Fake の移動、テストの移設を行いました（いずれも純粋移動）。

Phase 4D-1 では、Codex updater の状態と操作を `CodexUpdateController` へ抽出しました。

Phase 4D-2a では、Usage request orchestration を `UsageRequestCoordinator` へ抽出しました。

Phase 4D-2b では、turn別Usage snapshot stateとtimeout処理を `UsageSnapshotStore` へ抽出しました。

Phase 4D-2c では、通常Usageの状態・表示・SQLite履歴記録を `UsagePresentationController` へ抽出しました。

Phase 4E-1 では、Runtime状態に応じたUI表示更新を `RuntimeUiPresenter` へ抽出しました。`main_window.py` は 1,813 LOC / 74,115 bytes から 1,704 LOC / 69,370 bytes に縮小し、どちらも上限以下にはまだ達していません。

Phase 4E-2 では、tray構築・表示切替・activation・closeイベント処理を `TrayController` へ抽出しました。`main_window.py` は 1,704 LOC / 69,370 bytes から 1,626 LOC / 66,539 bytes に縮小しましたが、どちらも上限以下にはまだ達していません。

Phase 4E-3 では、thread操作・選択更新を `ThreadInteractionController`、diagnostics pane の表示と polling を `DiagnosticsPanelController` へ抽出しました。`main_window.py` は 1,626 LOC / 66,539 bytes から 1,520 LOC / 62,439 bytes に縮小しました。raw bytes は上限以下に達したため例外を削除し、physical LOC は上限を 20 行超えるため例外値を 1,520 に ratchet しました。新しい controller modules は両方とも production 上限内です。

Phase 4E-4 では、Console timer bootstrap を `timer_bootstrap.py` へ抽出しました。`main_window.py` は 1,520 LOC / 62,439 bytes から 1,479 LOC / 59,572 bytes となり、41 physical LOC を減らして、物理行数と raw bytes の両方で production 上限内に達しました。`main_window.py` の production exception を削除し、production module はすべて共通の 1,500 LOC / 65,536-byte 上限で検査します。新しい `timer_bootstrap.py` は 76 LOC / 3,756 bytes です。

今回、`tests/test_console_main_window.py` を UI/diagnostics・Codex update・thread interaction のテストとして残し、usage、history/SSE/approval、runtime/tunnel lifecycle の各テストを別モジュールへ移しました。共有テスト doubles と helper は `tests/console_main_window_test_support.py` に置いています。4つの test module を合わせた collection は、parameterized case を含めて 131 件です。この分割で旧ファイルの baseline を削除しました。

STEP 2 では、`tests/test_bridge.py` の 91 件を start/resume・permissions、history、notifications、requests の 4 モジュールに分割し、全モジュールを 50,000 bytes 未満にしました。`tests/test_bridge.py` の current baseline は削除しました。

| Path | Metric | Exact baseline | 理由 |
| --- | --- | ---: | --- |
| `tests/test_console_widgets.py` | raw bytes | 52,244 | Phase 4-C で context menu テストを移設。50 KB 超。 |

新しく上限超過した production module、未登録の 50,000 bytes 以上の test module、または baseline より増えたファイルは失敗します。別の例外を追加する場合は、checker 内の `POLICY` とこの文書に指定した JSON policy の両方を、レビュー対象の値・理由で一致させます。

## 実行方法

変更予定の Python ファイルを stage してから、repository root で実行します。

```powershell
uv run python scripts/check_python_module_size.py --check
```

結果の調査には read-only report を使えます。

```powershell
uv run python scripts/check_python_module_size.py --report
```

checker は `git ls-files --stage -z` で stage-0 entry を列挙し、各対象 blob を `git cat-file blob <object-id>` で読みます。作業ツリーの未 stage 内容や untracked file は対象値に混ぜず、CRLF の作業ツリー展開にも影響されません。unmerged entry、不正な index record/blob、空の対象集合、壊れた JSON、重複 JSON key、stale/unknown baseline は失敗します。

## Baseline の更新手順

1. `--report` で stage-0 の現在値を確認し、変更理由をレビューします。
2. baseline 対象が縮小した場合、production は縮小後の exact metric 値へ更新するか、その metric が hard limit 以下なら例外を削除します。test module は 50,000 bytes 以上に留まるなら exact byte 値へ更新し、50,000 bytes 未満なら baseline を削除します。
3. checker の `POLICY` と `docs/development/python_module_size_policy.json` を同時に更新します。JSON だけ、または checker だけの変更は失敗します。参照されなくなった path も両方から削除します。
4. 変更した対象ファイルを stage し、`--check` を再実行します。新しい overage の baseline を便宜的に引き上げて通す運用はしません。

GitHub Actions workflow はこの Issue では追加しません。runner の利用可否が確認された後に別途検討します。
