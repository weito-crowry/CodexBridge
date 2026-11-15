# Issue #121: Process Monitor の更新要求が失われる競合

## 過去の失敗と分類

Python 3.12.10 の既存記録
`tmp/pytest-bench-20261027_055804/pytest.log` / `junit.xml` と、
2026-11-04 の Python 3.14.10 移行時の Full pytest 出力は、次の同一 node で失敗した。

```text
tests/test_console_process_monitor.py::test_snapshot_and_live_events_merge_once_and_keep_command_and_os_status_separate
tests/test_console_process_monitor.py:96: AssertionError
assert _wait(application,
    lambda: "Command status: interrupted · exit 1" in widget.process_text.toPlainText(),
    timeout=5.0)
```

3.12 の JUnit は 1625 tests / 2 failures / 13808.564 秒で、当該 node は
34.193 秒だった。もう1件は SSE integration の別 assertion である。
3.14 の結果は `4 failed, 1646 passed, 6 skipped in 12895.97s`。
3.14 のコマンドは `uv run pytest -q` で JUnit 出力指定はなく、当時の実行出力を確認した。
他の3件は Console entry のテストダブル2件と Usage History の show/refresh 1件で、本変更の対象外。

今回の通常実行では対象と関連53件が通過した。一方、以下の競合を制御した回帰テストは
修正前に完了表示待ちで失敗した。分類は **製品側の非同期更新要求喪失**。
Python 3.14 固有の問題という証拠はない。過去の Full pytest にはこの境界の計測がないため、
そこでの失敗がすべてこの競合だけで生じたとまでは断定しない。

## 確定した原因と最小修正

1. worker は `_InspectionTask.run()` の末尾で `finished` を emit する。
2. GUI は通知を受けて `_busy` を解除し、保留中の更新を 0 ms timer で開始する。
3. 通知の処理時点で、直前の `run()` がまだスレッドプールの唯一の枠を保持し得る。
4. 次の `_pool.tryStart(task)` が false になる。既に `_refresh_pending` を false にした後なので、
   次の手動更新や定期 timer まで要求が失われ、完了状態が表示されない。

`tryStart` を `start` に変更し、枠が解放されるまで次の検査をキューに残す。
既存の `_busy` が以降の要求を集約し、`maxThreadCount=1` が検査を直列化する。
新しいポーリング、リトライ、安全ゲートは追加していない。

## 再現と検証（2026-11-07）

環境: Windows / Python 3.14.10 / PySide6 6.11.2 / pytest 9.1.1。

追加した
`test_pending_completion_refresh_waits_for_worker_to_release_pool_slot` は、
実際の worker の処理・通知を実行した後だけ復帰を Event で保持する。
GUI が完了通知と保留更新を処理してから worker を解放し、完了表示が更新されることを確認する。
定期 timer は60秒に設定し、2秒以内の成功が定期再検査による回復でないことを区別する。
待機解除と widget/pool の後処理は `finally` で実行する。

- 回帰テストのみ、製品修正前: **1 failed**（意図した完了表示待ち）。
- Process Monitor / SSE integration / process inspection / ActivityPane widgets / SSE / notifications:
  **91 passed in 4.36s**。
- 元の失敗 node、追加回帰 node、SSE integration を同一 pytest プロセス内で各15回:
  **45 passed in 8.35s**（`--keep-duplicates`）。
- 元 node 単独: **1 passed in 0.71s**。
- 変更した Python 2ファイルの Ruff check / format check: PASS。
- `uv run mypy src/codex_bridge/console/process_monitor.py`: PASS。
- 変更した Python 2ファイルの `compileall`、`git diff --check`: PASS。

JUnit は `.runtime/issue-sequence-20261107/pytest-121-{baseline,red,green,repeat,single}.xml`。
3時間超の全 pytest と GUI/EXE の手動検証は今回実施していない。
既存の `main_window.py` サイズ超過は変更していない。

## #122 への引継ぎ

過去の3.14 Full pytest の Usage History 失敗は
`test_show_refreshes_rolling_range_and_starts_auto_refresh_timer` の
`assert calls == [True]` に対する `[True, True]` だった。
その原因や `0x8001010d` との因果関係は本調査では確定していない。
Usage History の実装・テスト・共通 Qt fixture は変更していない。
