# CodexBridge Workflow 統合設計書（Phase 5）

## 1. 目的と今回の範囲

Phase 5では、独立して動作しているOWS/DBOSのPoCをCodexBridge製品へ段階的に統合する。製品統合後も、同期型DBOS処理をBridge本体のasync event loopへ載せず、Bridgeが管理する別プロセスのWorkflow Workerから、既存localhost UI control APIを通してCodex App Serverを操作する。

Phase 5Aで追加するのは `codex_bridge.workflows` パッケージのHTTP transportと本設計書だけである。Phase 5AではWorkflow機能を有効化せず、API・プロセス・ネットワーク通信を増やさない。既存UI API、MCP、core Consoleの挙動は変更せず、PoCのコード、設定、状態、起動方法も変更しない。

| Phase | 目的 |
| --- | --- |
| 5A（今回） | 製品パッケージへloopback HTTP clientを移し、5B以降の統合設計と受入条件を確定する。 |
| 5B | OWS/DBOS Workerを製品パッケージ化し、Bridge管理下の別プロセスとして起動・停止する。 |
| 5C | 認証付きlocalhost workflow control APIを実装する。 |
| 5D | ConsoleにWorkflowの開始・一覧・詳細表示を追加する。 |
| 5E | 固定single-job構成を保って製品E2Eを再現し、その後に扱うジョブ範囲を段階的に広げる。 |

## 2. 現行CodexBridgeの構成

`src/codex_bridge/server.py` の `BridgeRuntime` は、Codex App Server client、Bridge、Activity store、localhost UI server、外部セッション監視をまとめて管理する。`build_runtime()` がこれらを構成する。通常の起動順序はApp Server、UI server、外部セッション監視であり、終了時には購読を閉じ、外部監視を終了し、現在のactive turnへinterruptを送り、UI serverとApp Serverを終了する。

UI control APIは既存のlocalhost UI serverに属する。`ui_control.py` が実Codex threadの作成・継続・interrupt・状態照会を行い、`ui_api.py` がHTTP routeとBearer認証を接続する。thread作成APIは `client_request_id`、cwd、prompt等を検証し、request IDとpayloadを照合する既存の実行中冪等性を持つ。Phase 5AはこのAPI、その認証方式、MCP routeを変更しない。

ConsoleはUIを所有し、`src/codex_bridge/console_entry.py` の `INTERNAL_RUNTIME_FLAG` によりPyInstaller実行ファイル内でBridge runtimeを起動する構成を持つ。Windows package設定は `packaging/windows/CodexBridge.spec` にある。Workflow WorkerはConsole画面やBridge event loopへ埋め込まず、Bridgeが所有する独立プロセスとして扱う。Phase 5AではEXE entrypoint、`INTERNAL_RUNTIME_FLAG`、`CodexBridge.spec` を変更しない。

## 3. OWS/DBOS PoCと確認済みE2E

`poc/ows/` はCLIからWorkerを起動し、DBOS/OWS interpreterがworkflowを処理する。Codex job adapterはSQLite ledgerへ送信前状態を保存してから、loopback HTTP clientで既存UI control APIへ実Codex thread作成要求を送る。Workerはthread/turnの状態とitemsを照会し、結果をDBOS workflow resultへ反映する。

PoCの正常系実E2Eでは、`2+2→4` の固定jobが `SUCCEEDED` となり、`final_text` は `4` だった。記録されたthread IDは `01a1c70f-8d3f-7c70-88fd-daf5c64e96d4`、workflow IDは `6a02adf0-14cb-4f60-84e1-8790b6d85eee` である。この記録が示すのは単一の固定入力による正常系成功のみであり、障害注入、再起動中の回復、複数job、任意prompt、分岐やloopの製品動作を証明しない。ユーザー判断によりPoCの障害注入試験はスキップされた。製品統合では、ここに列挙した未確認領域を各Phaseの受入条件で検証する。

## 4. 製品統合後の責務と境界

