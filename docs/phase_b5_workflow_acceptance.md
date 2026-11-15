# Phase B5 Workflow 統合受入報告

実施日: 2026-11-16
対象親HEAD: `6d8b6b941a84d81694de5126e07998e3399ebf3e`
ブランチ: `codex/workflow-phase-b5-integrated-acceptance`

## 最終判定

**BLOCKED**。Gate 0とGate 1はPASSしました。Gate 2では保存Workflowの初回HTTP POSTが202で受理され、Codex job ledgerに1 jobが作られましたが、Workflow statusが`needs_reconciliation`となり、期待結果を確認できませんでした。指定どおり追加Workflowを開始せずread-only診断で停止したため、同一ID MCP replayとGate 3のBridge再起動は実施していません。B5全体の受入PASSとは判定しません。

## Gate別の結果

| Gate | 判定 | 確認内容 |
| --- | --- | --- |
| Gate 0 オフラインpreflight・対象回帰 | PASS | Windows native / Python 3.14 / 必要依存 / Codex CLI / 認証ファイルの存在を確認。認証内容・個人パスは記録していません。 |
| Gate 1 隔離MCP・Control API | PASS | 一時root、専用token、ランダムloopback port、MCP tool catalog、定義revision/hash、HTTP ETag、ControlのBearer/Origin/Host拒否を確認。 |
| Gate 2 保存revision実Codex E2E | BLOCKED | HTTP初回POSTは202。Workflowは`needs_reconciliation`、最終結果はNOT_OBSERVED。追加の初回POSTやMCP replayは行っていません。 |
| Gate 3 read-only Bridge再起動 | NOT_RUN | Gate 2が未完了のため、同じstate rootでのBridge再起動を行っていません。 |

### Gate 0の回帰・静的検査

34個の指定テストパスを4群に分けて実行しました。

| テスト群 | PASS | SKIP | FAIL / ERROR |
| --- | ---: | ---: | ---: |
| Workflow API/MCP/saved definitions | 107 | 2 | 0 |
| Workflow service/API/multijob/manager | 439 | 5 | 0 |
| Console・既存smoke・B5 runner | 245 | 1 | 0 |
| OWS/DBOS/job adapter | 155 | 0 | 0 |
| **合計** | **946** | **8** | **0** |

SKIP 8件はPASSへ合算せず、独立した件数として記録しました。全pytestは実行していません。

`ruff check`、`ruff format --check`、`mypy src`、`compileall`、`uv lock --check`、`git diff --check`、`git diff --cached --check`はすべてPASSしました。Gate 2停止後にB5専用テストを再実行し、15件PASSしました。runnerのread-only ledger query修正後に全34パスは再実行していません。製品ソースは変更していません。

### Gate 1の確認

- MCP Streamable HTTPで新Workflow tools 10件と既存Codex toolsを確認しました。MCP接続には隔離tokenと許可loopback Originを使用しました。
- Definition validate/create、Control list/get/history/graph、コメントだけのrevision 1→2、source hash変更/canonical hash不変を確認しました。
- If-Match staleは412、mutation同条件replayは200でrevision増加なし、同mutation IDの異なるpayloadは409でした。
- Control APIのBearer不一致は403、悪意Originは403、悪意Hostは400でした。
- permissionなしのcatalog非公開は対象offline catalog regressionでPASS。専用実プロセスによるpermission負テストはNOT_RUNです。

## Gate 2 read-only診断

### 送信数

- 初回HTTP saved Workflow POST: **1**（202 accepted）
- 初回のunique `client_request_id`: **1**
- 初回後に発行した別 `client_request_id`: **0**
- 同一IDのrevision/inline conflict HTTP POST: **2**（両方409）
- 同一ID MCP replay: **0**（結果確定前のため未実施）
- Codex job ledgerのjob数: **1**
- Codex job ledgerの`MAYBE_SENT` marker: **1**

### SQLite / manifest / DBOS / job ledger

停止後、当該attemptの一時stateだけをSQLite read-only (`mode=ro`, `query_only`) で読みました。

