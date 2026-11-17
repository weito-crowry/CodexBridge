# Phase C5-P3A GUI visible review harness

このハーネスは、既存の本物の `WorkflowManagementDialog` と
`WorkflowExecutionGraphWidget` を、固定された合成データだけで表示します。画面に
「合成データ・実Workflow未実行」と表示されます。Bridge、Worker、Controller、API client、MCP、
Workflow admission、Codex jobには接続しません。Refresh は同じfixtureを再適用します。
外部polling timerはありません。

## 事前確認

PowerShellでリポジトリの仮想環境から準備確認を行います。これは依存・fixture・UI classの
確認だけを行い、QApplicationやウィンドウを生成しません。

```powershell
Set-Location D:\CodexBridge
& .\.venv\Scripts\python.exe -m scripts.phase_c5_gui_visible_review --prepare
```

## 可視画面を開く

以下はユーザーが目視確認するときに、**一つずつ**実行します。`regular` は900x760、
`compact` は720x600で開始します。Qtが `offscreen` に設定されている環境では可視起動を拒否し、
環境変数を変更しません。

```powershell
Set-Location D:\CodexBridge
& .\.venv\Scripts\python.exe -m scripts.phase_c5_gui_visible_review --show --size=regular
```

```powershell
Set-Location D:\CodexBridge
& .\.venv\Scripts\python.exe -m scripts.phase_c5_gui_visible_review --show --size=compact
```

このハーネスは合成データのみです。過去の実Codex jobやWorkflow実行結果を読み込んだり、
Execution graphを実runに結合したりしません。

## 目視チェックリスト

- [ ] 通常サイズで日本語の「合成データ・実Workflow未実行」が分かる。
- [ ] A、B、C、Unknown、Needs reconciliation、Failed、Legacyの7件を選択できる。
- [ ] Execution graphタブでfor 0回/複数回、switch、do、call、entry/exitの仮想edgeを確認できる。
- [ ] node一覧からnodeを選び、node詳細とイベント履歴を確認できる。
- [ ] ResultタブとExecution graphタブを切り替えられる。
- [ ] Refresh後も同じ選択node、グラフ倍率、スクロール位置が保たれる。
- [ ] New runはdisabledで、Refreshや結果表示が外部接続を行わない。
- [ ] compactサイズでも上部一覧、タブ、詳細、固定下部操作行へ到達できる。
- [ ] Closeで画面が終了する。

報告時は確認したサイズ、チェック項目、表示上の問題を記録してください。Qt offscreenの
自動テストは可視GUIの目視確認ではありません。ユーザーが実際に表示して確認するまで、
Phase C GUI Gate1は `NOT_RUN` です。