| コンポーネント | 所有する責務 |
| --- | --- |
| Bridge | workflow開始要求の受理、Workerの起動・監視・終了、Bridgeが保有するcontrol tokenのWorker環境変数への内部引き渡し、workflowの一覧・状態・結果API提供。 |
| Workflow Worker | DBOS runtime、OWS interpreterとsubset/schema検証、workflow実行、SQLiteのDBOS状態とCodex job ledgerの管理。既存localhost UI control APIを呼び出す。 |
| Console | workflow開始操作と一覧・詳細・状態の表示。API呼び出しと表示に専念し、DBOSやSQLiteを直接操作しない。 |
| Codex App Server / UI control API | 既存Bridge機能として実Codex thread/turnの開始と照会を提供する。5Aでは変更しない。 |

WorkerはBridgeから独立したOS processであり、Console UI processとも独立する。Bridge async event loopはWorkerの制御とHTTP/API処理だけを行い、DBOSの同期実行を行わない。WorkerからApp Serverへの接続経路は既存UI control APIのloopback接続に限定し、App Serverへ新たな外向き接続を作らない。未接続・外部管理のBridgeを探索して勝手に接続しない。

Workerへ渡すcontrol tokenはBridgeの子process環境変数 `CODEX_BRIDGE_CONTROL_TOKEN` のみに置く。引数、通常ログ、例外ログ、manifest、DBOS record、SQLite job ledger、workflow resultへ出してはならない。新しいtokenの自動発行はしない。Bridgeにtokenが存在しない場合、Workerのlive control機能は無効とし、live jobを受理しない。

## 5. Bridge lifecycleと実Codex turnの扱い

5BではBridgeの起動完了後にWorkerを起動し、既存App ServerおよびUI control APIがreadyであることをWorker側preflightで確認する。Bridge runtimeの初期化・起動に失敗した場合は、起動していないWorkerを残さない。Bridge shutdownでは新しいworkflow開始を受け付けない状態にしてからWorkerへ終了を通知し、BridgeのHTTP/UIとApp Serverの終了順序を整合させる。Workerの起動失敗・異常終了をBridge全体の未処理例外にせず、管理状態として記録してAPIへ返す。

現在の `BridgeRuntime.shutdown()` はBridgeの全active turnへinterruptを送る。5B/5Cではturn所有者を識別できるようにし、通常のBridge shutdownからWorkflow所有turnへ意図しないinterruptを送らない方針を実装する。UI/MCP所有turnの現行終了動作は維持する。BridgeやApp Serverの終了に伴って結果を照会できなくなった送信済みjobはledger上の `MAYBE_SENT` を保持し、再POSTせず、同じthread/turn IDとの照合を再開できるまで結果を確定しない。明示的なjob cancel APIとshutdown時の所有turn停止は別の仕様として扱う。

## 6. Phase 5B: Workerの製品パッケージ化

5Bは次の順序で実施する。

1. `src/codex_bridge/workflows/` にOWS interpreter、OWS v1.0.3 pinned schema、DBOS Worker、Codex job adapter、SQLite ledgerを移行する。現行PoCの契約と回復挙動を基準に機械的に移し、任意workflow機能や認証の変更を混ぜない。
2. 製品側をcanonical sourceにした後、PoCのCLI/fixture/testsを維持したままcanonical実装を参照する形へ移し、OWS実装・schema・adapterの二重sourceを残さない。移行途中の5AではPoCファイルを変更しない。
3. DBOS等の依存を明示的な `workflows` optional extraへ追加する。基準は `dbos==3.4.0`、`jq==1.13.0`、`jsonschema==4.25.1`、`PyYAML==6.0.3` および OWS v1.0.3 pinned schemaとし、無断で最新版へ更新しない。Workflow機能を利用しない通常起動ではこのextraを必須にしない。
4. `packaging/windows/CodexBridge.spec` でWindows PyInstaller版に専用Worker entrypoint、起動flag、OWS schema、必要なWorker binariesを同梱する。既存 `INTERNAL_RUNTIME_FLAG` はBridge runtime専用のままにし、Worker modeを別flagで判定する。frozen時の `sys.executable` は通常の `python.exe` ではないため、`sys.executable -m ...` を前提にせず、同梱EXEのWorker専用internal modeを実装する。
5. BridgeはWorkerをasync subprocessとして管理する。引数にtokenを含めず、子環境変数だけにtokenを設定する。Bridgeが子プロセスを終了・回収でき、Worker異常終了を状態照会できることを確認する。
6. `AllowedPathPolicy` に沿って、Worker state rootとCodex job cwdを分ける。推奨state rootは `%LOCALAPPDATA%\CodexBridge\workflows`。PoCの `poc/ows/.state` 固定rootと `run_dir\codex-job-cwd\<id>` の作業cwdをそのまま製品へ持ち込まない。Codex cwdは許可root内で管理し、DBOS/ledger/schema等の状態ファイルは別のstate rootに保存する。子process用PATHはBridge側で許可する値を構成し、Worker起動側でも絶対path等の妥当性を検証する。
7. 送信前ledgerの `MAYBE_SENT` と結果照合停止を維持する。HTTP timeout、Worker再起動、Bridge再起動を理由に同じCodex thread POSTを自動再送しない。

