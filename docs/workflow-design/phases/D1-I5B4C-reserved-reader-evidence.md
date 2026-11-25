# D1-I5-B4C reserved reader and reservation-order evidence

- **実装状態:** IMPLEMENTED / PENDING_CHATGPT_REVIEW。
- **対象branch:** `codex/workflow-d1-i5-b4-verified-readers`。
- **base:** `origin/dev` at `483c341af5b2411ea17710d704d5eb2b9f908d32`。
- **B4B review:** ChatGPT GitHub review of `ac095df0a5b353a739cd6ec3e9e58ea579371f71` はACCEPTED (with documented caveats)。full pytest、CPP/account-auth回帰、実Worker/DBOS/job、WAL metadata未検証の留保を[B4B証跡](D1-I5B4B-verified-projection-evidence.md)に記録しました。
- **今回の範囲:** manager、worker_entry、直接worker readerを予約済みstrict v2検証へ接続し、prepared snapshot用の内部予約順序coordinatorを追加。
- **I5-B4C / I5-B4全体 / I5全体:** ChatGPT review前のためB4CはPENDING_CHATGPT_REVIEW、B4およびI5はIN_PROGRESS。**strict run admission:** OFF。**I2:** DEFERRED / NON-BLOCKING。**D1全体:** DRAFT。

## 実装

`manager._read_manifest`、`worker_entry._read_manifest`、`worker._read_run_manifest` / `load_run_request` は明示的version 2をB4A `verify_strict_reserved_readonly` に通します。managerは `self.allowed_roots`、worker_entryはconfig由来の既存allowed roots、直接workerは `--allowed-root` を渡します。rootsが空またはbinding、snapshot、pinned definition/input、variables ledger、identityの検証が失敗した場合、strict readerは安全なbinding errorで停止し、managed legacyやstandalone legacyへfallbackしません。unknown/malformed versionは従来のunsupported経路です。

worker loaderは予約済み検証結果が保持するpinned definition sourceとnormalized inputを返します。strict snapshotを検証した後に `workflow.yaml` や `input.json` を別読みしません。manifestはstrict形式のまま扱い、legacy control形式へ変換しません。直接 `worker.main` は読み取り検証後にruntime unsupportedで停止します。

manager start/recoverは予約済みv2の検証後、config書込み、lifecycle event、worker spawnより前にruntime unsupportedで停止します。worker_entry mainは同じ検証後、dependency check/import、running status、worker dispatchより前に停止し、既存のfailed diagnosticだけを許可します。

persisted shutdown ownership readerはmanagerのallowed rootsを渡してstrict reservationを検証します。strict v2は検証済みでも候補から除外しownershipを不完全として扱い、legacy `codex_jobs.sqlite` をownership evidenceとして読みません。strict runtime/ownership adapterはI7以降の残件です。

内部 `_reserve_prepared_strict_run` coordinatorは新規runでpre-reservation verification、manifestからのrequest input照合、`reserve_strict` commit、reserved verificationの順に実行します。既存reservationはpre-reservation verifierや再予約を行わず、既存idempotency規則とreserved verifierでreplayを確認します。途中でpost-reservation検証に失敗しても予約を削除・rollback済みと見なさず、次回の再照合に残します。identity conflictと変更済みprepared snapshotを拒否します。このcoordinatorは今回legacy admission/startへ接続していません。

## Reader inventoryと残件

| Reader | strict v2の現在動作 |
| --- | --- |
| service workspace reader（B4A） | B4A verified readerでreservation/snapshotを検証し、検証済みworkspaceを返す |
| service status/result、task/event（B4B） | statusはreconciliation/unknown/non-terminal/resultなし、taskは安全な静的topology、eventは空を返す |
| manager manifest/start/recover（B4C） | 検証済みreader後にruntime unsupported。config/lifecycle/spawnなし |
| worker_entry manifest/main（B4C） | 検証後にruntime unsupported。dependency check/running status/dispatchなし |
| direct worker manifest/loader/main（B4C） | loaderはverified pinned dataを返し、mainはruntime unsupportedで停止 |
| persisted shutdown ownership（B4C） | reserved verification後もstrict候補を除外しownershipを不完全扱い。legacy job ledgerを読まない |
| prepared snapshot reservation coordinator（B4C） | 内部実装済みだが既存admission/startには未接続 |

