# CodexBridge Open Issue順次実装報告（2026-11-07）

対象19件を指定順に処理。サブエージェントは同時に1体だけ使用し、入れ子・追加delegation・独立レビュー・model escalationは行っていない。
実装／自動検証済13件、部分実装3件、未解決調査1件、BLOCKED2件。これはIssue最終受入・クローズではなく、ChatGPTレビューへの引継ぎ。

## Gitと既存作業の保護

- branch: codex/python314-migration（既存checkoutを継続、新規worktreeなし）。
- 開始HEAD: e8e003468bcc6dfd7ab5137ba776f54f07dfd5b1。
- 製品ビルド／統合検証の基準HEAD: 2ad8b68d7fd11e75f3d3984dfd566ac62607f150。
- 各Issue通常commit・originへpush。PR作成、Issue close、merge、force push、rebase、tag、release、本番反映なし。
- 開始時の未追跡 .codexbridge-staging/ と tmp/ を保護。ユーザーconfig、稼働Console/Bridge/Tunnel、既存Codexターンを操作しない。
- pyproject更新後のuv同期は稼働中Console EXEのWindows lockで失敗したため、環境を停止・更新せず uv run --no-sync を使用。

## Issue別結果

| Issue | 状態 | 内容 | commit | 自動検証・残件 |
|---|---|---|---|---|
| #111 | 部分実装・手動検証待ち | 初回scanのIDなしterminal誤復元を修正。原非表示症状は未再現 | [9657a14](https://github.com/weito-crowry/CodexBridge/commit/9657a1433ea2f737eee5c99075a5f6b6b7dca856) | 関連76 PASS |
| #80 | 部分実装・手動検証待ち | 再起動・HTTP前後・空一覧・終了反映の統合回帰を追加 | [86add63](https://github.com/weito-crowry/CodexBridge/commit/86add63a7795aa7758e95dbc081e463f95801830) | 統合2 PASS／原症状未解決 |
| #124 | 実装済・手動検証待ち | last_turn/needs_attention、保存履歴復元、load状態と結果を分離 | [b467145](https://github.com/weito-crowry/CodexBridge/commit/b46714503b91b6820c09e881fe7109fd37a3abd5) | 185 PASS/1 SKIP＋最終77 PASS |
| #117 | 実装済・手動検証待ち | GUI同期doctorをworkerへ移動。既存起動順序/retry維持 | [72f9cf6](https://github.com/weito-crowry/CodexBridge/commit/72f9cf6ab6acd943113f5d4f5f8b3e4895eed379) | 関連149 PASS＋回帰5 PASS |
| #121 | 実装済 | worker終了通知とtryStart競合によるrefresh消失を修正 | [ec483a2](https://github.com/weito-crowry/CodexBridge/commit/ec483a2a40c0505c63297a23c6f04eae3b9c78b8) | 関連91 PASS／連続45 PASS |
| #122 | 未解決・調査記録 | Usage間欠二重更新とCOM例外の因果を断定せず記録 | [16a53d2](https://github.com/weito-crowry/CodexBridge/commit/16a53d29e665f609acdbccbc5280aebff78f8bdf) | 元node/20回/計測100回/周辺/関連PASS、根因未再現 |
| #123 | 自動検証済・手動検証待ち | Python3.14パッケージ・tzdata警告を検証、依存追加なし | [933ca77](https://github.com/weito-crowry/CodexBridge/commit/933ca77d8084251aa74298b637b042e1eed92aca) | 対象65 PASS／最終build・非対話smoke PASS |
| #119 | 実装済・手動検証待ち | 改行/Markdown描画と長文原文・Copy保持を修正 | [8d36fb9](https://github.com/weito-crowry/CodexBridge/commit/8d36fb943e891f1e1354e66e2a57a7421f22c21d) | 関連91 PASS |
| #120 | 実装済・手動検証待ち | Copy成功のみチェック、1800ms復帰、連打/破棄/失敗対応 | [c71cff9](https://github.com/weito-crowry/CodexBridge/commit/c71cff911a7b70678c28188c606eb2440452a35b) | 最終関連135 PASS |
| #116 | 実装済・手動検証待ち | 0pxラベル修正、元名・省略・tooltip・同名/draft対応 | [814afe5](https://github.com/weito-crowry/CodexBridge/commit/814afe55e5fba1d6e54198592bb1e48ca240e442) | 関連110 PASS/2 SKIP＋テーマ8 PASS |
| #112 | 実装済・手動検証待ち | コマンド別折りたたみ・識別・有界保持・描画coalesce | [c1e728c](https://github.com/weito-crowry/CodexBridge/commit/c1e728c8e832098ef4841386b213cf6e789a3f0a) | 最終23 PASS／保持上限61.81秒→0.39秒 |
| #110 | 実装済・手動検証待ち | Messages任意詳細toggle、初期OFF・時系列/個別展開維持 | [015ddcd](https://github.com/weito-crowry/CodexBridge/commit/015ddcd67e7148b7b3c00f082d0e21c08b284464) | 関連156 PASS＋focused5 PASS |
| #113 | 実装済・手動検証待ち | 既存/mcp認証LAN、単一共有監視、既存SSE/応答拡張 | [d98a5b1](https://github.com/weito-crowry/CodexBridge/commit/d98a5b1d39924c6985893119905fb821cc6d39c3) / [2a56d1d](https://github.com/weito-crowry/CodexBridge/commit/2a56d1d8e8ea9e3f5545af544cc32a155e57c648) | 実MCP20 PASS／関連133 PASS |
| #114 | 実装済・手動検証待ち | 5h/週間独立の枯渇予測・期間切替・理由付き算出不可 | [05afb31](https://github.com/weito-crowry/CodexBridge/commit/05afb31618b110efa300e17c7810cacc5ae6260a) | 関連147 PASS |
| #118 | 実装済・手動検証待ち | system/ja-JP/en、永続化・再起動適用、主要UI翻訳 | [31888f0](https://github.com/weito-crowry/CodexBridge/commit/31888f09492eda8af9190b734d420d3583ff5744) | 1120 node分割検証／wheel・EXE同梱PASS |
| #73 | 実装済・手動検証待ち | 絶対exeパスprefilter修正、所有Windows親子の終了/残存確認 | [b2f307b](https://github.com/weito-crowry/CodexBridge/commit/b2f307bae827e3e23a0f25f916297baf4291bc1c) | 関連100 PASS／OS実測PASS |
| #75 | 部分実装・手動検証待ち | 確定終端一度化・通信断誤通知抑止。クリックは前面表示のみ | [0b8cc59](https://github.com/weito-crowry/CodexBridge/commit/0b8cc59392b7a40df9c74f32c05c3b6398d97026) | 関連161 PASS／対象thread遷移は制約 |
| #74 | BLOCKED | 現行正式APIにwriter所有者/即時解放契約を確認できず | [ad8a646](https://github.com/weito-crowry/CodexBridge/commit/ad8a6463cebb84a8158c1c489d229b919f99e0c5) | 3schema契約＋限定11 pytest PASS |
| #48 | BLOCKED | 上流Sandbox ACL open共有違反、Bridge独自回避なし | [2ad8b68](https://github.com/weito-crowry/CodexBridge/commit/2ad8b68d7fd11e75f3d3984dfd566ac62607f150) | 3schema契約＋限定7 pytest PASS／実setup NOT RUN |