### 5B受入条件

- PoC既存テストが維持され、PoC入口から製品canonical実装を実行できる。
- OWS実装とpinned schemaの製品・PoC二重管理が解消される。
- 通常起動ではWorkerが起動せず、明示的な管理下でのみ別processとして起動する。
- frozen Windows版で専用Worker modeが起動・終了でき、`sys.executable` をPython interpreterと誤認しない。
- tokenは子環境変数以外に現れず、Bridgeにtokenがないとlive controlは無効である。
- Workerのstate rootとCodex cwdが分離され、AllowedPathPolicy境界、再起動時の `MAYBE_SENT` 非再送を確認できる。
- Workflow所有turnを通常shutdownで明示interruptしない。既存UI/MCP動作とConsoleの起動・終了は維持される。

## 7. Phase 5C: workflow control API

5Cでは既存localhost UI APIと認証境界を再利用して、workflowの `start`、`list`、`status`、`result` を提供する。Bridgeが認証と要求受付を担当し、Worker状態はWorkerの永続state/ledgerから読み出す。ConsoleはこのAPIを利用し、DBOSやSQLiteへ直接アクセスしない。

start要求には永続的なrequest IDを必須とし、同じIDと同じ正規化payloadの再送は既存結果を返す。同じIDに異なるpayloadを結び付けた要求はconflictとして拒否する。in-memory registryだけで永続冪等性を代用しない。YAML入力はOWS対応subsetの制約検証とpinned公式schema検証を両方通す。job cwdは `AllowedPathPolicy` の許可root内であることを検証する。状態保存rootは `%LOCALAPPDATA%\CodexBridge\workflows` を推奨し、Codex cwdと混在させない。PATHは子processに渡す値も含めて安全性を検証する。

APIは少なくとも次の失敗を区別して返す: 認証失敗、request/schema不正、request ID競合、cwd拒否、token未設定、Bridge/App Server/Worker未ready、job実行失敗、送信後の結果不明、内部障害。公開応答とログにはtokenや認証headerを含めない。job結果は状態照会によって確認し、送信結果不明を成功・失敗へ推測で変換しない。

### 5C受入条件

- 認証付きlocalhost APIにstart/list/status/resultが登録される。
- 永続request ID冪等性、異payload conflict、subset/schema拒否、許可root外cwd拒否が確認される。
- Worker再起動を挟んでも状態・結果が永続stateから読める。
- tokenを含むheader/bodyがログ、DB、manifest、API応答へ漏れない。
- 既存UI control APIとMCPのroute/contractが変わらない。

## 8. Phase 5D: Console UI

5DではConsoleにWorkflow開始、一覧、詳細、状態表示を追加する。既存の言語切替とnative menu barへ統合し、画面文言を既存locale方式に合わせる。Consoleは認証付きlocalhost control APIを呼び出し、実行中Bridgeを停止・再起動せずに状態と結果を再読込できる。Console終了だけでBridge管理Workerや実Codex turnをinterruptしない。

### 5D受入条件

- 開始・一覧・詳細・状態の各画面が5C APIを使い、DB/Workerへ直接接続しない。
- 既存locale切替とmenu barの動作を維持する。
- Bridgeを稼働させたまま状態を更新でき、Console再起動後もworkflow一覧と結果が復元される。

## 9. Phase 5E: 固定single-jobからの段階拡張