- `workflow_requests`: 1 row、state `running`。初回受付のworkflow/run prefixは`38669007` / `run-b18e2ab1`。
- `workflow_request_sources`: 1 row、revision 2、source SHA-256 prefix `ae431f5f21b6`。Gate 1で確認したrevision 2と一致しました。
- manifest: `workflow_source=ui-api`、`mode=codex-job-live`、workflow/run identity、workspace、request digest、canonical definition hashが一致しました。canonical hash prefixは`8cf15283a268`、request digest prefixは`15ab8480881a`。
- DBOS: `PENDING`。
- Codex job: 1 row、task `do[0].calculate`、state `RUNNING`、remote state `in_progress`。eventは`PREPARED`, `MAYBE_SENT`, `ACCEPTED`, `RUNNING`の4件でした。
- 固定入力と隔離workspaceの一致は確認できました。job receiptと`result.json`はなく、結果照合はNOT_OBSERVEDです。
- worker entry statusは`failed`, exit code 1でした。worker bootstrap記録とidentity recordのPID値は一致しましたが、成功時に行う実プロセスsnapshotによるancestry/creation-time pair照合は完了しておらず、NOT_OBSERVEDです。

`needs_reconciliation`を観測した後、同一ID replay、別IDでの再実行、Bridge再起動は行っていません。保存state上ではjobがRUNNING/in_progressのままで、Codexの最終応答は確認できていません。原因は未確定です。

### プロセスとAPIの受入状況

- 初回隔離Bridge: identity確認済み、graceful shutdown、reaped、強制終了なし、孤児process 0。shutdown時に追跡したdescendantは9件でした。
- HTTP: 初回accepted 202とstatus `needs_reconciliation`を確認。list/detail/resultの成功投影はNOT_RUN。
- MCP: Gate 1の定義操作・tool discoveryはPASS。run list/get/resultとsame-ID replayはNOT_RUN。
- Qt offscreen WorkflowManagementDialog readback: NOT_RUN。
- Gate 3のread-only再起動・ledger不変確認: NOT_RUN。

## POST前に停止したattempt

5件のunique evidenceを保持しています。前4件は初回Workflow POST 0件、Codex job 0件でした。

| Evidence | 結果 | 停止理由 |
| --- | --- | --- |
| [8635bf624fcc46aaaf7de46aa887b59a](phase_b5_workflow_acceptance-8635bf624fcc46aaaf7de46aa887b59a.json) | BLOCKED、POST 0 | Gate 0がproduction default portの空きを過剰に要求。後続では既定portを使用せず、実際に予約するdynamic portだけを検証するよう修正。 |
| [180fbc33d8e149fbbb546b1372e98391](phase_b5_workflow_acceptance-180fbc33d8e149fbbb546b1372e98391.json) | BLOCKED、POST 0 | 自分で予約中のportを解放前に再bind確認していた。解放後に確認するよう修正。 |
| [d21af485d0e64ec8b8604b09078dae5e](phase_b5_workflow_acceptance-d21af485d0e64ec8b8604b09078dae5e.json) | FAIL、POST 0 | 仕様対象外の認証なしMCP negative probeを削除。指定のBearer/Origin/Host拒否はControl APIで検証し、MCPは専用Bearerと許可Originで確認。 |
| [9270845be5914a2192075fe6da925b95](phase_b5_workflow_acceptance-9270845be5914a2192075fe6da925b95.json) | BLOCKED、POST 0 | mutation-conflict payload作成時のrunner `ValueError`。異なるYAML commentを使うhelperへ修正。 |

## 最終attemptと成果物

- 最終attempt: [6f061e434b3949b889a6b9bb673e70ee](phase_b5_workflow_acceptance-6f061e434b3949b889a6b9bb673e70ee.json)
- Runner実行時SHA-256: `2e15f91180f04e6bad25abd07f51198b2b0ff8536d5ea4d2304343d8bdd7dbfa`
- 受入成功経路で未実行だったjob ledger queryを、Gate 2後のread-only schema確認に合わせて修正しました。修正後のrunner SHA-256: `08a894cca1ad22835df409e12fcdea41cf98f3fb5af9debcf27f2ad0d8924d25`。修正後はB5 unit 15件と静的検査を実行済みですが、実Codexの再実行はしていません。
- Runner: `scripts/phase_b5_workflow_isolated_smoke.py`
- 専用テスト: `tests/test_phase_b5_workflow_isolated_smoke.py`
- Evidence: `docs/phase_b5_workflow_acceptance-<attempt-id>.json`（上記5件）
- この報告: `docs/phase_b5_workflow_acceptance.md`

実験結果はBLOCKEDのまま記録します。`B5_VERIFY_OK`の受信、同一ID MCP replay、Gate 3再起動は確認していません。
