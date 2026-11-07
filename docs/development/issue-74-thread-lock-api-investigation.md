# Issue #74 writer lock APIの読み取り確認

2026-11-07 JST、HEAD `0b8cc59` を基準に確認。
状態は **BLOCKED（上流の正式な所有者照会・解放契約が不足）**。
今回のPhase 7では製品機能を実装しない。これは調査完了の記録であり、
#74の受入条件を満たした報告ではない。

## 今回確認した資料と実行ファイル

- [Issue #74](https://github.com/weito-crowry/CodexBridge/issues/74) の最新本文を
  GitHub connectorで再取得。更新日時は2026-10-17、コメントは0件。
  ロック所有者や解放APIを仮定しないという要件を確認した。
- [公式App Server資料](https://learn.chatgpt.com/docs/app-server) を当日取得。
  旧URL `https://developers.openai.com/codex/app-server/` はこのページへ移動する。
  同資料はCLIのschema生成が実行したバージョン固有の契約を出力すると説明している。
- PowerShellで最初に解決される `codex` は
  `C:\Users\weito\AppData\Roaming\npm\codex.ps1`。
  `codex --version` は **codex-cli 0.166.0**、npm package.jsonも0.166.0。
  wrapperは同ディレクトリの `node_modules/@openai/codex/bin/codex.js` を呼ぶ。
  native binaryは
  `C:\Users\weito\AppData\Roaming\npm\node_modules\@openai\codex\node_modules\@openai\codex-win32-x64\vendor\x86_64-pc-windows-msvc\bin\codex.exe`。
- PATHに別途存在するDesktop配布の
  `C:\Users\weito\AppData\Local\OpenAI\Codex\bin\14b838c1d074ff01\codex.exe`
  は、絶対パス指定の `--version` で **codex-cli 0.167.0-alpha.3.2**。
  これをnpm版と同一バージョンとして扱わない。
- OSプロセス一覧でも両実行ファイルの稼働を読み取り確認した。
  これは実行ファイルの存在・稼働の証拠であり、各threadのlock所有者や
  稼働プロセスのロード済みコードのバージョンを証明しない。

CLIを更新せず、schema生成の有限サブコマンドだけを実行した。
新しい `.runtime/issue-sequence-20261107/issue74-*schema/` の3ディレクトリに、
npm版の通常schema、npm版のexperimental schema、Desktop版のexperimental schemaを保存した。
これらはローカル調査生成物であり製品へ同梱しない。

## 読み取り契約で分かることと分からないこと

| 契約・既存情報 | 確認した意味 | #74で確定できないこと |
| --- | --- | --- |
| `thread/read` / `thread/list` の `Thread.status` | `notLoaded` / `idle` / `systemError` / `active` とactiveFlags | writer所有者、外部lockの有無、lock解放結果 |
| `thread/loaded/list` | そのサーバーのメモリにロード済みのthread ID一覧 | 全プロセス横断の所有元とPID |
| `Thread.originator` / `source` / `cliVersion` | 作成時の由来・作成CLI等のメタデータ | 現在のexecutor・所有プロセス |
| experimental `canAcceptDirectInput` | ロード済みthreadへの直接入力可否。情報がない場合はnull。通常schemaにはない | false/nullが外部writer lockを意味する保証 |
| Bridge `state=not_loaded` | BridgeのStateStoreで未ロード | 未ロック、外部所有、停止済みの区別 |
| Bridge `current_execution` / `last_turn` | #111/#124/#113の実行観測・履歴結果 | writer lockの保有・解放・現在のPID |
| `AppServerClient.os_pid` | 管理対象App Serverの生存PID | そのPIDが個別threadのwriterを所有する証明 |

3種類の生成schemaの `Thread` に、型付きwriter/lock所有者フィールドはなかった。
`ThreadExtra` 等の実装依存データから所有者を推測する契約も追加しない。
`Thread.originator` の説明は現在のclient/executorとは独立した作成時の記録と明記する。
`thread/attachmentOwner/list` は、指定した添付を所有するthreadを返すものであり、
writer lock照会ではない。応答は `threadId` / `archived` でPIDを持たない。
履歴item中の `processId` もwriter所有者フィールドではない。

Bridgeの履歴読み取りは `bridge.py`、状態の投影は `thread_observations.py`、
外部rollout推定は `external_sessions.py` にある。
外部スキャナー自身もoriginatorがOSプロセス所有権ではないと明記している。
既存機能の意味をlock状態に読み替えず、今回これらの製品コードは変更しない。

## 解放に関する契約と制約

3種類の `ClientRequest` に、threadのlock照会・unlock/release/unload専用メソッドはなかった。
`thread/unsubscribe` は存在するが、現在の接続の購読を解除する操作である。
応答は `unsubscribed` / `notSubscribed` / `notLoaded` のみであり、
所有者PIDやwriter解放確認を返さない。
公式資料では、最後の購読がなくなってから非活動の猶予時間を経てunloadされる。
従って即時の「自分のlockを解放」と同じ操作として実装できない。
他の購読者・他プロセスのwriterまで解放できるという契約も確認できなかった。

writer lockのOS上の実体・保有プロセス・ファイルの実際の解放時点は、
今回の公開schemaと読み取りだけでは検証できていない。
これは全バージョンにわたり手段が存在しないという断定ではなく、
確認した2バージョンの公開契約だけでは#74全体を成立させられないという判断である。
`resume` をlockテストとして利用したり、ファイルの存在・作成由来・実行中表示から
未ロック/自所有/外部所有を合成したりしない。
判定不能なthreadのSendを一律禁止する等の新しい安全ゲートも追加していない。

## 検証と未実施

- CLI schema生成3回と、ローカルJSON契約検証を実施。
  `.runtime/issue-sequence-20261107/verify-74-schema.py` は3schemaについて、
  読み取り/購読解除のメソッド、Threadフィールド、runtime status、添付owner、
  購読解除応答を検証し、`schema-74-results.json` に一覧とSHA-256を保存する。
- 既存の読み取りテストを限定実行した。外部推定とlive実行、永続化した直近ターン、
  `not_loaded` の分離と、履歴照会でresume/startしない契約を確認する。
  外部境界はfake/temp directoryであり、稼働中のApp Serverへは接続しない。
- 製品Python・テストの追加変更はない。詳細なコマンドと件数は今回のローカルreportに記録。
- **NOT RUN:** 実threadのwriter lock照会・解放、unsubscribe/unload、resume、
  turn/start/steer/interrupt、プロセス停止、環境同期・設定変更、GUI/EXE実機受入、全pytest。
  実ターン状態や実lock所有者を今回観測したとする結果はない。

## 再開条件と後続への引き継ぎ

上流に所有者照会と自所有writer解放の正式契約が提供されるか、
既存の正式契約でこれらを満たせる一次資料が得られた時点で再開する。
その際、接続・App Serverプロセス・Desktop共有daemonのどの単位を
「自プロセス」と定義できるか、他の購読者がいるときの解放結果、
競合の機械可読エラーと解放完了の観測方法を該当バージョンで確認する。
これは調査の不足を埋める再開条件であり、新規の製品承認ゲートではない。

#48は上記CLIパス/バージョンとローカルschemaを再利用できる。
ただし `--version` は利用可能なbinaryの情報であり、現在のBridge設定で選択されたbinaryや
個別threadの状態を確定していない。設定優先順位は `config.py` の
explicit → environment → config → default `codex`。
upstreamの今後の変更は今回の記録だけで断定せず、必要な契約差分を再確認する。
