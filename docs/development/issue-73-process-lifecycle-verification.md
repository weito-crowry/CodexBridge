# Issue #73: Windows プロセス終了・残存追跡の自動検証

2026-11-07、Python 3.14.10 / Windows ネイティブ環境で実施。

## 確認した現仕様

- App Server の `process_id` は不透明IDとして表示し、Windows PIDへ数値変換しない。
- ローカルUIが提供する App Server OS PIDを起点とする子孫走査と、時刻・実行ファイル・argv・候補一意性からコマンドを照合する。
- 検証済み PID と `create_time` を証拠として保持し、終了後も観測済み子プロセスを追跡する。親終了とコマンド結果、残存子の稼働状態は別に扱う。
- 読み取り専用。stdin送信、attach、kill等のUIは追加しない。
- 最新Issueコメントでは実際のPowerShell `$PID` とGUIのWindows PID一致、子プロセス表示は過去に確認済み。終了後のOS状態はそのコメント時点では未確認だった。

## 確定した不具合と最小修正

所有Windows親子プロセスを使った追加テストで、空白のない絶対exeパスの
コマンドが `Unverified: no matching Codex child` になった。
原因は候補の軽量フィルターが絶対パスを、列挙で得るbasename
（例: `python.exe`）と比較していたこと。
既存のquoted PowerShellパステストは `Program Files` の空白によって
別分岐に入り、このケースを検出しなかった。

軽量フィルターだけをbasename比較へ変更。後段の完全な実行ファイルパス・argv・
作成時刻・親子関係・一意性確認は維持した。
空白のないPowerShell絶対パスの回帰テストでRED→GREENを確認し、
basenameが一致しても異なるディレクトリのexeは関連付けないテストも追加した。

実測用Pythonは `sys._base_executable` をresolveして起動する。
この環境のuvの `cpython-3.14-...` は `cpython-3.14.10-...` へのエイリアスであり、
未resolveのargvとOSのexeは異なる。その場合の厳密照合を緩める変更はしていない。

## 自動検証

`tests/test_console_process_lifecycle.py` は実際のpsutilでテスト所有親子だけを照会する。
App Server本体の代わりにpytestプロセスを既知のOS起点とし、
合成したCommandDescriptorでコマンド側の情報を渡す。実Codexターンの試験ではない。

1. 親子が稼働中: 実PID、親PID、作成時刻、exe/argv、プロセス名とRunningを確認。
2. 親をファイル合図で自然終了: exit 0/completed と exit 7/failed の両方で、
   親Exited、観測済み子Running、元のPID+作成時刻の保持を確認。
3. 子をファイル合図で自然終了: 同じ子のExitedを確認。
4. terminate/kill/send_signal/suspend/resumeへの呼出はテストを失敗させる。
   正常系・cleanupともにシグナル送信をせず、ファイル合図で終了を待つ。
   失敗時も所有プロセスだけを解放する。スクリプトには30秒の自律終了期限もある。

SSE→ActivityPane統合テストは、成功/失敗/中断で同じthread/turn/itemへ結果と出力が届き、
重複完了通知で項目が増えず、欠落item IDの出力が既存項目に混入しないことを確認する。
既存fakeテストは親/子PID再利用、anchor変更、取得不可、走査不完全、
候補曖昧、読み取り専用表示・コピー等をカバーする。

最終の関連一括:

```powershell
uv run --no-sync pytest tests/test_console_process_lifecycle.py tests/test_console_process_inspection.py tests/test_console_process_monitor.py tests/test_console_process_sse_integration.py tests/test_console_command_output.py tests/test_console_sse.py tests/test_console_activity_pane_widgets.py -q
```

結果: **100 passed in 5.71s**、exit 0。
所有Windowsライフサイクル+SSEの5件は一括実行に加え2回再実行し、いずれもPASS。
対象4 PythonファイルのRuff check/format checkとcompileall、
inspection/monitor/SSEのmypy（3 source files）、git diff --checkもexit 0。
初回Ruffの1行長超過・format差分は整形後に再検証して解消した。

## 残る手動確認

- 新EXE/Consoleで実Codexコマンドの終了後、残存子と最終状態を目視確認する作業は延期。
- 実ターンの中断やネットワーク切断をこの試験で発生させていない。
- SSEの成功/失敗/中断は合成イベント、Windowsの終了/残存は所有プロセスでの実測。
- 全pytest・無関係なMainWindow全域・EXE再ビルドはこのIssueでは実施していない。
  製品コードに変更があるため、配布EXEへ含めるには再ビルドが必要。
- ユーザーの実行中Console/Bridge/Tunnel/ターンを停止・変更していない。

Issue: <https://github.com/weito-crowry/CodexBridge/issues/73>
