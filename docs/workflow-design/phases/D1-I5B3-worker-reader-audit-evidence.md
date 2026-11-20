# D1-I5-B3 worker reader gate / all-reader audit evidence

- **実装状態:** ACCEPTED (with documented caveats) — ChatGPT GitHub review, 2026-11-20。
- **実装commit:** `cd1eb83b5e6a425c6788eab4f7e623de5a3521f2`。
- **I5-A / I5-B1 / I5-B2:** ACCEPTED (with documented caveats)
- **I5-B3:** worker entry と直接 worker reader を拒否ゲートで保護し、現在のtracked-source all-reader inventoryを完了しました。
- **I5-B4:** NOT_STARTED。strict v2の通常reader検証経路とworker reservation順序が後続です。
- **I5全体:** IN_PROGRESS。**D1全体:** DRAFT。**strict run admission:** OFF。
- **開発状態:** I5-B3後でPAUSED（CPP integrationを優先）。再開後の次工程はI5-B4で、ユーザーの再開指示があるまで着手しません。
- **実装日:** 2026-11-20。
- **対象branch:** `codex/workflow-d1-i5-manifest-readers`

## 実装と拒否順序

`worker_entry._read_manifest(...)` と `worker._read_run_manifest(...)` は、manifestをregular fileとして読み、symlink/junctionを拒否します。JSONは既存の `strict_reader_gate._decode_manifest(...)` に渡し、duplicate keyとNaN/Infinityを拒否します。独自のrequest DB schema検査は追加せず、requests.sqliteが存在するmanaged runはB1の `classify_readonly(...)` でbindingを照合します。

明示的な `run_format_version` keyは、値が2、1、3、boolean、null、その他の型でも、値を含まない `workflow run format is unsupported` で拒否します。worker entryではworker-entry statusのrunning書込み、worker import/call、DBOSより前です。許されるfailure statusは `failed / invalid_config` の診断だけです。直接workerでは `load_run_request(...)` の最初で拒否し、workflow.yaml/input.json読込み、hash再計算、worker identity、`_persist_workflow_id(...)`、DBOS、jobやevent出力へ進みません。

run_format_versionのないlegacy manifestは、requests.sqliteがないstandalone runなら従来どおり扱います。requests.sqliteがあればmanifestのworkflow_id/run_id/request_digestの型とrun identityを確認し、classifierが返す `LEGACY_V1` だけを許可します。canonical legacy v0/v1 rowは引き続き受理し、row不在、strict bindingに対するunversioned manifest、破損DB、unknown schema/version、manifest identity欠落または型不正は値を露出せず拒否します。validなstrict v2予約済みrunも、strict format markerによりworker ingressでは拒否します。

## 変更した5 path

- `src/codex_bridge/workflows/worker_entry.py`
- `src/codex_bridge/workflows/worker.py`
- `tests/test_workflow_d1_i5b3_worker_reader_gate.py`
- `docs/workflow-design/phases/D1-I5B3-worker-reader-audit-evidence.md`
- `docs/workflow-design/phases/D1-implementation-plan.md`

service、manager、task_projection、strict_reader_gate、strict_manifest、I3/I4、graph、API/MCP、DB schema/reservation/writerや既存testは変更していません。

## B3 test / regression / static evidence

| 検証 | PASS | FAIL | SKIP | 注記 |
| --- | ---: | ---: | ---: | --- |
| `tests/test_workflow_d1_i5b3_worker_reader_gate.py` | 69 | 0 | 1 | Windows symlink作成権限によるSKIP |
| B1/B2とlegacy workerのfocused regression（manager、task projection、service、fake workerを含む） | 172 | 0 | 1 | `tests/test_workflow_task_projection.py:1456`、Windows symlink作成権限によるSKIP |
| `uv run --no-sync ruff check src/codex_bridge tests/test_workflow_d1_i5b3_worker_reader_gate.py` | PASS | — | — | — |
| `uv run --no-sync ruff format --check src/codex_bridge/workflows/worker_entry.py src/codex_bridge/workflows/worker.py tests/test_workflow_d1_i5b3_worker_reader_gate.py` | PASS | — | — | 3 files formatted |
| `uv run --no-sync mypy src/codex_bridge` | PASS | — | — | 143 source files |
| `uv run --no-sync python -m compileall -q src/codex_bridge` | PASS | — | — | — |
| `git diff --check` | PASS | — | — | — |

初回の専用suite試行では、v1/strict test fixtureをrun directory作成後に初期化しようとしたため28 PASS / 14 FAIL / 1 SKIPでした。fixture初期化順序だけを修正し、product gateは緩めていません。最終suite結果は上表の69 PASS / 0 FAIL / 1 SKIPです。

次はNOT_RUNです: full pytest、実DBOS checkpoint、実worker subprocess / recovery、実Codex job、live Bridge/Worker、production/shared DBまたはproduction run directory。既存 `test_source_worker_entry_runs_an_offline_fixed_workflow` は実worker subprocessとDBOSを起動するため実行していません。隔離test DBと `tmp_path`、worker/manager/job/DBOSのfakeまたはmockを用いました。request DBとsnapshotのmain file bytesが拒否前後で不変であることを比較しましたが、SQLite WAL sidecar metadataの不変性は主張しません。

## all-reader audit matrix

