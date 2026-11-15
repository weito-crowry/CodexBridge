# Issue #75 Windows通知の自動検証

2026-11-07。Python 3.14 / PySide6 6.11.2、既存Console通知を検証した。
実Windowsのトースト表示・音・OS上のクリックは今回実行していない。

## 修正した不具合

- 同一thread/turnに異なる終端Activityが到着すると、通知種別まで含む重複キーにより
  完了・失敗・中断をそれぞれ通知できてしまった。キーをthread/turnに統一した。
  最初に観測した確定終端を1回通知し、後着の終端で再通知しない。
  別thread、別turnの通知は抑止しない。
- App Server接続喪失時は、互換性のため従来の`state=failed`と`turn_failed` Activityが残るが、
  #124の`last_turn`では`unknown / inferred / connection_lost`である。
  このActivityに`details.reason=connection_lost`を付け、通知・重複キー登録から除外した。
  接続喪失から実際の終端を推測せず、後続のnative終端通知を受け取れる。
  `willRetry=true`のerror Activityも従来どおり終端通知にはしない。

## 自動検証の範囲

- completed / failed / interrupted、およびapproval / user inputの通知種別。
- 初回終端の3種類それぞれから、同一turnの同種・異種終端を再取得した際の重複抑止。
- 実Bridgeの接続喪失ActivityからControllerまでの経路で、通知0回の後、
  completed / failed / interruptedの各native終端が1回通知されること。
- status初期表示と再接続のsnapshotは通知・seen登録をしないこと。
  選択中threadとglobal SSEの両経路で同じActivityが来ても1回であること。
- SSE購読前の過去の完了・承認要求を新着として再配信しないこと。
  現存するpending requestも、起動時snapshotだけでは通知せず、新規live要求を通知対象とする。
- 完了のalways / inactive / off、承認・質問の個別無効化、設定保存直後の反映。
  QSettingsを別インスタンスで読み直し、同じ設定と通知抑止を確認。
- en / ja-JPで通知表示だけを翻訳し、種別・thread/turn/request ID・重複判定・
  設定の内部キーを維持。Activity.summaryとthread名の原文を翻訳しない。
- 実Qtの`messageClicked`信号で、単発／連続通知の後にConsoleを表示し、
  選択中threadを維持すること。通知やクリックだけではturn開始、回答、承認、
  再送、サービス制御のPOSTを発行しないこと。
- native通知APIが例外を返しても、後続threadの通知処理を継続できること。

テストの外部境界はFakeAppServer / FakeClient / FakeTrayで置換している。
実Codexターン・承認操作・サービス操作・ユーザー設定の変更は実行していない。

## 既存APIの制約と手動受入待ち

- 現行`QSystemTrayIcon.messageClicked()`は通知ごとのIDやthread IDを返さない。
  実装は単発・連続ともConsole前面表示のみで、対象threadへの正確な遷移は未対応。
  最新通知のthreadを推測して選択することはしない。WinRT等の新基盤は追加していない。
- Windowsでは追加beep設定を無効にし、OSがnative通知の音・集中モードを制御する旨を表示する。
  Consoleからnative通知音のdefault / noneを指定する機能はない。
- 通知内容は状態タイトルと既存の長さ制限済みActivity.summaryであり、thread名の取得を追加していない。
  今回の原文維持テストは本文と一覧名を対象とし、通知本文へthread名が載ることは保証しない。
- 通知APIの失敗はControllerがFalseを返して隔離するが、配信成功・OS抑止を判定できる
  receiptや専用失敗ログはない。OSで無効化された際の表示結果は自動テストから断定できない。
- 実Windows 11の通常表示／非アクティブ／最小化、音、集中モード、各通知種別、
  実Auto-reviewと手動要求の区別、再起動後の実画面での保持は手動受入待ち。
  Issueコメントで通知表示とクリック実績は報告済みだが、今回の自動検証は全件実機PASSを意味しない。

最新Issue本文とコメント:
[Issue #75](https://github.com/weito-crowry/CodexBridge/issues/75)。

## 検証結果

- `uv run --no-sync pytest tests/test_console_notifications.py tests/test_console_main_window_notifications.py tests/test_bridge_notifications.py tests/test_bridge_last_turn.py tests/test_console_last_turn.py tests/test_console_i18n.py tests/test_console_history_stream.py tests/test_activity.py tests/test_ui_api.py -q`
  — 161 passed、exit 0。
- 変更対象Python 5ファイルの`ruff check`、`ruff format --check`、`python -m compileall -q`
  — すべてexit 0。
- `uv run --no-sync mypy src/codex_bridge/bridge.py src/codex_bridge/console/notification_controller.py`
  — 2 source filesに問題なし、exit 0。
- `git diff --check` — exit 0。
- 修正前の終端競合回帰3件と接続喪失回帰1件は意図した不具合でFAILし、修正後PASS。
  追加テスト初回の6 FAILは期待翻訳2件と未選択tree item参照4件のfixture誤りであり、
  実カタログと一覧item検証へ修正後PASS。最初のformat checkも3ファイルFAIL、整形後PASS。
- 全pytestとGUI/EXE実機試験は本IssueではNOT RUN。関連範囲のみの実行結果である。
