# Phase C5-P0 Windows GUI状態表示の隔離オフライン受入

## 判定

- Gate 0: **PASS**。指定した既存回帰は195 passed、2 skipped、0 failed。Ruff、mypy、compileallもPASS。
- Gate 1: **FAIL**。Qt offscreenで900×760は指定サイズを維持したが、720×600はQtにより748×665へ拡大された。コンパクト境界条件を満たさない。
- C5-P0全体: **未PASS**。この結果をWindows可視GUIの目視、C5 Live、または実Codex成功と解釈しない。

## 作業基点と環境

- Branch: `codex/workflow-phase-c5-p0-gui-offline-acceptance`
- Base: `ac09377a04a29cbaa0b0a5d0bcccb1c8ce80f68b` (`codex/workflow-phase-c4-graph-view`)
- OS: Windows 11 `10.0.22631`
- Python: `3.14.10`; `pyproject.toml` の `requires-python = ">=3.11"` は変更なし。
- PySide6: `6.11.2`; Qt: `6.11.2`; DBOS: `3.4.0`
- OWS: `poc/ows/` のコードとfixtureは存在する。独立した `ows` Python import moduleは環境にない。fixture投影テストは実行済み。
- 既存 `.venv` を使い、`uv sync` / dependency updateは実行していない。`pyproject.toml` と `uv.lock` は変更していない。

## Gate 0: 既存offscreen回帰

全コマンドで `QT_QPA_PLATFORM=offscreen` を設定し、既存 `.venv\Scripts\python.exe -B -m pytest -q <file>` を実行した。

| テスト | 結果 |
|---|---:|
| `tests/test_console_workflow_graph_widget.py` | 8 passed |
| `tests/test_console_workflow_controller.py` | 20 passed |
| `tests/test_console_workflows_dialog.py` | 32 passed |
| `tests/test_console_api_client.py` | 20 passed |
| `tests/test_workflow_task_projection.py` | 61 passed, 1 skipped |
| `tests/test_workflow_task_api.py` | 9 passed |
| `tests/test_workflow_task_mcp.py` | 8 passed |
| `tests/test_workflow_node_events.py` | 20 passed, 1 skipped |
| `tests/test_workflow_graph.py` | 17 passed |

Skipは各1件、Windowsがsymlink作成を許可しなかったため。成功件数には含めない。`test_workflows_graph.py` は存在せず、実ファイル `test_workflow_graph.py` を実行した。

静的検査:

- `python -B -m ruff check src tests`: PASS (`All checks passed!`)
- `python -B -m ruff format --check src tests`: PASS (`295 files already formatted`)
- `python -B -m mypy src`: PASS (`132 source files`, no issues)
- `python -B -m compileall -q src`: exit 0
- `git diff --check` と `git diff --cached --check`: PASS

## Gate 1: Qt offscreen確認

Windows native環境上で `QT_QPA_PLATFORM=offscreen` を使った。PySide6 `QApplication`、実 `WorkflowManagementDialog` / `WorkflowExecutionGraphWidget`、メモリ内の偽DTO、およびcontroller試験の `FakeWorkflowClient` と同等のテスト専用fakeのみを使った。スクリーンショットは作成していない。

既存テストと `tests/test_phase_c5_gui_offline_acceptance.py` が確認した条件:

- Result tabの初期選択、Graph切替後の結果本文とworkflow選択維持、Graph非表示時のtasks/events GETなし。
- `for` の0 executionと複数execution、`switch` / `do` / `call`、1/2/3件のfake job execution、および `succeeded` / `running` / `failed` / `unknown` を別状態として表示。
- `needs_reconciliation` と `coverage=unavailable` / `journal_missing` を表示。0 executionのunknownをskipped/succeededへ置き換えない。
- fake DTOに含まれるjob IDとtimestampだけを表示すること。
- `@scope:` edgeを2つのvirtual nodeを介して表示し、直接edgeへ置換しないこと。
- 同一snapshotのitem identity維持、同じ構造でsnapshot versionが変わった時のnode item再利用、選択・zoom・event履歴の維持。別workflow/runへの変更ではevent/cursorを消去。
- 実際の3秒QTimer周期をfake clientで待機し、増分eventページを最大1ページ要求すること、cursorとhas_moreの更新。
- stale selection/token/close、403/503、manual refresh/start優先をFakeWorkflowClientによる既存controllerテストで確認。
- 300 nodes / 1100 edgesのfake snapshotを上限256 nodes / 1024 edgesに制限し、元countの省略表示を維持。
- prompt/token/path/thread/turnの未許可値をgraph表示へ漏らさないこと。

新規受入テストの最終実行:

```text
QT_QPA_PLATFORM=offscreen .venv\Scripts\python.exe -B -m pytest -q tests/test_phase_c5_gui_offline_acceptance.py
1 failed, 3 passed in 3.94s
```

再現した問題:

- `WorkflowManagementDialog.show()` 後、`resize(QSize(900, 760))` は900×760を維持。
- 続けて `resize(QSize(720, 600))` を指定すると実サイズは748×665になり、受入テストが失敗した。よって720×600条件下の子widget geometry確認には到達していない。
- 改善案: compactサイズを維持できるよう、dialog全体のminimum-size要因を見直し、Graphと詳細paneの幅・高さを縮めるか詳細領域をscroll可能にする。`workflow_graph_widget.py` のgraph view minimum width 360pxは確認候補だが、Qt全体のminimum size hintが原因という点は未確定。
- この問題は製品ソース修正なしで再現テストとともに記録した。仕様に従いPASS扱いしない。

テスト実行時の後始末:

- 3秒timerテストの最初の単独実行はassertion通過後、pytest終了時にGC中のWindows fatal exception `0xc0000374` で終了した。
- controller既存fixtureと比較してDeferredDelete配送が不足していたため、新テストの後始末に `deleteLater` と `QCoreApplication.sendPostedEvents(..., DeferredDelete)` を加えた。その後、timer単独テストは `1 passed`、新規ファイル全体は上記のとおり `1 failed, 3 passed` で正常終了した。

## 実行境界と未検証項目

- 試験中の外部ネットワーク送信: 0。Workflow HTTP APIは `httpx.ASGITransport` によるin-processテスト、MCPはAnyIO memory streamによるin-processテスト。live BridgeへのHTTP request/POSTは0。
- 実Codex job: 0。live MCP / CLI Codex発行: 0。実Bridge起動・停止: 0。本番port `18000/18001`、production state、実session/job、既存run、旧B5 UNKNOWN outcomeへの接続・照会・変更・再送: 0。
- 新規テストのpayload、job ID、timestampはすべて固定fake値。実session ID、認証情報、raw prompt、production absolute pathを作業成果に含めていない。
- 既存の未追跡 `.codexbridge-staging/` と `tmp/` は保持する。
- Qt offscreen結果はWindows可視GUIを人が目視した証拠ではない。目視確認と画面撮影は未実施。

## 後続C5 Live受入の分離案

後続Live受入は今回の対象外で、別の明示承認が必要。実施する場合は各job数を独立したworkflow/run・隔離workspace/stateに分ける。

1. 独立run A: 1 Codex job
2. 独立run B: 2 Codex job
3. 独立run C: 3 Codex job

承認時に明示する事項は、3 run全体の実Codex開始数上限、不明結果時の再送禁止、本番portおよび既存session/jobを使わないこと。今回これらのLive処理は行っていない。
