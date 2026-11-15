# Issue #48: Windows Sandbox setup refresh の共有違反

2026-11-07 JST 調査。状態は **BLOCKED（上流制約）**。読み取り調査と記録を行い、
製品コード・設定・実行環境は変更していない。通常Sandboxでの受入条件は未達・未検証。
開始HEADは `ad8a6463cebb84a8158c1c489d229b919f99e0c5`。

## 過去に確認された直接原因

[Issue #48](https://github.com/weito-crowry/CodexBridge/issues/48) の最新本文とコメントを
2026-11-07に取得した。本文の最終更新は2026-10-11、コメントは0件。
当時のCLIは **0.162.1**。新規threadの作成は成功し、通常Sandboxでのシェル起動は
`unifiedExecStartup` / `helper_unknown_error: setup refresh had errors` で失敗した。

今回、既存の `D:/CodexData/.codex/.sandbox/sandbox.2026-10-11.log` も読み取りで照合した。
先頭の関連記録（3〜9行）では次の順序になっている。

1. npm配下のCodex native packageに同梱された `codex-windows-sandbox-setup.exe` を起動。
2. DesktopのComputer Use runtime `cua_node/cfb32733c877621e/bin/node_repl.exe` に対する
   `runtime read/execute validation` が `open ACL target for root-only update` で失敗。
3. Windows共有違反 `os error 32` を記録し、setup refreshがエラー終了、helperの終了値は1。

したがって直接の失敗箇所はCodexのWindows sandbox helperによるruntime ACL対象のopen。
BridgeのPowerShellコマンド内容やTunnel転送の失敗を示す証拠ではない。
どのプロセス・handleが共有違反を生んだか、要求した共有モード、正確な競合条件、
現在の上流実装での再発有無は未確定。特定のPIDやACL不良を原因と断定しない。

過去の `danger-full-access` での比較成功はIssue本文の観測としてのみ扱う。
今回比較実行はしておらず、Bridgeの既定を変更する根拠にはしない。

## 現行installed versionと契約

#74で同日に取得済みのCLI/path/version/schemaを再利用した。バージョン取得やschema生成は
重複実行していない。CLIは **0.166.0**、Desktop同梱native exeは **0.167.0-alpha.3.2**。
これはディスク上のbinaryの確認結果であり、2026-10-11の0.162.1の挙動と同一視しない。
Bridgeが現在選択したbinaryおよび稼働済みプロセスのロード済みversionは今回確定していない。

CLIの通常・experimental、およびDesktopのexperimentalの3つの生成済みschemaで、
`windowsSandbox/` requestは以下の2つだった。

| 契約 | 取得できる情報／入力 | 共有違反解消との関係 |
| --- | --- | --- |
| `windowsSandbox/readiness` | 応答`status`: `ready`, `notConfigured`, `updateRequired` | ロック所有PID、handle、ACL open失敗の原因を返す契約ではない |
| `windowsSandbox/setupStart` | `mode`: `elevated` / `unelevated`、任意`cwd`、応答`started` | セットアップを開始する変更操作。呼び出すだけで競合が解消するとは定義されていない |
| `windowsSandbox/setupCompleted`通知 | `mode`, `success`, 任意の文字列またはnullの`error` | セットアップ結果。競合の正規解放APIではない |

3 schemaの該当fieldとmethod集合を、保存済みJSONだけを読む
`.runtime/issue-sequence-20261107/verify-48-schema.py` で検証した。
3件PASS、exit 0。未来の全バージョンに解放APIが存在しないという主張ではない。

[公式App Server文書](https://learn.chatgpt.com/docs/app-server) を2026-11-07に取得し、
readinessから必要時に非同期setupStartを開始し、setupCompletedを待つ契約を確認した。
確認したschemaと公式記載の範囲では、使用中runtimeのOS共有違反を解放する契約を特定できない。
[公式Windows sandbox文書](https://learn.chatgpt.com/docs/windows/windows-sandbox) は、
問題切り分け用にsandbox log、選択implementation、エラー、Windowsのversion/buildを
収集する手順を示している。文書中の再起動・setup再実行などの環境操作は今回行っていない。
公開文書のMXC推奨から、この端末での利用可否や#48解消を推定しない。

既存 `sandbox.2026-11-07.log` の読み取り結果は `MXC availability: false` と
`Windows sandbox selection: elevated` の記録のみだった。
この記録だけでは通常Sandboxのコマンド成功・不具合解消・再現を証明できない。

## Bridge側の責務と検証範囲

`src/codex_bridge/bridge.py` の `start` は `inherit` / 未指定の場合にsandbox overrideを
送らず、`thread/start` を上流に委譲する。Full accessは明示指定時のprofile override。
`src/codex_bridge/app_server.py` は選択されたnative exeを `app-server --stdio` で起動する。
Bridgeに上記runtimeファイルのACL更新や共有違反解除を行う経路は見つからなかった。
存在が確認できたsetup APIを追加呼び出ししても、競合解消の根拠にはならない。

既存の有限なfake App Serverテストを実行した（temp directoryのみ）。

```powershell
uv run --no-sync pytest tests/test_bridge.py::test_start_inherit_does_not_send_sandbox tests/test_bridge.py::test_start_full_access_sends_durable_permission_profile_override tests/test_bridge_notifications.py::test_turn_terminal_notifications_are_normalized_as_activities tests/test_bridge_notifications.py::test_error_notification_records_only_bounded_error_summary tests/test_bridge_notifications.py::test_retryable_error_keeps_turn_active_until_completion tests/test_bridge_notifications.py::test_command_output_delta_and_process_metadata_are_exposed_as_activities -q
```

**7 passed in 0.59s、exit 0**。inheritの委譲、明示profile、一般的なterminal/error/command活動の
伝播を確認した。実際の `unifiedExecStartup` エラーの全ペイロードを再現するテストでも、
Windows helperのACL open動作や#48の解消を検証したテストでもない。

**NOT RUN:** 実Sandboxのreadiness/setupStart/setup refresh、通常Sandbox/Full accessでの
コマンド比較、上流APIへの実接続、upgrade、再起動、サービスやプロセス停止、handle操作、
ACL変更、ファイル削除・退避、GUI/EXE受入、全pytest。
製品Python変更がないため、ruff/mypy/compileallもNOT RUN。

## 再開条件

上流で共有違反を解消した修正・対応版の一次資料、またはruntime利用中でも正規に処理できる
公開契約が得られた時点で、現行環境と照合する。上流への調査材料は上記helper名・runtime path・
エラーの順序・当時0.162.1というversionを保ち、現在のversionとは分ける。
ログ共有・upstreamへの投稿は今回行っていない。

その後、環境操作を含む実機確認が許可された範囲で、Issueの完了条件である
`D:/CodexBridge` の通常 `workspace-write` での連続コマンド成功とsetup refreshエラーの
非再発を確認する。それまでは **調査記録のみ／BLOCKED** を維持する。
Bridge製品への推測修正や新しい安全ゲートは追加しない。
