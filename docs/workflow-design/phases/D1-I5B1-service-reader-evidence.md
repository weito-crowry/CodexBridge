# D1-I5-B1 common run format discriminator / service reader evidence

- **実装状態:** ACCEPTED (with documented caveats) — ChatGPT GitHub review 2026-11-20
- **I5-A:** ACCEPTED (with documented caveats) — ChatGPT GitHub review 2026-11-19
- **I5-B1:** ACCEPTED (with documented caveats) — ChatGPT GitHub review 2026-11-20。service reader entry gateのみを対象とします。
- **I5-B2 / I5-B3:** NOT_STARTED。
- **I5全体:** IN_PROGRESS。**D1全体:** DRAFT。**strict run admission:** OFF。
- **B1実装日:** 2026-11-20。
- **B1実装commit:** `475d30e838d5f1e56b805ceef2d9288517177215`。
- **対象branch:** `codex/workflow-d1-i5-manifest-readers`

## 対象と実装

共通discriminator `classify_readonly(...)` を追加し、既存serviceの `_verified_active_workspace(...)` と `_inspect_run(...)` の入口で呼びます。manifestの形式を判別したあと、既存のlegacy control/digest検証より前に分岐します。

- manifestに `run_format_version` がない場合だけlegacy v1候補です。`requests.sqlite` の `user_version=0` ではlegacy table/column metadataとbinding table不在をread-onlyで確認します。`user_version=1` では `StrictWorkflowRequestStore.inspect()` にschema・整合性検査を委譲し、legacy request rowにbindingがない場合だけlegacy pathへ進みます。caller row、manifest、DB request rowのworkflow/run/request digestを照合します。既存legacy digestは再計算・変更しません。
- `run_format_version=2` のときだけ `parse_strict_manifest_v2(...)` を呼びます。v1 schemaは既存 `StrictWorkflowRequestStore` のread APIで照合し、strict bindingとmanifestのworkflow/run/request digest、definition ID/revision/source SHA、contract version/SHA、binding digest、normalized input SHAを一致確認します。
- 明示version 1、unknown/bool/null/malformed version、非object JSON、重複key、DB schema/version/tableの矛盾、破損、request/binding identity不一致は値を含まない `NEEDS_RECONCILIATION` でfail closedします。DBはSQLite URI `mode=ro` と `query_only` で開き、新規作成・migration・repairをしません。
- 有効なstrict v2分類もsnapshotの予約済み完全性確認や実行許可ではありません。現段階の `_inspect_run` はlegacy digest/status/result/DBOS/job readerへ進まず、`needs_reconciliation` / `unknown` / resultなしを返します。`_verified_active_workspace` は `WorkflowConcurrencyConflict` に収束します。I5-Aの `verify_reserved()` を呼ぶ完全reader接続は後続sliceです。

変更した5 path:

- `src/codex_bridge/workflows/strict_reader_gate.py`
- `src/codex_bridge/workflows/service.py`
- `tests/test_workflow_d1_i5b1_service_gate.py`
- `docs/workflow-design/phases/D1-I5B1-service-reader-evidence.md`
- `docs/workflow-design/phases/D1-implementation-plan.md`

request reservation、manifest writer、strict manifest/request/definition、manager、worker、worker entry、task projection、API/MCP、DBOS接続は変更していません。strict runの生成・起動を追加せず、strict run admissionはOFFのままです。

## 検証結果

すべての新規SQLite/manifest fixtureはpytest `tmp_path`内に作成しました。稼働中のBridge/Worker、実Workflow/Codex job、production/shared DB/run directory、DBOS checkpoint、WSL/VM/Dockerにはアクセスしていません。

| 検証 | 結果 |
| --- | --- |
| B1専用 `tests/test_workflow_d1_i5b1_service_gate.py` | PASS — 17 passed、FAIL 0 |
| 既存service `tests/test_workflows_service.py` | PASS — 40 passed、FAIL 0。MAYBE_SENT再送抑止を含む |
| I5-A `tests/test_workflow_d1_i5a_manifest.py` | PASS — 45 passed、FAIL 0。SKIP — 2（Windows symlink権限） |
| I3-B `tests/test_workflow_d1_i3b_request_storage.py` | PASS — 45 passed、FAIL 0 |
| `uv run --no-sync ruff check src/codex_bridge tests/test_workflow_d1_i5b1_service_gate.py` | PASS |
| `uv run --no-sync ruff format --check src/codex_bridge/workflows/service.py src/codex_bridge/workflows/strict_reader_gate.py tests/test_workflow_d1_i5b1_service_gate.py` | PASS |
| `uv run --no-sync mypy src/codex_bridge` | PASS — 143 source files |
| `uv run --no-sync python -m compileall -q src/codex_bridge` | PASS |
| `git diff --check` | PASS |
| Full pytest suite | NOT_RUN |

専用テストはlegacy `requests.sqlite` v0とv1/no-bindingのstatus/result/digest維持、strict v2 bindingとservice status/workspace gate、version欠落/1/unknown/bool/null、missing binding、unknown/corrupt v1 schema、v0のbinding table混在、request identity/hash不整合、malformed/non-object/double-key manifest、missing DB readでDB未作成を確認します。strict readerとserviceのread-only status/getではrequest DB本体、row counts、snapshot内容を比較し、変更がないことを確認します。

Ruff、format、mypy、compileallとdiff checkは記録したB1コード上でPASSです。Windows symlinkの2 SKIPは既存I5-A regressionで発生したもので、PASSとして数えません。full pytestは起動していません。

## 制約と後続作業

- ChatGPTはGitHub上でcommit `475d30e838d5f1e56b805ceef2d9288517177215` を独立レビューし、文書化した留保付きでI5-B1を受入れました（2026-11-20）。
- B1はserviceの `_verified_active_workspace(...)` と `_inspect_run(...)` の2 reader入口だけです。manager、worker、worker entry/start/recover、task projection、graph/status/recovery readerの全数監査は未完了で、全reader coverage gateは未達です。I5-B2/B3を実装してから全reader互換を判定します。
- strict v2はserviceから保守的にreconciliationへ送られます。`StrictWorkflowSnapshotVerifier.verify_reserved()`、I4 variable ledgerとDBOS checkpointの完全照合、strict v2の正常status/result投影は未接続です。
- SQLite read-only接続でもWAL sidecar metadataが変化する可能性があります。主DB file/rowの不変比較は通りましたが、sidecar bytes不変性は主張しません。manifest filesと複数DB間のatomicityもありません。
- I5-AとI5-B1はACCEPTED (with documented caveats)、I5-B2/B3はNOT_STARTED、I5全体はIN_PROGRESSです。D1はDRAFT、strict run admissionはOFFです。

## Stage / rollback

B1では上記5 pathだけを変更します。test DBはpytest一時directoryのみで、共有・production stateへのmigrationやrepairはありません。rollbackはB1 implementation commitを戻し、serviceのdiscriminator呼出しと共通gate module/test/evidence/計画追記を取り除く範囲です。stored workflow/request dataを削除・降格・再計算するrollback操作はありません。
