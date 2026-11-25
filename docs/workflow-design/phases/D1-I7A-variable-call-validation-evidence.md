# D1-I7-A strict variable-call validation and reader evidence

- **実装状態:** ACCEPTED (with documented caveats) by ChatGPT GitHub review。
- **対象branch:** `codex/workflow-d1-i5-b4-verified-readers`。
- **base:** I6-C commit `61a6ddbb22ce38768289004a2b834b0512119e32` (ChatGPT ACCEPTED with documented caveats)。
- **範囲:** strict contract context付きstatic OWS validator、strict v2 definition/read/graph integration。
- **全体境界:** I6/I6-A/I6-B/I6-CはACCEPTED (with documented caveats)、I7はIN_PROGRESS、I2はDEFERRED / NON-BLOCKING、D1はDRAFT、strict run admissionはOFF。

## 実装

`validate_workflow`と内部validatorはoptionalな`CompiledContract`を受け取ります。strict contractを明示的に渡した場合だけ、既存OWS CallFunctionの`codexbridge-variable`を認識します。許可operationは`initialize`、`assign`、`increment`、`decrement`、`append`、`object_set`、`object_delete`、`variable_delete`です。共通必須keyは`operation`、`declaration_id`、`scope`で、scopeは`workflow`固定、declarationは同一contract内のworkflow ownerに限定します。operation固有key以外、caller指定identity/frame、未知operationは拒否します。

`value`は必要なoperationで必須です。既存`_validate_set_value`がliteral/jq式を扱い、式は既存jq compilerとstrictで利用可能な変数集合を使います。静的JSON literalは`initialize`/`assign`ではdeclarationの`value_schema`へ、`append`では直接指定されたitems schemaへ照合し、nullも対象schemaが許すときだけ通します。`object_set`はpath上のschemaを実行時に解決するため値schemaの静的照合を行いません。`by`は省略時1、bool以外の有限な非負int/floatだけを許可します。`path`はRFC 6901形式を確認し、I4のpointer parserを再利用します。pathの存在とschema適合はruntime operationが担い、このsliceでは解決しません。

strict modeでは`$cbvars`をroot task/action/output等の既存jq式で予約し、`for.each`/`for.at`によるshadowingを拒否します。root input式には`$cbvars`を追加しません。legacy modeのcall allowlist、`$context`/input/output/set semantics、official OWS schema、mock-job/codex-job制約は変更していません。strict `max_live_codex_jobs=1`でCodex jobが0件のdefinitionを静的検証する判断はこのsliceに限定します。ADR 0007の全体statusはProposedのままです。

I3 strict definition storeはcreate/update時のsourceをそのrequestでcompileしたcontract contextで検証し、read/replay revisionは保存済みcontractを先に検証してからsourceをstrict validationします。strict contract omissionによる更新はrequired errorへ収束し、legacy repositoryのvalidationと保存形式は維持します。共通definition validation helperはHTTP/MCPのcontract付きvalidateに使い、contractをcompileしてからsourceを検証します。HTTP/MCP definition graphとverified task/event graphは、保存またはreserved verifierで検証されたcontract contextを渡します。strict task/event projectionはI6 contractどおりverified pinned definition由来のstatic topology/空eventsに限定され、入力値やvariable値をprojectionしません。reserved snapshot verifierのdefinition readはI3 strict storeを経由するため、actual contract付きrevision validationが適用されます。

worker、manager、worker_entryのstrict runtime拒否は維持しました。run admission、reservation作成、DB migration、variable store mutation/read adapter、DBOS実行、output validation、成功判定はこのsliceでは追加していません。valid strict snapshotの静的検証と実行許可は別です。

## 検証

| 検査 | 結果 |
| --- | --- |
| I7-A専用validator/store/HTTP/MCP/graph/reader suite | 29 passed |
| I1/I3/I4/I5/I6およびdefinition/service/manager/API/MCP/task/graph/codex-job focused aggregate | 947 passed / 11 skipped / 0 failed |
| Ruff check（変更Python files） | PASS |
| Ruff format check（変更Python files） | PASS |
| mypy `src/codex_bridge` | PASS — 154 source files |
| `python -m compileall -q src/codex_bridge` | PASS |
| `git diff --check` | PASS |
| Full pytest / CPP / account-auth / live API / real Worker / DBOS / external job / WAL metadata | NOT_RUN |

aggregateは最終validator変更後の同一treeで実行しました。11 skipはWindows symlink privilege unavailableのtest casesです。全実行はWindows native上の隔離`tmp_path`/test clientです。production/shared DB、live Bridge/worker、実Codex jobは使用していません。

## 残件と境界

- ChatGPT GitHub review of `8dc2399dcd443de13f6d60119a08ef343ba01d09` はI7-AをACCEPTED (with documented caveats)としました。レビュー時は947 passed / 11 skipped、専用suite 29 passedです。full pytest、CPP/account-auth、live API、実Worker/DBOS/job、WAL metadataは未検証です。受入はstatic validator/definition-reader接続までで、runtime semantic mismatchesはI7-Bで対応します。
- I7-BではI4 variable store adapter、root ACTIVE frame連携、strict `$cbvars` root projection/missing semantics、mutation ledger/replayを接続します。
- I7-Cではworker/DBOS/output validation/reason mappingとisolated strict admission integrationを扱います。I6-C review caveatのcorrupt request-source ledger replayでID保持前にunavailableとなり得る境界をここで再確認します。
- I8 isolated acceptance/full pytest/go-no-go、strict admission enablement、D1最終承認は未実施です。I2はDEFERRED / NON-BLOCKINGです。
- D1はDRAFT、strict run admissionはOFFです。I7-B runtime接続もD1/ADR 0007全体の承認を意味しません。

## 変更path

- `src/codex_bridge/workflows/definition_api.py`
- `src/codex_bridge/workflows/definition_contract.py`
- `src/codex_bridge/workflows/definition_mcp.py`
- `src/codex_bridge/workflows/definitions.py`
- `src/codex_bridge/workflows/graph.py`
- `src/codex_bridge/workflows/interpreter.py`
- `src/codex_bridge/workflows/strict_definition_repository.py`
- `src/codex_bridge/workflows/strict_definitions.py`
- `src/codex_bridge/workflows/task_projection.py`
- `tests/test_workflow_d1_i7a_variable_call_validation.py`
- `docs/workflow-design/phases/D1-I6C-saved-run-status-result-evidence.md`
- `docs/workflow-design/phases/D1-implementation-plan.md`
- `docs/workflow-design/phases/D1-I7A-variable-call-validation-evidence.md`