I6 strict admission接続時には、`_snapshot_for_row` の `forced_state` がinvalid strict run/replayをlegacy forced stateへ上書きしないことを確認します。strict admission OFFの現在はB4Cのblockerではありません。I6の公開API契約、I7のstrict runtime/DBOS/job/output判定、I8のfull pytestと最終acceptanceが残っています。I5全体を完了とは扱いません。

## 変更path

- `src/codex_bridge/workflows/strict_manifest.py`
- `src/codex_bridge/workflows/strict_reader_gate.py`
- `src/codex_bridge/workflows/manager.py`
- `src/codex_bridge/workflows/worker_entry.py`
- `src/codex_bridge/workflows/worker.py`
- `tests/test_workflow_d1_i5b2_reader_gate.py`
- `tests/test_workflow_d1_i5b3_worker_reader_gate.py`
- `tests/test_workflow_d1_i5b4c_reserved_readers.py`
- `docs/workflow-design/phases/D1-I5B4B-verified-projection-evidence.md`
- `docs/workflow-design/phases/D1-I5B4C-reserved-reader-evidence.md`
- `docs/workflow-design/phases/D1-implementation-plan.md`

## 検証

検証はWindows nativeのpytest `tmp_path` DB/snapshotとmockのみです。実行したaggregateにはI5-A/B1/B2/B3/B4A/B4B/B4C、manager、service、multi-APIのfocused回帰を含みます。初回aggregateは **331 passed / 3 failed / 4 skipped** でした。3件はB3のbindingを削除したstrict v2拒否caseが従来の「unsupported」文言を期待していたものです。readerは値非露出の `workflow request binding is invalid` で引き続き拒否していました。期待値だけ更新後、B3全suiteを再実行して **66 passed / 0 failed / 1 skipped**、B4C専用suiteは **14 passed / 0 failed** でした。集計上は **334 passed / 0 failed / 4 skipped** です。

| 検査 | 結果 |
| --- | --- |
| I5-A/B1/B2/B3/B4A/B4B/B4C、manager/service/multi-API focused aggregate | 331 passed、3 stale-message assertions failed、4 skipped。修正後B3全suite 66 passed / 1 skippedで再確認 |
| B4C reserved readers/coordinator | 14 passed、0 failed |
| Ruff check（変更Python source/test paths） | PASS |
| Ruff format check（同上） | PASS — 8 files already formatted |
| mypy `src/codex_bridge` | PASS — 151 source files |
| `python -m compileall -q src/codex_bridge` | PASS |
| `git diff --check` | PASS |
| full pytest | NOT_RUN — I8に残す |
| CPP MCP/account-auth境界回帰 | NOT_RUN |

4件のskipはI5-A symlink 2件、B3 symlink 1件、multi-API symlink 1件で、Windows symlink権限が利用できないためです。隔離fake job fileはownership拒否確認だけに使いました。production/shared DB、live Bridge/worker、実Codex外部job、実Workflow再POSTは使っていません。DBOS/実Worker/jobは未検証です。WAL metadata不変性は主張しません。

## Reviewと残件

- ChatGPT GitHub review待ち。Codex作業終了時点でB4CまたはI5全体を受入済みとは扱いません。
- coordinatorは既存のlegacy admission/startに接続していません。strict admissionはOFFのままです。
- valid reserved v2の読取り検証とruntime実行許可は別です。strict runtimeと実job/output成功判定はI7以降です。
- full pytest、CPP/account-auth回帰、実Worker/DBOS/job、WAL metadataは未検証です。D1全体はDRAFTです。
