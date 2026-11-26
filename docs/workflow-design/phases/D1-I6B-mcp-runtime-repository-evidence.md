# D1-I6B MCP definition and runtime repository evidence

- **実装状態:** ACCEPTED (with documented caveats)。
- **対象branch:** `codex/workflow-d1-i5-b4-verified-readers`。
- **base:** I6-A commit `36ab6a8ece2a1ffd6fea03065bd901f4d811fe40` (ChatGPT ACCEPTED with documented caveats)。
- **範囲:** 既存MCP definition toolsへのcontract surface接続と、startup時のread-only definition repository selection。
- **全体境界:** I6はIN_PROGRESS、I5はreader/reservation scopeでACCEPTED、I2はDEFERRED / NON-BLOCKING、D1はDRAFT、strict run admissionはOFF。

## 実装

既存6 MCP toolsだけを維持し、definition list/get/graph/validate/create/updateにI6-Aのshared contract compiler、serializer、typed errorを接続しました。validate/create/updateのoptional `contract`はPydantic `Field(default_factory=...)`と内部sentinelを使い、実MCP schemaではoptional object-or-nullになり、JSON schemaへsentinel defaultを出しません。実MCP invocationで省略とexplicit nullを区別します。MCP contextから元のarguments mappingを確認し、frameworkのargument modelが破棄する未知のtop-level argumentを既存safe invalid errorへ写像します。tool名、既存required引数とdefault、read/write/execute permission登録は維持しました。

Strict list summaryとgraphはcontract version/hash/binding digestだけを返し、detailだけがcanonical contract objectを返します。create/updateは既存metadataにstrict binding metadataを追加します。Legacy DTO field setは追加なしです。validateはv1 repositoryでもpure compileし、metadataにcontract version/hashを含めます。v1/unprepared repositoryへのvalid strict writeは `workflow_contract_unavailable`、null/不正contractは `workflow_contract_invalid`、strict updateの省略/nullは `workflow_contract_required`、legacy updateのexplicit nullはinvalidになります。HTTP/MCPは同じstrict storeの既存mutation request id、expected revision、contract digest/replay規則を利用します。

`server.build_runtime`は一箇所のselectorでrepositoryを選び、同一instanceをUI APIと`BridgeRuntime.workflow_definitions`へ渡します。selectorはread-onlyでSQLite `user_version`を調べ、DB不存在またはcanonical v1なら従来repository、version 2なら既存`StrictWorkflowDefinitionStore`のread-only list/schema verifierが通った場合だけstrict repositoryを選びます。version unknown、破損、symlink/unsafe pathはunavailable repositoryへ収束し、Bridge起動を止めず、v1へfallbackしません。selectionでDB作成、migration、修復は行いません。MCP runtime lookupは`BridgeRuntime`と同じrepository fieldを読みます。

I6-Aの`WorkflowService.start_saved_workflow` guardは維持し、strict revisionはHTTP/MCPのどちらからもlegacy startへ流れません。strict run admission、run DTO/status/result metadata、request reservation、worker/DBOS/job、output success判定はこのsliceで追加していません。

## 検証

| 検査 | 結果 |
| --- | --- |
| I6-B MCP/repository専用suite | 5 passed / 1 skipped |
| I6-B、I6-A、I3-A/B/C、definition MCP/saved-run/HTTP、legacy definitions、server/router focused aggregate | 242 passed / 3 skipped / 0 failed |
| Ruff check / format check（変更Python paths） | PASS |
| mypy `src/codex_bridge` | PASS — 154 source files |
| `python -m compileall -q src/codex_bridge` | PASS |
| `git diff --check` | PASS |
| Full pytest / CPP/account-auth / live API / real Worker/DBOS/job / WAL metadata | NOT_RUN |

専用suiteのskipはこのWindows hostでfile symlink作成権限がなく、selectorのsymlink testを実行できなかったためです。focused aggregateには既存state/database symlink testsの同理由による2 skipも含みます。selectorは新規v2 DBと、隔離v1 fixtureを明示migrationした後のlegacy historyを読み分けます。未知version・破損v2・read-only byte不変は隔離`tmp_path` DBで確認しました。production/shared DB、live Bridge/worker、実Codex jobは使用していません。

## ChatGPT review

ChatGPT GitHub review of `8626667daaf78985d164d3fd5d1efd8a363ac9b5` はI6-BをACCEPTED (with documented caveats)としました。review時の集計は242 passed / 3 skipped、I6-B専用suiteは5 passed / 1 skippedです。skipはWindows symlink privilege制約です。full pytest、CPP/account-auth回帰、live API、実Worker/DBOS/job、WAL metadataは未検証です。

I6-Cではrun-start/status/result metadata、request-id lookupをallocation前に行うreplay境界、`reservation.replayed`単独でfresh reservationを判定しないこと、`_snapshot_for_row`のforced_stateとinvalid strict/replayを確認します。I7/I8までstrict runtime/admissionは接続しません。

## 変更path

- `src/codex_bridge/server.py`
- `src/codex_bridge/workflows/definition_mcp.py`
- `src/codex_bridge/workflows/definition_repository_selection.py`
- `tests/test_workflow_d1_i6b_mcp_repository_selection.py`
- `docs/workflow-design/phases/D1-I6A-definition-repository-http-evidence.md`
- `docs/workflow-design/phases/D1-implementation-plan.md`
- `docs/workflow-design/phases/D1-I6B-mcp-runtime-repository-evidence.md`
