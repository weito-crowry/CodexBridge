# Python モジュールサイズ guardrail

`src/**/*.py` と `scripts/**/*.py` の増大、および大きなテストモジュールのレビュー漏れを、依存追加なしで検知します。既存コードやテストの分割はこのルールの導入条件ではありません。

## 適用ポリシー

- Git index に登録された `src/`・`scripts/` 配下の全 `*.py`（`__init__.py` を含む）は、物理行数 1,500 以下かつ raw bytes 65,536 以下です。物理行数は raw blob の `bytes.splitlines()` の件数です。
- Git index に登録された `tests/**/test_*.py` は、50,000 bytes 以上で責務レビューが必要です。50000 bytes に達した時点で、承認済みの exact-byte baseline が必要になります。
- 100,000 bytes 以上のテストは `REVIEW` として強く表示します。これは自動的な分割要求ではなく、レビュー時の明示確認です。
- `tests/**/test_*.py` 以外の test support Python ファイルには、このサイズ gate を適用しません。

### 現在の承認済み baseline（Phase 4C-3a 変更後）

Issue #45 / PR #46 で承認した Phase 3 前の初期 baseline は、`main_window.py` が 2,588 physical LOC / 109,534 bytes、`test_bridge.py` が 76,784 bytes、`test_console_main_window.py` が 135,064 bytes、`test_console_widgets.py` が 60,460 bytes でした。これらの履歴値は以下の現在の承認値と区別します。本表は統合ブランチの値であり、`origin/main` の現行値を示すものではありません。

Phase 3 では、本番 2 モジュールの抽出、Fake の移動、テストの移設を行いました（いずれも純粋移動）。

| Path | Metric | Exact baseline | 理由 |
| --- | --- | ---: | --- |
| `src/codex_bridge/console/main_window.py` | physical LOC | 2,046 | Phase 4C-3a でBridge runtime observationを抽出。引き続きLOC上限超過。 |
| `src/codex_bridge/console/main_window.py` | raw bytes | 84,598 | Phase 4C-3a のBridge runtime observation抽出後。引き続きbyte上限超過。 |
| `tests/test_bridge.py` | raw bytes | 75,927 | Phase 3 で Fake を移動。50 KB 超。 |
| `tests/test_console_main_window.py` | raw bytes | 132,552 | Phase 3 でテストを移設。100 KB 超のため REVIEW 通知による明示確認が必要。 |
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
