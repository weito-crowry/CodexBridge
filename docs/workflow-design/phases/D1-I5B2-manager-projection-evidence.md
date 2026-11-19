# D1-I5-B2 manager / task projection reader evidence

- **実装状態:** IMPLEMENTED / PENDING_CHATGPT_REVIEW
- **I5-A / I5-B1:** ACCEPTED (with documented caveats)
- **I5-B2:** PENDING_CHATGPT_REVIEW。対象は manager と task/event projection の reader gate です。
- **I5-B3:** NOT_STARTED。worker、worker entry/start/recover、および最終全reader監査は未完了です。
- **I5全体:** IN_PROGRESS。**D1全体:** DRAFT。**strict run admission:** OFF。
- **実装日:** 2026-11-20。
- **対象branch:** `codex/workflow-d1-i5-manifest-readers`

## 対象と実装

`WorkflowWorkerManager._read_manifest(...)` は、`run_format_version` key が明示された manifest を値に依存しない同一エラーで拒否し、worker config の作成・process spawnより前に停止します。`requests.sqlite` がない standalone legacy manager は従来どおり扱います。DBが存在する場合は read-only の `classify_readonly(...)` で request row と manifest の workflow/run/request digest を照合し、`LEGACY_V1` だけを許可します。DB破損・未対応schema・row欠落・strict binding・不一致はfail closedです。DB作成、migration、修復は行いません。

`_persisted_live_run_directories(...)` はactive request rowごとに同じdiscriminatorを使います。有効なlegacy v0/v1 runだけをshutdown ownership候補に含め、strict v2、binding不一致、破損manifestは候補から除外して `complete=False` にします。他の有効runの列挙は続け、shutdown reportにunknown ownershipを残します。

共有関数 `_verified_run_source(...)` はmanifest bytesを先に読み、row identityを渡して `classify_readonly(...)` を呼びます。legacy control/digest、YAML/input、graph、journal、Codex job ledgerを読む前に `LEGACY_V1` を確認し、それ以外は値を露出しない `run_integrity_invalid` としてtask projection/event paginationを `coverage=unavailable`、空のnodes/edges/eventsで返します。MAYBE_SENTおよびlegacy projection DTO/coverage/truncation処理は変更していません。

変更した5 path:

- `src/codex_bridge/workflows/manager.py`
- `src/codex_bridge/workflows/task_projection.py`
- `tests/test_workflow_d1_i5b2_reader_gate.py`
- `docs/workflow-design/phases/D1-I5B2-manager-projection-evidence.md`
- `docs/workflow-design/phases/D1-implementation-plan.md`

service.py、strict reader/manifest、I3/I4、worker/worker_entry、graph.py、API/MCP、既存tests、DB schema、reservation/writerは変更していません。テストは `tmp_path` 内の隔離DB/manifestを使い、process spawnはfakeに置き換えています。稼働中Bridge/Worker、実Workflow/Codex job、共有/production DB・run directory、実DBOS、WSL/VM/Dockerは使用していません。

## 検証結果

| 検証 | 結果 |
| --- | --- |
| `tests/test_workflow_d1_i5b2_reader_gate.py` | PASS — 30 passed、FAIL 0 |
| 既存manager/task projection/service/B1専用回帰（4 filesを一括実行） | PASS — 134 passed。FAIL — 1。SKIP — 1（Windows symlink作成権限） |
| 既存回帰FAILの内容 | `test_shutdown_preserves_turns_from_durable_requests_after_manager_recreation` は、B1が認識するlegacy v0/v1 schema要件を満たさない `workflow_requests` 単独tableを完全なownershipとして期待します。今回のdiscriminatorはそのrunを検証済みlegacyとして列挙せず、ownershipをincompleteにします。既存testは変更していません。 |
| `uv run --no-sync ruff check src/codex_bridge tests/test_workflow_d1_i5b2_reader_gate.py` | PASS |
| `uv run --no-sync ruff format --check src/codex_bridge/workflows/manager.py src/codex_bridge/workflows/task_projection.py tests/test_workflow_d1_i5b2_reader_gate.py` | PASS |
| `uv run --no-sync mypy src/codex_bridge` | PASS — 143 source files |
| `uv run --no-sync python -m compileall -q src/codex_bridge` | PASS |
| `git diff --check` | PASS |
| Full pytest suite | NOT_RUN |
| DBOS checkpoint / strict execution | NOT_RUN |

新規suiteはmanager start/recoverにおけるversion 2/1/3/bool/null拒否、config/spawn不在、legacy standalone、legacy v0/v1 binding照合、DB不在row・破損・未対応schema、persisted ownership、strict binding付きunversioned manifest、strict/v2・unknown version・missing binding・corrupt DB・malformed manifestのprojection unavailableを確認します。strict projection fixtureはI3/I5-Aの隔離store/writer/reservationで作り、reserved verifierも通します。request DB main bytesとsnapshot main bytesはprojection前後で比較し、WAL sidecar bytes不変性は主張しません。

## 制約と後続作業

- このsliceが扱うのはmanagerとtask/event projectionだけです。有効なstrict v2もmanagerでは起動できず、projectionでは意図的にunavailableとなります。strict run実行・worker有効化は追加していません。
- B1 serviceの2入口およびB2 reader以外に、worker、worker_entry、start/recover reader、status/recovery等の経路が残っています。I5-B3の接続と最終all-reader auditが終わるまで全reader coverageを主張しません。
- DBOS checkpointは未接続・未検証です。full pytestは未実行です。I5-A/I5-B1はACCEPTED (with documented caveats)、B2はreview待ち、B3はNOT_STARTEDです。D1はDRAFT、strict run admissionはOFFです。
- SQLite read-only接続でWAL sidecar metadataが変わる可能性があります。比較したmain DB/snapshot bytesの不変性のみを記録します。

## Stage / rollback

B2では上記5 pathだけを変更します。shared schema、request reservation、manifest writerに変更はなく、strict admissionはOFFです。問題があればB2のmanager/projection reader gateと専用test/evidence/plan記録をこのsliceのcommit単位で戻せます。stored workflow/request dataの削除、降格、再計算は行いません。
