# Phase B5-R2 統合受入結果

## 判定

**全体: BLOCKED**。実機runnerは指定の1回だけ起動したが、runner本体の`_main`に入る前にmodule importで終了した。隔離Bridge、MCP/Control API、Workflow、Codex jobは開始していない。自動再試行は行っていない。

- Issue: #144
- branch: `codex/workflow-phase-b5-r2-acceptance`
- 基準parent: `ce05ba54d9d47f9160a9b1a3f571f0239cad4983` (`codex/workflow-phase-b5-poll-hardening`)
- runner実行前のHEAD: `ce05ba54d9d47f9160a9b1a3f571f0239cad4983`
- 実行に使ったrunner SHA-256: `602d8830368493fe54c5f57033c9d69b8b959af23a0fcd0631b3a86f45c0e82b`
- evidence: [phase_b5_workflow_acceptance-54955c9765414fa0bd87b977d304239d.json](phase_b5_workflow_acceptance-54955c9765414fa0bd87b977d304239d.json)

## 実機runnerの結果

1回だけ実行したコマンド:

```powershell
uv run --no-sync --extra dev --extra workflows --extra console python scripts/phase_b5_workflow_isolated_smoke.py
```

Pythonがmodule import中に`ImportError`となり、`scripts`から`phase5e2c2_isolated_smoke`をimportできなかった。失敗stageは`module_import_before_main`で、`_main`未到達、runnerによるattempt ID生成なし、隔離root生成なし。raw tracebackは保存していない。

この結果から確定できるのは、今回の直接実行方法ではrunner importが失敗したことまで。script-path起動とnamespace importの組合せが原因候補だが、import解決の根因は未確認。将来の候補としてmodule形式の起動方法をレビューできるが、今回は試していない。ChatGPTレビューと別途指示があるまでrunnerを再実行しない。

`PARENT_HEAD`は承認済み基準parentに合わせて`ce05ba54d9d47f9160a9b1a3f571f0239cad4983`に更新した。B1〜B4製品ソース、B5テストコードは変更していない。

## Gate結果

| Gate | 判定 | 実施内容 |
|---|---|---|
| Gate 0: offline preflight・対象回帰 | **PASS（独立実行）** | 下記の環境確認、対象pytest、静的検査をrunner外で完了。runner自身のGate 0はimport失敗のため未開始。 |
| Gate 1: 隔離MCP・Control定義API | **NOT_RUN** | 一時root、token、port、Bridge、HTTP/MCP接続を作成・起動していない。 |
| Gate 2: saved execution・replay | **NOT_RUN** | 初回Workflow POST 0件、実Codex job 0件、同一ID MCP replay 0件、競合POST 0件。result/status観測は未実施。 |
| Gate 3: read-only Bridge再起動 | **NOT_RUN** | Bridge起動・再起動とも0件。 |

B5-R2のHTTP初回POST数、unique initial request ID数、MCP replay数、実Codex job数はすべて0。Control HTTP/MCP呼出し、worker/app-server起動、Bridge起動も0。process ownership・orphan確認は未実施であり、孤児数は未観測。

前回B5 attemptのremote outcomeは引き続き**UNKNOWN**。旧attemptの証跡・ID・run・session・Codex jobは今回読取、再利用、再送、再開、照会、復旧していない。

## Offline preflightと回帰

Windows native、Python 3.14.10、repository venvを確認。workflow/console依存とOWS DSLのruntime preflightはPASS。active Codex CLIは存在確認のみPASS。認証sourceはAVAILABLEのmetadata確認に留め、認証内容の読取・コピーはしていない。

B5専用unit test:

```powershell
uv run --no-sync --extra dev --extra workflows --extra console pytest -q tests/test_phase_b5_workflow_isolated_smoke.py
```

**29 passed / 0 skipped / 0 failed**。

runnerの`TEST_GROUPS`に対応するoffline回帰は次のとおり。すべてfake/unit対象で、実HTTP、Bridge、Codexを起動していない。

| 対象群 | Passed | Skipped | Failed / errors |
|---|---:|---:|---:|
| Workflow API/MCP/saved definitions | 107 | 2 | 0 |
| service/API/multijob/manager | 439 | 5 | 0 |
| Console/smoke/B5 runner | 260 | 1 | 0 |
| OWS regression | 155 | 0 | 0 |
| **合計** | **961** | **8** | **0** |

8件のskipはWindows symlink privilegeに関する既存条件によるもの。全pytestは実行していない。

## 静的検査

以下はすべてPASS:

- `uv run --no-sync --extra dev ruff check src scripts tests`
- `uv run --no-sync --extra dev ruff format --check src scripts tests`（296 files）
- `uv run --no-sync --extra dev mypy src`（128 source files）
- `uv run --no-sync --extra dev python -m compileall -q src/codex_bridge scripts`
- `uv lock --check`
- `git diff --check`

## 未検証事項と次の安全な手順

本結果はB5-R2統合受入PASSを示さない。実機上の隔離、MCP/Control契約、HTTP初回実行、needs_reconciliation観測推移、result/DBOS/job ledger/receipt/source mapping照合、成功時の同一ID replay、Qt表示、read-only restart、owned process cleanupはすべて未検証。

次回実機受入を検討する前に、ChatGPTレビューでrunnerのimport起動問題と起動コマンドを確認する。再実行には別途の指示を要し、実行する場合も新attempt・新規IDで最大1件のジョブという上限を維持する。#141の初回B5判定はBLOCKEDのまま。
