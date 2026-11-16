# Phase B5-R4 隔離Windows実機受入結果

## 判定

**B5-R4: PASS**。修正commit後のimport/HEAD preflightとGate 0がPASSし、Gate 1の隔離Control/MCP管理、Gate 2の保存済みrevision実行と結果照合、成功後の同一ID replay、Gate 3のread-only restartまで完了した。実Codex jobはこのattemptで1件のみ。

- Issue: #146
- branch: codex/workflow-phase-b5-r4-acceptance
- 開始parent: c6676ffe1c22cbed3c6f34e99a5e18518d0b7e0e
- guard/test修正commit: c7d127fdfd985ae8031016a8374b90d9e77665a9
- formatting-only follow-up commit: 9dbb1b85433ab71694faa85b25bcb5812422fafc
- 実機runnerのsource HEAD: 9dbb1b85433ab71694faa85b25bcb5812422fafc
- runner SHA-256: 25807e7f3ad98aff52152f98b88816ae3a13cedc266ed066a2e16b987790d8c3
- attempt ID: d13a3795821d4b578010f832c321d8a5
- evidence: [phase_b5_workflow_acceptance-d13a3795821d4b578010f832c321d8a5.json](phase_b5_workflow_acceptance-d13a3795821d4b578010f832c321d8a5.json)

## HEAD guard修正

PARENT_HEADはce05ba54d9d47f9160a9b1a3f571f0239cad4983に固定した。_git_head()は40桁のHEADとancestor関係を確認し、その後git diff --name-only -zのcommit差分を許可pathだけに限定する。許可対象は専用runner、専用test、docs/phase_b5_*.md、新しい32桁hex evidence名。差分取得失敗、不正なNULレコード、未許可path、対象src/scripts/testsに未commit変更がある場合は安全なBLOCKED reasonで停止する。作業中の既存未追跡ユーザーデータである.codexbridge-staging/とtmp/はstatus pathspecに含めず、閲覧・変更していない。

新しいfake _run_command testは、許可された子孫commit差分、同一parentでの空diff、非祖先、src変更、無関係docs、diff失敗、不正出力、不正HEAD、未commitの危険なsource変更を確認する。RED時は新規6件が既存の完全一致guardにより失敗し、修正後は9件PASS。許可path判定の外部GitHub、HTTP、MCP、Codex操作はない。

修正commit後、指定のread-only module importでB5_IMPORT_OKを確認し、_git_head()は実HEAD 9dbb1b85433ab71694faa85b25bcb5812422fafcを返した。

## Gate結果

| Gate | 判定 | 確認事項 |
|---|---|---|
| Gate P | **PASS** | module import、helper import、ancestor/path guard、実HEAD一致。 |
| Gate 0 | **PASS** | runner evidenceの対象回帰970 passed / 8 skipped / 0 failed、静的検査全PASS。 |
| Gate 1 | **PASS** | 新しい隔離root、独立state/workspace、専用token、dynamic loopback portsでControl/MCP定義管理。 |
| Gate 2 | **PASS** | saved revision 2の初回HTTP POST 1件、実Codex job 1件、durable/result照合成功、成功後の同一ID MCP replay 1回。 |
| Gate 3 | **PASS** | 同じ隔離rootのBridge read-only restart、MCP/HTTP/Qt readback、追加worker/POSTなし、owned process cleanup完了。 |

### Gate 0テストと静的検査

専用B5 suiteは38 passed / 0 skipped / 0 failed。対象4群は以下のとおり。B5 38件はConsole/smoke群に含まれるため、合計に重複加算していない。

| 対象群 | Passed | Skipped | Failed / errors |
|---|---:|---:|---:|
| Workflow API/MCP/saved definitions | 107 | 2 | 0 |
| service/API/multijob/manager | 439 | 5 | 0 |
| Console/smoke/B5 runner | 269 | 1 | 0 |
| OWS regression | 155 | 0 | 0 |
| **合計** | **970** | **8** | **0** |

