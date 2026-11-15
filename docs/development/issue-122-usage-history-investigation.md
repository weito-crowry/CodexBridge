# Issue #122: Usage History の間欠失敗と Windows 例外の切り分け

## 状態

2026-11-07 時点で **未解決**。過去の失敗 node と assertion は特定できたが、
今回の再実行では二重更新が再現せず、製品コード・テストコードは変更していない。
Windows 例外を Usage History の原因とする証拠も得られていない。
新しい安全ゲート、COM 初期化処理、例外抑止、テスト期待値の緩和は追加していない。

## 過去の実資料

### Usage History

2026-11-04、Python 3.14.10 の `uv run pytest -q` は
`4 failed, 1646 passed, 6 skipped in 12895.97s`。
JUnit 出力指定はなかった。実行記録は次のファイルに保存されている。

```text
D:/CodexData/.codex/sessions/2026/11/04/rollout-2026-11-04T13-16-01-01a1a4c5-5e48-7242-9e7d-9fb8d12c18b4.jsonl
```

#121 の調査で原出力をローカルの
`.runtime/issue-sequence-20261107/historical-py314-pytest-121.txt` に抽出済み。
失敗は次の一件である。

```text
tests/test_console_usage_history_window.py::test_show_refreshes_rolling_range_and_starts_auto_refresh_timer
tests/test_console_usage_history_window.py:90
assert calls == [True]
E assert [True, True] == [True]
```

テストは `refresh` を呼び出し記録関数に置き換え、`window.show()` と
`application.processEvents()` の後に回数を検査する。
この二重呼び出し自体は記録で確認できる。一方、呼び出し元、各時刻、Qt イベント種別、
timer 発火は原出力にない。失敗した node の実行時間も不明である。
DB、日時、オブジェクト寿命、前のテストの副作用を原因と断定できない。

Python 3.12.10 の既存記録
`tmp/pytest-bench-20261027_055804/pytest.log` と `junit.xml` では、
同じ node は **PASS、53.009 秒**。
この一件の成功は、3.12 で間欠失敗が起きない証明にはならない。
3.14 固有の回帰か既存の不安定性かは、現時点では判定不能である。
今回確認した最新 Issue 本文に変更はなく、コメントは 0 件だった。

### Windows 例外 `0x8001010d`

同じ 3.14 の原出力には次の stack がある。

```text
Windows fatal exception: code 0x8001010d
Current thread ...:
  File "D:\CodexBridge\tests\test_console_activity_collapse.py", line 31
    in test_activity_toggle_preserves_pending_approval_and_restores_splitter_widths
```

該当行は `window.show()`。C stack は `<cannot get C stack on this system>`。
出力は進捗 13% と 17% の間で、その後も suite は続行した。
Usage History の失敗は後続の約 69% の位置である。
時刻情報・native stack がないため、時間差の定量評価や例外を起こした DLL の特定はできない。

Microsoft の定義では、この値は `RPC_E_CANTCALLOUT_ININPUTSYNCCALL` で、
入力同期呼び出しの処理中に外向き呼び出しができない状態を表す。
これはエラーコードの意味であり、今回の COM 呼び出し元の特定ではない。
当該 Python stack には直接の COM 呼び出しがなく、Qt/Windows 内部や
外部コンポーネントのどれに由来するかは未確認。
Usage History の二重更新とは **因果未確認の別問題** として扱う。

参照: [Microsoft COM error codes](https://learn.microsoft.com/en-us/windows/win32/com/com-error-codes-3)。

## 今回の再実行

環境: Python 3.14.10 / PySide6 6.11.2 / pytest 9.1.1。
既存のサービス、設定、ユーザーターンは操作していない。

| 検証 | 結果 |
| --- | --- |
| 元 node 単独 | exit 0、1 passed、0.92s |
| 同一 pytest プロセスで元 node を 20 回 | exit 0、20 passed / 560 deselected、6.14s |
| Activity Collapse / Process Monitor / Usage History Window / shortcut | exit 0、53 passed、29.19s |
| show/hide/timeout 計測付きの同一 node 100 回 | exit 0、100 passed / 2800 deselected、24.25s |
| Usage History DB / Window / shortcut / Activity Collapse | exit 0、69 passed、28.27s |

100 回計測では、各 window に通常の show 一回、hide 一回だけが届いた。
`spontaneous=False` の show 100 件、hide 100 件、timer timeout 0 件。
計測中の例外出力もない。これは今回の条件での観測であり、過去の失敗原因の確定ではない。

繰り返しでは **ファイル引数** を重複させて `-k` で node を選ぶ。
同じ完全 node ID を引数で反復しただけでは、この環境の pytest は一件にまとめたため、
必ず JUnit の testcase 件数と標準出力の件数を確認する。

```powershell
$targets = @()
1..20 | ForEach-Object { $targets += 'tests/test_console_usage_history_window.py' }
uv run pytest --keep-duplicates @targets `
  -k show_refreshes_rolling_range_and_starts_auto_refresh_timer -q
```

対象ファイルの ruff check / format check、製品モジュールの mypy、対象 compileall は通過。
Full pytest は約 3 時間半の過去実行と今回の focused 方針に従い再実行していない。
GUI/EXE の手動確認は延期。詳細コマンド、JUnit、今回のイベント JSONL はローカルの
`.runtime/issue-sequence-20261107/` にある。

## 次の再現で必要な計測

`refresh(rolling=True)` の既存経路は `showEvent`、自動更新 timeout、Refresh ボタン。
このテスト自身は Refresh ボタンをクリックしない。
自動更新間隔は 60,000ms。Qt には通常と window system 由来の spontaneous show event がある。
どちらの更新経路で二回目が起きたかを、失敗する実行そのもので計測する必要がある。
60 秒 timer や spontaneous show を原因とする推測修正は行わない。

参照: [Qt QShowEvent](https://doc.qt.io/qt-6/qshowevent.html)。

ローカルの `diagnose_usage_122.py` は、実行中の pytest プロセスだけで
`UsageHistoryWindow.__init__` / `showEvent` / `hideEvent` をラップし、
次の情報を `.runtime/issue-sequence-20261107/usage-events-122.jsonl` に追記する。

- monotonic の経過時刻、pytest node ID、window の識別値。
- show/hide の `event.spontaneous()`。
- 自動更新 timer の active/remainingTime、timeout 通知。

100 回実行は上のコマンドの `1..20` を `1..100` にし、
`uv run pytest` を
`uv run python .runtime/issue-sequence-20261107/diagnose_usage_122.py` に置き換えた。
計測用 script と結果は一時調査資料で、製品や通常の pytest の動作を変えない。
清潔な checkout で再現計測する場合も、上記三箇所を process 内でラップして
本来の関数を一回ずつ呼び、timeout の追加接続は記録だけにする。
interval、イベント配信、`refresh` 回数、OS の COM 状態は変更しない。

二重更新を再現できたら、呼び出し元とタイミングから実装不具合とテストの前提不備を分類し、
確定した条件に対して RED/GREEN の回帰を作る。
Windows 例外が再発したら、Activity Collapse の該当 node を対象に native stack / DLL 名と
イベント時刻を取得する。現状の資料だけで Qt や Python の upstream 不具合とは断定しない。
どちらも現在の focused 実行を妨げていないため、修正優先順位は再現証拠を得た後に決める。