| Reader / entry | 読む内容とside effect | 現在のgate / 判定 |
| --- | --- | --- |
| `service._verified_active_workspace` (B1) | workspace選択前にrun manifest、workflow.yaml、input.jsonを読む | `classify_readonly` が先行。legacy v0/v1だけ既存検証へ進み、strict v2はworkspace conflict/reconciliationへ収束 |
| `service._inspect_run` と `get_result` (B1) | manifest、definition/input、worker status、DBOS status、resultを読む。status/result DTOの基点 | manifest classifierがstatus/result等の読取り前。strict v2はreconciliationとなり、成功status/resultへ投影しない |
| `manager._read_manifest` と `start_worker` / recover (B2) | worker config作成、lifecycle launch記録、process spawn前にmanifest/controlを読む | B1 classifierがmanaged legacy bindingを確認。明示version、DB不整合等はconfig/spawn前に拒否 |
| `manager._persisted_live_run_directories` (B2) | active request row、各run manifest/controlを読みshutdown ownershipを復元 | legacy v0/v1だけownership候補。strict/invalid/unknownは除外し `complete=False` でunknown ownershipを残す |
| `task_projection._verified_run_source`、`project_workflow_tasks`、`project_workflow_task_events` (B2) | manifest、workflow.yaml/input.json、DBOS status、task/job/node-eventデータを読みgraph/task/event DTOを作る | row identity付きclassifierをsource読取り前に実行。strict/不整合はunavailableで空投影。service result/task/event methodsもこのgateを経由 |
| `worker_entry._read_manifest` / `main` (B3) | worker configとmanifestを読む。次にrunning status、worker import/callへ進む経路 | explicit-version拒否とmanaged legacy classifierをrunning status、worker call前に実行。invalid時はfailure diagnosticだけ |
| `worker.load_run_request` / `main` / `_persist_workflow_id` (B3) | manifest、workflow.yaml/input.jsonを読みdigestを計算。main後段でidentity/DBOS/job/task-event/resultを書き、start時にworkflow_idをmanifestへ保存 | loaderの最初にexplicit-version拒否とmanaged legacy classifier。拒否されたrunはsource/digest/identity/DBOS/job/outputへ到達しない。persist writer本体は変更なし |
| `strict_manifest` codec/writer、`verify_snapshot_files` / `verify_pre_reservation` / `verify_reserved` (I5-A) | strict snapshotファイル、request reservation、variables bindingを検証 | opt-in isolated writer/verifier。通常worker/service/manager reader admissionには未接続 |
| `classify_readonly` (B1) | 渡されたmanifest bytesとrequests.sqliteをread-only照合 | legacy identityまたはstrict-v2 bindingを分類するだけ。strict v2実行許可・snapshot実行開始はしない |
| `graph.project_workflow_graph` | 呼出側が渡したmappingから静的graphを生成 | manifest/run-directoryを読まないpure projection。run-state readerではない |
| API/MCP、Console frozen worker wrapper | API/MCPはserviceのresult/task/event methodsへ委譲。Console wrapperはmanager commandで指定されたworker_entryへ転送 | 独自manifest readerなし。service/worker_entry上記gateを継承 |
| `CodexJobAdapter`、`DurableJobAdapter`、`NodeEventWriter`、job service | run-local job/event DBの読書き・job service処理 | manifest readerではない。通常呼出元はworker ingress後、または`task_projection._verified_run_source`後。job serviceはmanifest自体を読まない |

### bounded search evidence

現在のtracked sourceに対し、次のbounded searchesと対象workflow package inventoryを使いました。

```text
rg -n -g '*.py' 'manifest\.json|workflow\.yaml|input\.json|result\.json|worker-entry-status\.json|worker-lifecycle\.jsonl|task-events\.jsonl|dbos\.sqlite|start-worker\.json|recovery-worker\.json' src/codex_bridge/workflows
rg -n -g '*.py' 'worker_entry|workflow_worker|codexbridge-workflow-worker|load_run_request|_persist_workflow_id|project_workflow_tasks|project_workflow_task_events|_inspect_run|_verified_active_workspace|_verified_run_source' src/codex_bridge
rg --files src/codex_bridge/workflows
```

このinventoryは現在のtracked sourceと、そこで確認したfail-closed ingressの範囲に限られます。このtracked-source inventoryで直接のrun manifest/snapshot readerはservice、manager、task_projection、worker_entry、worker、strict_manifestに限られます。判定は `all-reader inventory complete for current tracked-source search` です。これはproduction-readyなv2 workflow、DBOS integration、包括的なfull-suite検証、またはstrict v2を通常のrun/status/result/graph/event/worker flowで検証して受理する実装を意味しません。

## 残件と受入境界

- B3はlegacy workerを維持したまま、strict v2とinvalid bindingをworker ingressで拒否する最終coverage sliceです。strict v2を通常実行する機能は追加していません。
- I5-B1/B2/B3はstrict runをservice reconciliation、projection unavailable、またはworker refusalへ分類します。DBOS checkpoint接続・実strict runの受入証跡はありません。
- worker reservation orderingとstrict-v2 verified normal reader pathはI5-B4で扱います。よってI5はIN_PROGRESSのままです。
- I5-B3は2026-11-20のChatGPT GitHub reviewで、実装commit `cd1eb83b5e6a425c6788eab4f7e623de5a3521f2` が文書化した留保付きで受け入れられました。初回fixture初期化失敗（28 PASS / 14 FAIL / 1 SKIP）と修正後の最終結果（69 PASS / 0 FAIL / 1 SKIP）は別の結果として保持します。
- I5-B3のaccepted coverageはfail-closed ingressまでです。full pytest、実worker subprocess/recovery、DBOS checkpoint、実Codex job、production migrationはNOT_RUNで、strict v2の通常reader/runtime接続やproduction-readyなworkflowを示しません。
- Workflow開発はCPP integrationを優先してI5-B3後でPAUSEDです。再開後はI5-B4からで、ユーザーの指示があるまで再開しません。D1はDRAFT、strict run admissionはOFF、I6〜I8はNOT_STARTEDです。