最初は5B〜5Dを通した製品E2Eを固定のsingle-jobで再現する。入力はPoCと同じ制約付き正常系で、成功結果、状態永続化、request ID冪等性、再起動後照合、ログsecret非漏出を確認する。利用可能なジョブ数、任意prompt、分岐、loopはこの受入後に個別に段階対応する。危険な動作に対するscope/approval等の仕様と検証範囲が設計されるまで、これらの制限を解除しない。

### 5E受入条件

- まず固定single-jobでstartからfinal result表示までをWindows native環境で再現する。
- 同一request IDの再送で実Codex POSTが重複せず、送信状態不明時もledger照合を維持する。
- Bridge/Worker/Console各processの再起動後にworkflowとjob状態を照会できる。
- 複数job等の拡張は、追加する各機能ごとに仕様・テスト・受入条件を満たす。

## 10. 製品統合時の制約・未実装範囲

- 現行PoCは固定の `poc/ows/.state` と `run_dir\codex-job-cwd\<id>` を使う。製品ではAllowedPathPolicyに沿うstate root/cwd分離が必要である。
- PyInstaller環境の `sys.executable` は通常Python executableではない。Worker用internal modeとEXE梱包は5Bで実装する。
- Bridge shutdownは現状全active turnにinterruptを送る。workflow turn所有権とshutdown policyは5B/5Cで実装する。
- 二重投入を防ぐ `MAYBE_SENT` と、結果照合ができるまでの停止状態は維持する。
- 初期提供範囲は固定single-jobに限定する。任意prompt、複数Codex job、分岐、loop、job cancel、approval拡張は後続設計・実装の対象である。
- 対象はWindows nativeのみ。WSLとDockerは対象外である。
- PoCでスキップした障害注入はPhase 5Aの判断を変えない。一方、各Phaseの通常系・negative testは必要であり、製品化の受入に含める。
- 手動で本番APIへPOSTする検証は行わない。自動テストは自前のloopback fake serverまたはHTTP stubを使用する。

## 11. Phase 5Aの受入と次の実装順

5Aの受入条件は、標準ライブラリだけでimport可能な `workflows.http_client`、PoCと同じhost/port・token・payload・timeout・response size・loopback制約、GETのみのpreflight、固定POST shapeとBearer header、HTTP/JSON/size/redirect回帰テスト、そして本設計書である。HTTPテストは自前の127.0.0.1 fake serverだけに接続し、既定の本番port 18001へ接続しない。Phase 5Aの完了後、5Bの順序1からパッケージ移行を開始する。

## 12. Phase 5C実装記録

Phase 5Cでは、既存UI APIへ次のBearer認証付きrouteを追加した。各routeは既存control token照合とlocalhost Origin検査を通し、token未設定時は公開しない。

- `POST /ui-api/control/workflows`: 128 KiB以下のJSONを受け付け、OWS v1.0.3 subset/schema、固定codex-job prompt、AllowedPathPolicyを検査する。成功時は`202`でworkflow/run IDと状態を返す。
- `GET /ui-api/control/workflows`: 新しい順に最大100件を返す。既定は50件でcursor paginationは未実装。
- `GET /ui-api/control/workflows/{workflow_id}`: request ledger、manifest、DBOS、job ledger、worker statusを読み取り照合する。
- `GET /ui-api/control/workflows/{workflow_id}/result`: 成功かつID/hash整合済みの64 KiB以下のresultだけを返す。再実行・再POSTは行わない。

`%LOCALAPPDATA%\CodexBridge\workflows\requests.sqlite`の`workflow_requests`は、client request ID・workflow ID・run IDのUNIQUE制約、正規化payloadのSHA-256、受付状態とUTC時刻を保持する。受付は`BEGIN IMMEDIATE`で直列化し、run内に`workflow.yaml`、`input.json`、`manifest.json`をatomic保存してから`queued`行をcommitする。commit後の状態遷移は`queued → starting → running`。managerが起動失敗を確定した場合は`failed_to_start`、開始結果を確定できない場合は`needs_reconciliation`とする。GETはSQLiteをread-onlyで開き、run filesのdigestとworkflow IDを照合して状態を投影する。