上表のテスト件数はIssue間で重複するため合算しない。未実施の実機確認はPASSに含めていない。

## 最終統合検証

- 現行pytest収集: **1863 node**。
- #118の1120検証済nodeのうち現行名称と一致する1117件を再利用。旧3名称は#73/#75の置換・追加後nodeとして最終実行へ含めた。
- 未検証分746件を実行: **740 PASS / 6 SKIP、exit 0**。選択集合と実収集集合は一致。
- 重複を除く分割coverage: **1857 PASS / 6 SKIP、未検証0**。単一コマンドのFull pytestはNOT RUN。
- Console広域はWidget蓄積によるstyle再適用の長時間化で中断。実測PASSと修正後PASSを記録し、境界・未実施417件を150/150/117件の正常終了プロセスへ分割。旧期待値FAILは修正後テストへ対応付けた。
- 最終6 SKIPはWindowsのfile/directory symlink利用不可。明示された原因は作成権限不足（WinError1314）で、1件は一般的な利用不可メッセージ。成功扱いしない。
- Ruff check: PASS。Ruff format --check: 227 files PASS。mypy src: 102 source files PASS。compileall src: PASS。
- PySide6/shiboken6をimport不可にした別processでMCP core server import: PASS。
- **既存module-size検査はFAIL**: main_window.py 1534 physical LOC > 1500。開始時1506から今回1534へ増加。baseline/制限の引上げや無関係な分割はしていない。

