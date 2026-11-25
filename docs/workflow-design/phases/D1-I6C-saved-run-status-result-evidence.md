# D1-I6C saved-run status, result, and replay evidence

- **実装状態:** IMPLEMENTED / PENDING_CHATGPT_REVIEW。
- **対象branch:** `codex/workflow-d1-i5-b4-verified-readers`。
- **base:** I6-B commit `8626667daaf78985d164d3fd5d1efd8a363ac9b5` (ChatGPT ACCEPTED with documented caveats)。
- **範囲:** strict saved-run request preflight、request-id replay boundary、HTTP/MCP status/result/task/event metadataとsafe error mapping。
- **全体境界:** I6はIN_PROGRESS、I6-A/I6-BはACCEPTED (with documented caveats)、I2はDEFERRED / NON-BLOCKING、D1はDRAFT、strict run admissionはOFF。

## 実装

保存済みimmutable revisionにserver-side strict contract markerがある場合だけ、run-start前にread-only preflightします。contract/source metadataを再照合し、I1 compilerと`normalize_input`でobject input、defaults、resource limitを確認します。省略inputは`{}`、strict explicit nullはinvalidです。HTTPは既存128 KiB request capとunknown-field拒否を保ち、MCP `workflow_run_start`はschema default factory sentinelと実tool invocationで省略/nullを区別します。legacy MCPのexplicit nullは従来どおり空objectです。追加のclient contract/version指定は拒否します。

preflightはI1 normalized input digestと既存`strict_request_digest_v2`を計算し、既存AllowedPathPolicyとstate/workspace separationを確認します。入力不正は`workflow_input_invalid`、resource limitはHTTP 413 / `workflow_resource_limit`、strict contract unavailableは503 / `workflow_contract_unavailable`へ値非露出で写像します。secretについて新しい未定義値検査は追加していません。

既存client request IDは新規run allocationより前に読み取り検索します。同一normalized request digest・saved revisionのstrict reservationはB4A reserved verifierで再検証し、同じworkflow/run IDを`needs_reconciliation`として返します。異なるdigest/mode/revisionはconflictです。reservation/snapshotがinvalidならIDと安全な`run_integrity_invalid`だけを残し、未検証hashを公開しません。新規strict requestはadmission OFFのためservice unavailableで停止し、request DB、run directory、workerを作りません。strict rowがlegacy start/replayへ入りlifecycle mutationやworker再送を受けないようにし、`_snapshot_for_row`の`forced_state`もstrict candidateの不整合を上書きしません。

verified reservationからのみallowlisted `run_format_version`、`contract_version`、`contract_sha256`、`binding_digest`、`reason_code`をstatus/list/result/task/eventへ追加します。legacy DTOには新fieldを追加しません。strict runtimeは未接続なのでstatusは`needs_reconciliation`、worker stateは`unknown`、resultなし・nonterminal、reasonは`strict_execution_uncertain`です。taskは検証済みpinned definitionの既存static graphのみ、eventは空です。Graph validatorが未対応のstrict nodeはgraph invalid/partialで返し、legacy journal、DBOS、job、artifact、resultをevidenceに採用しません。HTTP/MCP result reconciliationはworkflow/run IDsと安全なreason/verified binding metadataを含む既存409形です。既存API、tool、response caps、permission boundaryを維持しました。

## 検証

| 検査 | 結果 |
| --- | --- |
| I6-C専用 saved-run / actual MCP invocation suite | 11 passed |
| I5-A/B1/B2/B3/B4A/B4B/B4C、I6-A/B/C、HTTP/MCP/service/task projection focused aggregate | 419 passed / 6 skipped / 0 failed |
| Ruff check（変更Python files） | PASS |
| Ruff format check（変更Python files） | PASS |
| mypy `src/codex_bridge` | PASS — 154 source files |
| `python -m compileall -q src/codex_bridge` | PASS |
| `git diff --check` | PASS |
| Full pytest / CPP / account-auth / live API / real Worker / DBOS / external job / WAL metadata | NOT_RUN |

Focused aggregateの6 skipはこのWindows hostでtest symlinkを作成できないためです。testsは`tmp_path` fixtureとfake/service test clientを使用し、production/shared DB、live Bridge/worker、実Codex jobは使用していません。初回aggregateで旧I6-A test inputがstrict schemaに無効な値であることが判明したため、admission-disabled boundaryを検証する有効な空objectへfixtureを直し、対象testとaggregateを再実行しました。最終集計は上表の値です。

## 残件と境界

- I6-CはChatGPT review待ちで、受入済みとは扱いません。I6はIN_PROGRESSです。
- strict admissionはOFFのままです。新規strict reservation、run directory作成、worker起動、DB migration、strict runtime execution、output validationはありません。
- I7でstrict runtime/output validation/reason mapping/実行接続が残っています。I8のenablement・full acceptanceも未実施です。
- I6-B ChatGPT review of `8626667daaf78985d164d3fd5d1efd8a363ac9b5` はACCEPTED (with documented caveats)です。review時は242 passed / 3 skipped、I6-B専用suiteは5 passed / 1 skippedでした。full pytest、CPP/account-auth、live API、real Worker/DBOS/job、WAL metadataは未検証です。
- D1全体はDRAFT、I2はDEFERRED / NON-BLOCKINGです。

## 変更path

- `src/codex_bridge/workflows/service.py`
- `src/codex_bridge/workflows/api.py`
- `src/codex_bridge/workflows/definition_api.py`
- `src/codex_bridge/workflows/mcp.py`
- `src/codex_bridge/workflows/run_mcp.py`
- `src/codex_bridge/workflows/task_projection.py`
- `tests/test_workflow_d1_i6c_saved_run_contract.py`
- `tests/test_workflow_d1_i5b2_reader_gate.py`（input default fixture option）
- `tests/test_workflow_d1_i5b4b_verified_projection.py`（unsupported graph metadata）
- `tests/test_workflow_d1_i6a_definition_repository_http.py`（valid strict input for admission-disabled boundary）
- `docs/workflow-design/phases/D1-I6B-mcp-runtime-repository-evidence.md`
- `docs/workflow-design/phases/D1-implementation-plan.md`