同じclient request IDと同じ正規化payloadは記録済みworkflow/runを返し、再spawnしない。異なるpayloadは`409`。起動中・結果不明の既存runも再投入せず、`MAYBE_SENT`は`needs_reconciliation`として扱う。異なるrequest IDの同時live実行は1件に制限する。run状態はBridge再起動後も読み取れるが、Workerのautostart/autorecoverはしない。shutdown時は永続request ledgerから確定可能なworkflow所有turnを読み取り、既存の所有turn除外へ加える。

Console UIと自由prompt・複数job対応はPhase 5D/5Eの範囲である。5C実装時の製品検証は隔離fake manager/APIのみとし、本番checkout・稼働中サービス・実Codex jobには接続しなかった。

## 13. Phase 5E-0 実測記録

2026-11-11にWindows nativeの一時環境で固定fixture `codex_job_workflow.yaml` を1回実行した。Bridge/AppServerは検証用token、temp `LOCALAPPDATA`/`APPDATA`/`CODEX_HOME`、loopbackのみの別portで起動し、GitHub mountとCodex MCP設定は無効だった。初期preflightで1回BLOCKEDになったが、workflow POST前であり、証跡は [`phase5e-0-initial-preflight.json`](phase5e-0-initial-preflight.json) に残した。後続のread-only preflightはPASSした。

- 初回 `POST /ui-api/control/workflows`: HTTP 202、client request `phase5e0-5b4bf65d67544bdba428363d8fe6988d`。
- workflow/run/job/thread: `3f056c95-c0e4-4e93-ac4d-e024ef73762d` / `run-904bda7df9944ac5ad432fa944b28892` / `9ec76042-15e1-407e-8092-ff2cee6afda5` / `01a1c992-bd38-70d1-81b9-2ebb54e43f79`。
- Worker exit 0、Codex job `SUCCEEDED`、DBOS `SUCCESS`、thread `completed`、永続結果 `final_text="4"`。実Codex jobは1件。
- 初回pollingは一時的な`unknown`/`needs_reconciliation`で停止した。run-dirをread-onlyで照合し、完了済みstatus/resultを確認後に同じ隔離stateでBridgeを再起動した。再起動後のworkflow一覧・詳細・結果は同じIDで`succeeded`、結果`4`を返した。
- 成功確認後の同一ID・同一payload replayはHTTP 202で同じworkflow/run IDを返した。job row 1件、job event 5件、`MAYBE_SENT` claim 1件、thread 1件は前後で不変。新しいCodex job POSTは0件、再起動後worker数は0。
- 初回とreadback restartのloopback portは `62767/62768` と `61072/61073`。実行後のBridge 3プロセスと観測したdescendant 8プロセスは通常終了し、強制終了は0件。
- sanitized runtime証跡は [`phase5e-0-evidence.json`](phase5e-0-evidence.json)。token、個人パス、生ログは含めない。稼働中本番checkout・Bridge・AppServer、既存workflow state、既存thread、18000/18001への接続・変更は行っていない。
- `ruff check`, `ruff format --check`, `mypy src`, `compileall` はPASS。関連pytestは168 PASS / 2 SKIP（Windows symlink作成権限 `WinError 1314`）。Console関連テストは`QT_QPA_PLATFORM=offscreen`で実行したが、APIを表示する実GUI描画は未確認。

状態の扱い: 保存列`workflow_requests.state`は受付時のdurable admission stateとして更新されるが、Worker完了を検知して必ず`succeeded`へ同期する機構ではない。GETのeffective statusはrun-local worker status、DBOS、job ledger、検証済resultをread-onlyで照合した投影である。次回の新規受付では保存stateだけでcapacityを決めず、既存runの実状態を照合する。このため台帳の`running`は、その時点で実ジョブがまだ稼働中である証拠ではない。この仕様を安全性を保って将来材料化する方法は後続フェーズで検討する。

初回pollingが一時的に`unknown`/`needs_reconciliation`で停止した確定原因は、保存証跡だけでは特定できない。今回修正するWorker正常完了途中の状態投影競合と同一原因とは断定しない。追加診断の再起動後thread-status照会は`completed`以外を返したが、具体値は記録していない。thread自体のpersisted turn stateとWorkflow resultは別の状態観測であり、このthread-statusだけではWorkflow resultを確定しない。workflow一覧・詳細・結果、thread IDの永続表示、job/DBOS状態、同ID replayはPASS。Consoleのoffscreen描画は未確認で、関連pytestに分離した。