再現用ログ・node集合は .runtime/issue-sequence-20261107/ の coverage-manifest118.json、verified-nodes118.txt、all-nodes-final.json、final-selection.json、final-remaining-results.json、final-remaining.xml、final-*.log。

## 最新Windows成果物

- ソース基準: 2ad8b68d7fd11e75f3d3984dfd566ac62607f150（#73/#75製品修正を含む）。
- Python 3.14.10 / PySide6 6.11.2 / PyInstaller 6.22.3、現行specを専用出力へ直接使用。既存build/distは保護。
- EXE: D:\CodexBridge\.runtime\issue-sequence-20261107\package-final\dist\CodexBridge\CodexBridge.exe (11,329,453 bytes)。
- EXE SHA256: d4a84a8756db0725d5f6d23b8decd2b64d22f97a1a48e255b6100e11942aac67。
- ZIP: D:\CodexBridge\.runtime\issue-sequence-20261107\package-final\CodexBridge-windows.zip (70,570,317 bytes)。
- ZIP SHA256: 2d150db23929d792df28012a7c3c9a31a2b3600f4f80daa6fb20bb6681291b27。
- 実EXE --help exit0、不正--ui-port exit2。隔離先の生成ファイルは入力config.tomlのみ。
- 同梱PYZ/bootstraps/runtime hooks/DLLのみの補助probeでQt offscreen生成・イベントループ終了、主要module import、icon、隔離設定、epoch/epoch-ms/UTC ISO日時、日本語Copy/標準ボタン・qm読込PASS。
- 日本語catalog 648件とqtbase_ja.qmはソースとbyte一致。
- tzdata警告のための依存追加はしていない。現行日時経路PASS、明示的IANA ZoneInfo利用は未提供の制限。補助probeは通常Console/BridgeのGUI起動・終了証明ではない。

## 残課題・人間による確認

1. #111/#80: Desktop実行中のConsole再起動による原非表示症状。自動経路では未再現で、原因・完全解消を主張しない。
2. #124/#117: 実通信断・再起動後の状態表示、通常起動readiness。135.091秒の過去再起動間隔やcold JSONL scanとの因果は未確定。
3. #123/#119/#120/#116/#112/#110/#114/#118/#73: 最新EXEの通常起動・通常終了、長文/改行/Copy/添付/並列出力/グラフ/日本語・英語/テーマ・狭幅/実CodexプロセスのGUI受入。
4. #113: 別PC LAN、実Chrome MV3、実Tunnel公共経路。ローカル実MCP・模擬peerはPASSだが実LANの代替とはしない。認証設定はユーザー環境へ有効化していない。
5. #75: 実Windows通知の各種/最小化/非アクティブ/音/クリック。現行Qt API/Controllerは単発・連続とも前面表示のみで、正確な対象thread遷移は未対応。通知だけで承認/回答しない。
6. #122: 旧Usage間欠失敗と0x8001010dの因果・根因は未解決。今回のPASSで修正済みとは扱わない。
7. #74/#48: 上流正式契約・修正と許可範囲の実機受入待ち。lock解放/Sandbox setup/ユーザー環境変更は未実施。

## ChatGPTレビュー対象

- 全実装commitの差分と上記module-size FAIL。
- #119のQt MD4C HARD_SOFT_BREAKS 0x8000依存（名前付き公開Qt enumではない。Qt6.8/6.11一次sourceと自動テストで確認）。
- #113の実peerに基づくloopback免除、proxy_headers=False、既存Host/Origin保護・明示CORS、共有監視/SSE、cached last_turnのUnknown表現。
- #118のexplicit tr/placeholder、API raw文字列と表示adapterの分離、translator一回登録、再起動適用、Qt dialog化とqm同梱。
- #73のprefilter最小修正と後段厳密照合の維持、#75のconnection_lost通知抑止・確定終端一度化。

[実装全差分](https://github.com/weito-crowry/CodexBridge/compare/e8e003468bcc6dfd7ab5137ba776f54f07dfd5b1...2ad8b68d7fd11e75f3d3984dfd566ac62607f150)