8 skipはWindows symlink privilege条件。全pytestは実行していない。ruff check、ruff format --check（296 files）、mypy（128 source files）、compileall、uv lock --check、git diff --checkはすべてPASS。初回format checkの指摘はformatting-only commitで修正し、その後PASSを確認した。

### Gate 1: 隔離Control/MCP

runner evidenceでは隔離root/state/workspace、control/MCP authority分離、独立token、GitHub MCP mount無効、LAN無効、既定port 18000/18001回避、隔離認証copy検証がPASS。認証内容はevidenceに含めていない。

HTTP/MCP検証:

- 新Workflow MCP tool 10件を発見し、旧Codex toolsも残存。
- MCP validate/createはrevision 1でPASS。コメントのみの更新でrevision 2となり、source hashは変化、canonical hashは不変。
- HTTP定義list/history/graphはPASS。
- bearer不一致403、悪意Origin403、Host不一致400。
- stale ETagは412、同一mutation再送は200でrevision増加なし、同ID異payloadは409。
- permission-negative catalog検査はPASS。real_permission_processはNOT_RUNで、検査methodはtargeted offline catalog regression tests。

### Gate 2: saved revision実行と照合

revision 2の初回HTTP POSTは202で1件、unique initial request IDも1件。追加の新規IDは0。実Codex jobは1件、job ledgerはSUCCEEDED、DBOSはSUCCESS、Workerはcompleted/exit 0。job row 1件、job event 5件、worker spawn 1件、Codex start/success marker各1件を記録した。receiptとrun resultは固定期待値に完全一致し、raw prompt/resultは保存していない。source/manifestのhash prefixとrevision 2 provenanceの照合もPASS。

保存済みWorkflow statusはread-only GETで次の順に観測。各応答はHTTP 200、omitted count 0。

| Status | First–last elapsed ms | Poll count |
|---|---:|---:|
| running | 75–562 | 2 |
| needs_reconciliation | 1067–1067 | 1 |
| running | 1663–5756 | 9 |
| succeeded | 6261–6261 | 1 |

needs_reconciliationの観測後もread-only pollingが継続し、runningを経てsucceededに到達した。workflow_requests rowのstateはevidence上runningだった一方、HTTP/MCP detailsはsucceeded、Codex job ledgerはSUCCEEDED、DBOSはSUCCESS、Workerはcompleted/exit 0で、durable/result gateはPASS。このstate値は隠さず記録する。

成功確認後に限り同じIDでMCP replayを1回実施し、既存Workflowを再利用。追加Workflow 0、追加Codex job 0。異revisionとinline衝突はHTTP 409で、追加jobは0。MCP list/get/result、HTTP list/detail/result、Qt offscreen readbackはPASS。

### Gate 3: read-only restartとcleanup

同じ隔離root上でowned Bridgeをread-only restartし、saved revision/history、MCP/HTTP readback、Qt offscreen、job/request ledger不変を確認。restart時のworkflow POSTは0、追加Workerは0。初回Bridgeとrestart Bridgeのidentity確認、graceful shutdown、reapedはPASS。force terminationなし、orphan processは各0。

## 実行回数と残る未実施項目

- 実機runner起動: **1回**（module形式、終了コード0）
- 初回HTTP Workflow POST: **1**
- 実Codex job: **1**
- 成功後same-ID MCP replay: **1**
- conflicting HTTP posts: **2**（revision/inline各409）
- production Bridge/ports 18000/18001/state: 未接続・未変更。認証原本は変更せず、隔離先copyだけを使用。
- 旧B5/R2/R3 attempt: 読取・再利用・再送・再開・照会・復旧なし。旧B5 remote outcome UNKNOWNを維持。

runner evidenceでNOT_RUNとなったreal_permission_process以外に主要Gateの未完了はない。B5-R4の隔離受入はPASS。PR、merge、本番反映は行っていない。
