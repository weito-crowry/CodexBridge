# D1-I6A definition repository and HTTP evidence

- **実装状態:** IMPLEMENTED / ACCEPTED (with documented caveats)。
- **対象branch:** `codex/workflow-d1-i5-b4-verified-readers`。
- **base:** I5-B4C / I5 accepted at review commit `85f1fd206aea0db77d6ccd1e1d78f1551d6d89ec`。
- **範囲:** 明示準備済みdefinitions.sqlite v2のdefinition repository adapter、HTTP definition contract面、strict saved-runのlegacy runtime拒否。
- **全体境界:** I6はIN_PROGRESS。I6-B/C、I7、I8は未実施。D1はDRAFT、I2はDEFERRED / NON-BLOCKING、strict run admissionはOFF。

## 実装

`StrictWorkflowDefinitionRepository` は既存 `StrictWorkflowDefinitionStore` のcreate/update/getを使い、既存repository互換DTOへ変換します。構築だけではdefinitions.sqliteを作成・移行せず、明示的に初期化または移行されたv2 DBだけを読み書きします。通常のv1 repositoryと既定runtime wiringは変更していません。v1 repositoryへのstrict writeはHTTPで `workflow_contract_unavailable`、contract validationはDB書込みなしで処理します。

v2 storeにread-only list/cursorとrevision ETag逆引きを追加しました。listは既存v1と同じorder/cursor/metadata規則を使い、1つの検証済みread connectionと既存revision verifierでページ内のimmutable revision/contractを検証します。summaryにcontract bodyを含めません。ETag逆引きはmutation ledgerからrevisionを特定し、そのimmutable revisionを既存検証経路で再読込してv1/v2 ETagと照合します。duplicate mutation ledger rowsはrevision検証前に一意化します。digest、CAS、ETag計算は再実装していません。

共通 `definition_contract` helperとtyped errorsはHTTP、および後続MCP adapterが利用できるpure parse/serialization境界です。field omissionはsentinelで区別し、explicit nullはcreate/validateでinvalid、strict updateでrequired、legacy updateでinvalidとなります。contract objectは既存I1 `compile_contract` で検証し、値を含まない `workflow_contract_invalid` / `workflow_contract_required` / `workflow_contract_unavailable` に写像します。

HTTPの既存Create/Validate/Get/List/Update/Graph routeにだけoptional contractを追加しました。Strict summary/graphではcontract version/hash/binding digest、detailでは加えてcanonical contract object、create/updateでは既存metadataとstrict binding metadataを返し、ETag header/bodyを一致させます。Legacy serializerの既定値とMCP route/tool/wiringは変更せず、v1 responseへstrict null fieldsは追加しません。If-Matchはstrong v1/v2のみを受理し、欠落428、不正形式400、stale/wrong mode 412を維持します。

`WorkflowService.start_saved_workflow` はrevision取得直後、legacy source digest / request normalization / reservation / worker call前にstrict marker/contractを判定し、現行のservice unavailable経路で拒否します。HTTP saved-runと既存MCP saved-runが共通service methodを使うため、strict revisionをlegacy runtimeへ流しません。I6-Aでstrict run admissionは追加していません。

## テスト

Windows nativeのpytest `tmp_path` DBと既存fake fixtureを使用します。新規suiteは明示新規v2 DBとI3-Aからのmigrated v1 historyを対象にし、verified list/pagination、v1/v2 ETag逆引き、optional contract validate/create/detail/list/graph、legacy rowでstrict field omission、null/invalid contractのredactionとno-write、strict update required/null/stale/wrong-mode/replay/conflict、legacy-to-strict transitionとstrict-to-legacy拒否、strict saved-run拒否前の副作用なしを確認します。

| 検査 | 結果 |
| --- | --- |
| I6-A dedicated HTTP/repository/service suite | 9 passed |
| I3-A/I3-B storage、legacy definitions HTTP、saved-run/service、MCP definition、I6-A aggregate | 168 passed / 2 skipped |
| Ruff check / format check（変更Python paths） | PASS |
| mypy `src/codex_bridge` | PASS — 153 source files |
| `python -m compileall -q src/codex_bridge` | PASS |
| `git diff --check` | PASS |
| Full pytest / live API / DBOS / real Worker/job | NOT_RUN — 後続gate |
| CPP/account-auth regression | NOT_RUN |

共有/production DB、実Codex外部job、live Bridge/workerは使用していません。CPP/account-auth回帰はこのsliceで未実施です。

## 変更path

- `src/codex_bridge/workflows/definition_contract.py`
- `src/codex_bridge/workflows/strict_definition_repository.py`
- `src/codex_bridge/workflows/definitions.py`
- `src/codex_bridge/workflows/strict_definitions.py`
- `src/codex_bridge/workflows/definition_api.py`
- `src/codex_bridge/workflows/service.py`
- `tests/test_workflow_d1_i6a_definition_repository_http.py`
- `docs/workflow-design/phases/D1-I5B4C-reserved-reader-evidence.md`
- `docs/workflow-design/phases/D1-implementation-plan.md`
- `docs/workflow-design/phases/D1-I6A-definition-repository-http-evidence.md`

## 次sliceと留保

- **I6-B (後続slice):** MCP definition optional contractをshared parser/serializerへ接続し、明示準備済みv2 repositoryのみを選択するruntime wiringを追加する計画でした。実装結果は[D1-I6B証跡](D1-I6B-mcp-runtime-repository-evidence.md)に記録し、現在I6-BはIMPLEMENTED / PENDING_CHATGPT_REVIEWです。自動migrationは行っていません。
- **I6-C:** strict run-start DTO/status/result metadataとreplayを実装します。request-id lookupは新規run allocation前に行います。I5-B4C coordinatorのreservationはrun_idでreplay照合するため、`reservation.replayed`単独でfresh reservationを推定しません。I5-B4Bで記録した`_snapshot_for_row` forced_stateとinvalid strict/replayの相互作用も接続時に確認します。
- strict runtime、worker/DBOS/job、output成功判定、公開strict run admissionはI6-Aの対象外です。I7/I8とChatGPT reviewを経るまでstrict run admissionはOFFです。
- I6-AはChatGPT review受入済みですが、I6全体とD1全体の承認を意味しません。D1はDRAFTのままです。

## ChatGPT GitHub review acceptance

ChatGPT reviewed commit `36ab6a8ece2a1ffd6fea03065bd901f4d811fe40` and ACCEPTED I6-A with documented caveats. The dedicated I6-A suite recorded 9 passed; the focused aggregate recorded 168 passed / 2 skipped. Full pytest, CPP/account-auth regression, live API, real Worker/DBOS/job, and WAL metadata were not verified. This acceptance covers only the I6-A definition repository and HTTP contract surface; I6-B/C, strict runtime, and D1 overall remain pending.
