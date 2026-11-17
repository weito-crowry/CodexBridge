# Phase C5-P3C 隔離実データの読み取り専用GUI確認

このツールは、過去に成功した隔離Workflow A/B/Cの状態を製品のread-only projectionで読み、既存の`WorkflowManagementDialog`とExecution Graphへローカル表示します。Workflowの開始・再実行、Bridge/Worker起動、HTTP/MCP接続、定期pollingは行いません。

GUI表示前に、指定されたapproval marker、Temp直下の隔離root、A/B/Cのrequest row、公開snapshot、result、task projection、event pageを検証します。確認済みprovenanceの8候補/4読取可能DB/1完全一致rootと、A/B/Cの公開データ契約に一致しない場合は固定stage/codeを出して停止します。Cの要求台帳state `running`は保持し、製品projectionの公開state `succeeded`と画面上で区別します。

## 合成データの準備確認

`--prepare`は固定合成DTOとQt依存だけを確認します。実marker、Temp、SQLite、認証情報は読みません。GUIも生成しません。

```powershell
Set-Location D:\CodexBridge
& .\.venv\Scripts\python.exe -m scripts.phase_c5_isolated_visible_review --prepare
```

## 後日のユーザー目視確認

ChatGPTによるGitHubレビュー完了後、ユーザーがWindows nativeの対話PowerShellから、本人が閲覧を承認した過去試行IDを指定して実行します。placeholderは実行前に本人が置き換えてください。

```powershell
Set-Location D:\CodexBridge
& .\.venv\Scripts\python.exe -m scripts.phase_c5_isolated_visible_review --show --approval-id=<承認済みの過去ID> --size=regular
& .\.venv\Scripts\python.exe -m scripts.phase_c5_isolated_visible_review --show --approval-id=<承認済みの過去ID> --size=compact
```

`--show`はWindows nativeかつ`QT_QPA_PLATFORM`が`offscreen`以外の場合だけ表示します。画面を閉じると終了します。実runのIDやresultはそのローカル画面でのみ表示し、ログ・Git・スクリーンショットには保存しません。

目視チェックリスト：

- regular 900x760とcompact 720x600でダイアログが開き、通常のClose操作で終了できる。
- タイトルが読み取り専用でWorkflowを起動できないことを明示している。
- Workflow一覧からA、B、Cを選び、detail・Result・Execution Graph・event履歴が選択したrunに切り替わる。
- Bの2件のjob一覧と、Cの3回実行を含むGraphを確認する。
- Cでは「要求台帳 running」と「製品公開状態 succeeded」が別々に表示される。
- Graph/Resultタブ、node detail、event history、scroll/zoomが操作できる。
- Refreshは既にメモリに読み込んだ同じDTOを再描画し、再読込・外部通信をしない。
- New runは無効のままで、クリックや選択によっても有効にならない。

この文書やCLI出力だけでは可視GUIの人間受入を示しません。ユーザーが実際に画面を確認するまではGUI可視Gateは`NOT_RUN`です。また、本ツールは実Bridge GUI ControllerからHTTPを通る受入試験ではありません。
