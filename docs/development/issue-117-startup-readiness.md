# Issue #117: Console 起動後の準備待ち

2026-11-07 の調査・修正記録。実サービスの再起動、設定変更、新規ターンの作成は行っていない。

## 確定した原因と修正範囲

managed Tunnel profile を使う場合、GUI が Bridge/App Server の ready 応答を処理する途中で
`set_tunnel_bridge_ready()` → `generate_tunnel_profile()` → `subprocess.run(doctor)` を
同期実行していた。doctor の上限は 15 秒で、その間 GUI の描画、操作、タイマー、ネットワーク応答処理も止まる。

0.7 秒待機する fake を使うと ready callback が **0.740 秒**停止し、GUI callback の応答時間を
200 ms 未満とする回帰テストが失敗した。修正後は既存の profile 生成・doctor・atomic install を
Qt worker で実行し、結果だけを GUI thread に返す。同じ回帰条件で 200 ms 未満の応答、doctor 待機中の
Qt heartbeat、検査前の Tunnel 起動抑止を自動検証している。実際の profile 生成を通し、doctor のみを
fake にしたケースでも、検査中は target が未設置で、成功後に設置されることを確認した。

起動順序は Bridge/App Server ready → managed profile 検査と設置 → Tunnel Supervisor の
version/doctor → Tunnel 起動 → health/ready のまま。既存の timeout、retry、失敗時の profile 保護、
手動 Retry Now は維持する。遅れて到着した結果は、閉じた window や古い Bridge 世代を更新せず、
Bridge 到達不能・停止遷移中には Tunnel を起動しない。既存の稼働中 Tunnel やサービスを再起動する処理は追加していない。

## 起動経路と待ち時間

| 段階 | 実装・既存の待ち時間 | GUI との関係 |
| --- | --- | --- |
| Codex executable 検証 | `CodexVersionProbe`: 候補ごとに最大 3 秒 | QProcess、非同期 |
| Bridge 起動 | detached launcher → `BridgeRuntime.start()` | 子プロセス |
| App Server 初期化 | `app-server --stdio` → initialize/initialized → UI API 起動 | Bridge 側 asyncio、GUI とは別 |
| Bridge ready | healthz と ui-api/status、350 ms 間隔、10 秒 deadline | Qt network、非同期 |
| Bridge managed retry | 2 / 5 / 10 秒 | Qt timer、非同期 |
| managed profile | staged doctor 最大 15 秒、成功時 atomic replace | **今回 GUI 同期処理を worker に移動** |
| Tunnel version/doctor | Supervisor の QProcess、各検査最大 10 秒 | 非同期 |
| Tunnel health/ready | 起動中 350 ms、10 秒後 not_ready、通常 5 秒 | Qt network、非同期 |
| Tunnel recovery | 1 / 3 / 10 / 30 秒、その後 60 秒 | Qt timer、非同期 |

これらの上限を合計して実際の起動所要時間とはみなさない。分岐や並行処理があり、実際の初期化・応答時間は
外部プロセスと環境に依存する。profile の doctor 自体を高速化する変更ではないため、Tunnel の ready 到達が
doctor の時間だけ早まるとは主張しない。確定した改善は、その待ち時間で GUI 全体を停止させないこと。

## 未確定の現象

保存済み Bridge observability には、次の別 PID 間の記録が残っていた。

- `2026-10-28T19:50:15.542276Z`: `server.shutdown`, PID 261664
- `2026-10-28T19:52:30.633196Z`: `server.start`, PID 287408

差は 135.091 秒。ただし、この区間には window 表示、profile doctor の開始・終了、initialize の
対応する測定記録がなく、今回の GUI 停止と同じ原因だとは確定できない。
`console-observability.jsonl` にも当該日の Tunnel イベントがなく、他日の再接続ループを当該起動に結びつけられない。

#111/#80 で別途測定された初回 JSONL scan の cold 遅延も独立した観測で、今回の GUI 同期 doctor の
原因とは混同しない。今回、初回 scan の仕様・キャッシュ・対象範囲は変更していない。

## 再現時の追加測定

今後通常運用で Console を起動する際、次の時点を同じ時計で記録すると未確定部分を分離できる。

1. window 表示、Bridge connected、App Server ready の各時刻。
2. Status の managed profile generation/doctor が Running → Passed/Failed になる時刻。
3. Tunnel Supervisor validation と Tunnel ready の各時刻。待機中も window の操作が応答するか。
4. 同じ時刻の Diagnostics と `%LOCALAPPDATA%/CodexBridge/logs/` の observability。
   Tunnel イベントは PID、supervisor_id、attempt_id、reason、recovery_delay_ms を対応させる。
5. Bridge/App Server ready より前が長ければ子プロセス起動と initialize の区間、profile の Running が
   長ければ staged doctor、Supervisor validation が長ければ version/doctor、起動後が長ければ health/ready と
   recovery の区間として追加測定する。JSONL の初回 scan 完了時間は別項目にする。

API キー、profile 本文、入力内容を診断記録に含める必要はない。

自動回帰は `uv run pytest tests/test_console_profile_readiness.py -q` で実行できる。
これは fake doctor と一時ディレクトリを使用し、実サービスやユーザー設定には接続しない。
