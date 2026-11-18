# ADR 0006: 公開HTTP/MCP契約・ETag v2・エラー互換

- **Status:** Proposed（レビュー待ち）
- **対象:** Workflow D1詳細設計
- **関連:** [D1詳細設計](../phases/D1-input-output-variables.md)、[アーキテクチャ](../architecture.md)、[ADR 0002: contract保存と正規化](0002-contract-storage-and-normalization.md)、[ADR 0003: run-local変数DBとmanifest v2](0003-run-local-variable-store-and-manifest-v2.md)、[ADR 0005: 起動排他・resource cap・Windows ACL](0005-startup-exclusion-resource-limits-windows-acl.md)

このADRは既存HTTP routeとMCP toolを使ったstrict contractの公開形、厳格ETag v2、実行状態・エラー・結果の互換規則を提案します。D1はDRAFTのままで、API/MCP、DB、UI、Workflow実装はありません。旧clientの入力・応答・ETag・request id再送規則を変更しません。

## Context

現行HTTP APIにはdefinition list/create、validate、detail/update、revision detail、graph、saved-run startとrun status/result等があります。HTTP updateはIf-Matchを必須とし、v1 strong ETagとrevision compare-and-swapを使います。MCPにはdefinition list/get/graph/validate/create/update、workflow_run_start、run/task status・event取得toolがあります。MCP updateはexpected_revisionによるCASを使い、HTTPのIf-Matchとは異なるwire契約です。

現行HTTP/MCPは許可されたrequest field setを検査し、未知fieldを拒否します。両transportの既存error code/string、成功response形、permission boundary、response上限は同一ではありません。したがってstrict用の差分だけを名前付きで追加し、legacy request/responseに新しいnull fieldやversion labelを足しません。

| 境界 | 現行契約として保つもの |
| --- | --- |
| HTTP definition | GET/POST /ui-api/control/workflow-definitions、POST validate、GET/PUT /{definition_id}、GET /{definition_id}/revisions/{revision}、GET graph |
| HTTP saved run | POST /{definition_id}/runs。client_request_id、revision、workspace_root、optional inputを受ける |
| HTTP update concurrency | If-Match必須。欠落428 precondition_required、malformed 400、stale 412 precondition_failed。既存v1 ETagは不変 |
| MCP definition | workflow_definitions_list、workflow_definition_get、workflow_graph_get、workflow_definition_validate、workflow_definition_create、workflow_definition_update |
| MCP run/read | workflow_run_start、既存run/status/result/task/event read tools |
| MCP permission | list/get/graphとrun/task/event readはread、validate/create/updateはwrite、workflow_run_startはexecute |
| size/status | HTTP request body 128 KiB、definition response 1 MiB。MCP definition/task/event response 256 KiB（JSON ensure_ascii=True）。既存run statusとHTTP/MCP error契約を維持 |

### HTTP route / MCP tool対応案

| HTTP path/method | MCP tool | Permission | Strictで加える内容 / legacy互換 |
| --- | --- | --- | --- |
| GET /ui-api/control/workflow-definitions | workflow_definitions_list | HTTP既存認可 / MCP read | Strict summaryにcontract_version/hash/binding_digest。legacy key setとpagination/cursor維持 |
| POST /ui-api/control/workflow-definitions | workflow_definition_create | HTTP既存認可 / MCP write | Optional contract。未指定legacy body/response維持 |
| POST /ui-api/control/workflow-definitions/validate | workflow_definition_validate | HTTP既存認可 / MCP write | Optional contract。strict validation resultだけcontract version/hashを追加 |
| GET /ui-api/control/workflow-definitions/{definition_id}、GET /ui-api/control/workflow-definitions/{definition_id}/revisions/{revision} | workflow_definition_get | HTTP既存認可 / MCP read | Strict detail/revisionのみcontract metadata/body。legacy ETagとbodyは不変 |
| PUT /ui-api/control/workflow-definitions/{definition_id} | workflow_definition_update | HTTP既存認可 / MCP write | HTTP If-Match CAS、MCP expected_revision CAS。strict更新だけcontract必須 |
| GET /ui-api/control/workflow-definitions/{definition_id}/graph | workflow_graph_get | HTTP既存認可 / MCP read | Strict graph metadataにcontract hash/bindingを付加。graph node自動拡張なし |
| POST /ui-api/control/workflow-definitions/{definition_id}/runs | workflow_run_start | HTTP既存認可 / MCP execute | Clientにcontract/run_format_version選択を許さず、pinned revisionからserver決定 |
| 既存run status/detail/result/task/event routes | 既存run/task read tools | HTTP既存認可 / MCP read | Strict runに許可済みbinding/reason metadataのみ。legacy projectionとpermissionを維持 |

## Decision

### 1. 既存route/toolへoptional contractを追加

Create、validate、updateの既存JSON objectにoptionalなトップレベルcontractを追加します。新しいversioned route/tool群は作りません。contractはJSON objectであり、JSON null、unknown top-level key、未対応contract_versionは拒否します。契約objectのキーはcontract_version、input_schema、output_schema、variablesの4つです。

~~~json
{
  "contract_version": 1,
  "input_schema": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {},
    "additionalProperties": false
  },
  "output_schema": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {},
    "additionalProperties": false
  },
  "variables": [
    {
      "declaration_id": "review_decision",
      "name": "decision",
      "owner_scope": "workflow",
      "value_schema": {
        "type": "string",
        "enum": ["ACCEPTED", "REJECTED"]
      },
      "sensitivity": "public"
    }
  ]
}
~~~

各variable declarationはdeclaration_id、name、owner_scope、value_schema、sensitivityを持ち、initialはoptionalです。D1 strict v1はsensitivity=publicだけを許し、secretはcontract保存時または値の受入時に拒否します。Schema dialect/keyword、objectのadditionalProperties、参照、default、正規化規則はD1 PD-01..PD-10に従います。contractはrequest body上限の一部として数え、保存時はcanonical JSONとhashを使います。

Legacy requestはcontractキーがないことをlegacy modeの明示的意味として扱います。以下はcontract fieldの差分だけを示すJSON fragmentです。実requestには各endpointの既存必須fieldを従来どおり含めます。

~~~json
{}
~~~

Strict create/validate/updateでは同じrequest objectにcontract objectを加えます。下記fragment以外の既存fieldとvalidationは変わりません。

~~~json
{
  "contract": {
    "contract_version": 1,
    "input_schema": {
      "$schema": "https://json-schema.org/draft/2020-12/schema",
      "type": "object",
      "properties": {},
      "additionalProperties": false
    },
    "output_schema": {
      "$schema": "https://json-schema.org/draft/2020-12/schema",
      "type": "object",
      "properties": {},
      "additionalProperties": false
    },
    "variables": []
  }
}
~~~

| Mutation | contract absent | contract object | contract null |
| --- | --- | --- | --- |
| Legacy create | 従来どおりlegacy definitionを作る | strict v1をopt-inして新revisionを作る | HTTP/MCP 400。legacy解除表現として扱わない |
| Legacy revision update | 従来どおりlegacy更新 | strictへの明示的移行として新revisionを作る | HTTP/MCP 400 |
| Strict revision update | HTTP/MCP 400 workflow_contract_required。strictからlegacyへのdowngradeを許さない | contractを再検証し必ず新immutable revisionを作る。同一contractでも上書きしない | HTTP/MCP 400 workflow_contract_required |

Contract変更、定義内容変更、ETag/CAS更新は既存revision追加とmutation ledgerの原子性に従います。mutation digestにはcontractのpresence、canonical value/hash、revisionと期待ETagまたはexpected_revisionを含めます。既存legacy request idとmutation replayのdigest/retry規則を再解釈しません。HTTPとMCPは共通のstrict意味を持ちますが、それぞれの既存transport envelopeを保ちます。

### 2. Validationと定義read response

既存HTTP POST validateとMCP workflow_definition_validateにoptional contractを追加します。contract省略時はlegacy response key setを完全に維持します。strict validation成功responseは次の追加情報を含みます。

~~~json
{
  "valid": true,
  "source_sha256": "<64 lowercase hex>",
  "canonical_sha256": "<64 lowercase hex>",
  "max_live_codex_jobs": 1,
  "contract_version": 1,
  "contract_sha256": "<64 lowercase hex>"
}
~~~

validate時点ではdefinition_id/revisionが確定していないためbinding_digestやpreviewを返しません。Schema/contract invalid、secret指定、JSON objectでないcontractは安全なvalidation errorとし、値・prompt・tracebackを公開しません。検証失敗時はdefinition revisionやrequest ledgerを書き込みません。

Legacy GET/detail/list/graphは現在のbody/headerを維持し、strict専用keyを省略します（nullを追加しません）。Strict GET detail/revision responseは既存metadataとsource_yamlに次を加えます。ETag bodyとHTTP ETag headerは同じ強いvalidator値にします。

Legacy summary/detailのETag表示は従来のbody fieldとHTTP headerのstrong v1値のままであり、次はvalidator fieldだけを示す互換fragmentです。ほかの既存metadata/source fieldはそのままです。

~~~json
{
  "etag": "\"workflow-definition-v1-<64 lowercase hex>\""
}
~~~

~~~json
{
  "definition_id": "<definition id>",
  "revision": 3,
  "etag": "\"workflow-definition-v2-<64 lowercase hex>\"",
  "contract_version": 1,
  "contract_sha256": "<64 lowercase hex>",
  "binding_digest": "<64 lowercase hex>",
  "contract": {
    "contract_version": 1,
    "input_schema": {
      "$schema": "https://json-schema.org/draft/2020-12/schema",
      "type": "object",
      "properties": {},
      "additionalProperties": false
    },
    "output_schema": {
      "$schema": "https://json-schema.org/draft/2020-12/schema",
      "type": "object",
      "properties": {},
      "additionalProperties": false
    },
    "variables": []
  }
}
~~~

上記はstrict専用fieldを示すresponse fragmentであり、既存detail fieldsは保持します。Strict list summaryとgraph metadataにはcontract_version、contract_sha256、binding_digestだけを追加し、contract本体や大きなSchemaは含めません。graph node自動拡張・編集はD8の対象です。responseは既存HTTP 1 MiB / MCP 256 KiB cap以内で作り、保存bindingとcontract hashを照合します。超過するfull detailを無制限に返したり、legacy capを変更したりしません。

### 3. ETag v1/v2とCAS境界

Legacy revisionのETagは現在の強い形式workflow-definition-v1-<64 lowercase hex>と既存digest計算を維持します。Strict revisionだけ、次のdomain-separated canonical JSONから強いworkflow-definition-v2 ETagを作ります。

~~~text
etag_payload = {
  "domain": "codexbridge-workflow-definition-etag-v2",
  "definition_id": definition_id,
  "revision": revision,
  "source_sha256": source_sha256,
  "canonical_sha256": canonical_sha256,
  "max_live_codex_jobs": max_live_codex_jobs,
  "contract_version": 1,
  "contract_sha256": contract_sha256,
  "binding_digest": binding_digest
}
etag_hash = lowercase_hex(SHA256(canonical_json(etag_payload)))
etag = quote("workflow-definition-v2-" + etag_hash)
~~~

canonical_jsonはD1で定めた決定的UTF-8 JSON（sort_keys、NaN/Infinity禁止）です。v2 ETagはETag header/bodyおよびdefinition mutation ledgerに同じidentityで記録します。ETag v2のv2はdefinition contract bindingの版で、run_format_version=2とは独立です。

HTTP If-Match parserは既存v1とstrict v2のstrong ETagを受け付けます。serverはimmutable revisionのmode、contract row/hash、binding、current latest revisionを解決してCASします。Strict revisionにlegacy v1 ETag、legacy revisionにstrict v2 ETag、stale revision/hash/bindingを提示した場合は412 precondition_failedです。If-Match欠落は既存428、malformed valueは既存400です。新しいwire statusやweak ETagは追加しません。

MCP workflow_definition_updateは現在のexpected_revisionをauthoritative CASとして維持し、必須If-Matchを新設しません。Strict updateはexpected_revisionに加えてcontractを要求し、staleなら既存MCP workflow_definition_precondition_failedを返します。ETag値はGET responseに表示しますが、MCP mutationをHTTP headerへ依存させません。

### 4. Saved-run startとstrict v2 binding

Clientはstrict/legacyやrun_format_versionを選べません。Serverが指定revisionを解決し、workflow_definition_contracts rowがあるときだけstrict modeにします。contract fieldはrun-start requestに受け付けず、callerによるcontract差し替えを許しません。

~~~json
{
  "client_request_id": "<client-generated id>",
  "revision": 3,
  "workspace_root": "<workspace path>",
  "input": {}
}
~~~

上記はHTTP POST /{definition_id}/runs bodyです。input省略時は現行どおり空objectを基点にします。MCP workflow_run_startは同じ意味のdefinition_id、revision、client_request_id、workspace_root、optional input引数を使います。Strict inputはJSON objectであり、input:nullはinvalidです。input省略時の空objectへSchema defaultsを一度適用し、normalized inputで検証・hash計算・input.json保存を行います。default適用前に明示的に同じdefault valueを渡したrequestと省略requestは、他のbindingが同じなら同じnormalized input digestとなり、同じclient_request_idの同一requestとして扱えます。異なるnormalized inputは既存のrequest_id_conflict規則で拒否します。無効inputはreservationを作りません。

MCP tool argumentsの例は同じserver-side mode選択を使います。

~~~json
{
  "definition_id": "<definition id>",
  "revision": 3,
  "client_request_id": "<client-generated id>",
  "workspace_root": "<workspace path>",
  "input": {}
}
~~~

Legacy MCP start infoは現在の固定field setのままです。Strict-only fieldsはlegacy replyに出しません。

~~~json
{
  "workflow_id": "<workflow id>",
  "run_id": "<run id>",
  "status": "queued",
  "definition_id": "<definition id>",
  "revision": 3
}
~~~

Strict start成功responseは既存固定fieldsを保ち、strict runに限り以下を追加します。Legacy responseには追加しません。

~~~json
{
  "workflow_id": "<workflow id>",
  "run_id": "<run id>",
  "status": "queued",
  "definition_id": "<definition id>",
  "revision": 3,
  "run_format_version": 2,
  "contract_version": 1,
  "contract_sha256": "<64 lowercase hex>",
  "binding_digest": "<64 lowercase hex>"
}
~~~

Strict request digestはD1のversioned canonical digest規則に従い、normalized input SHAと固定revision/contract bindingを照合します。過去legacy digestを再計算しません。不確実な送信・commitを自動再送しません。reservation後のbinding不整合はrun_idを保持しneeds_reconciliationにします。

### 5. Status、result、reason_code

既存primary statusはqueued、starting、running、succeeded、failed、failed_to_start、needs_reconciliation、unknownのままです。新primary statusは作りません。Strict status/detail/event projectionだけに、許可済みで安全なreason_codeとcontract/run binding metadataを追加できます。Legacy body/statusを拡張しません。

Output Schemaはrunをsucceededとして確定しresultを公開する前に検証します。Validation成功の場合だけ現在のJSON result形で値を返します。Output違反は既知runtime failureとしてstatus=failed、reason_code=output_contract_violationにし、未検証resultを親・API・MCPへ返しません。業務上のACCEPTED/REJECTEDはSchema valid output内の値であり、runtime succeeded/failedとは独立です。

~~~json
{
  "workflow_id": "<workflow id>",
  "run_id": "<run id>",
  "status": "succeeded",
  "result": {
    "decision": "REJECTED"
  },
  "run_format_version": 2,
  "contract_version": 1
}
~~~

~~~json
{
  "workflow_id": "<workflow id>",
  "run_id": "<run id>",
  "status": "failed",
  "reason_code": "output_contract_violation"
}
~~~

外部副作用が送信されたか、SQLite mutationがcommitされたか、ledger/checkpointを一致させられるかが不明なら成功/通常失敗に推定せず、status=needs_reconciliation、reason_code=strict_execution_uncertainまたは限定されたspecific reasonを返します。

~~~json
{
  "workflow_id": "<workflow id>",
  "run_id": "<run id>",
  "status": "needs_reconciliation",
  "reason_code": "strict_execution_uncertain"
}
~~~

Status/result APIは既存動作のqueued/running=202、succeeded result=200、failed/reconciliationなど=409を維持します。unknownはunknownのまま扱い、自動再実行・Codex再POSTに使いません。reason_codeは値を含まないstable machine-readable codeであり、prompt、input document、variable value、secret、tracebackを含めません。公開run/task eventでも同じredaction境界を守ります。

### 6. HTTP/MCP error mapping

既存legacy error code/stringはtransportごとに従来どおり維持し、HTTPとMCPの異なる既存codeを相互にrenameしません。以下のstrict additionsは同じmachine codeを両transportで使います。ただしresponse-sizeは既存MCP codeを維持する例外とします。

| Strict condition | HTTP response | MCP error | Run state / run_id | Retry・result扱い |
| --- | --- | --- | --- | --- |
| contract/schema invalid | 400 workflow_contract_invalid | workflow_contract_invalid | runなし | 修正後に新requestとして再送可能。保存revisionなし |
| strict update contract欠落/null | 400 workflow_contract_required | workflow_contract_required | runなし、revision不変 | strict-to-legacy downgradeせず、明示contract付き更新を求める |
| strict input invalid、input null、secret input | 400 workflow_input_invalid / workflow_secret_not_supported | 同名code | reservation前でrunなし | 値をerrorに出さず、入力修正後に再要求 |
| admission request/body/normalized input cap超過 | 413 workflow_resource_limit。legacy既存payload_too_large mappingは変更しない | workflow_resource_limit | reservation前でrunなし | capを超えた入力を開始しない |
| mutation後にruntime quota超過 | run/result取得時409 workflow_resource_limit | workflow_resource_limit | failed、run_idあり、reason_code=resource_limit_exceeded | acceptance/succeededにせず、値・ledgerを保持 |
| definition contract store unavailable | 503 workflow_contract_unavailable | workflow_contract_unavailable | runなし | 障害解消まで再要求可能 |
| startup/security boundary不成立 | 503 workflow_security_boundary_unavailable | workflow_security_boundary_unavailable | strict reservationなし | strict startを止める |
| binding conflict前のreservation | 409 workflow_binding_conflict | workflow_binding_conflict | runなし | binding不一致を直さず再実行しない |
| reserved runのbinding/state不整合 | 409 workflow_needs_reconciliation + safe reason_code | workflow_needs_reconciliation | needs_reconciliation、run_idあり | status/result照合。副作用を自動再送しない |
| validated outputを作れない | result GET 409 workflow_output_invalid | workflow_output_invalid | failed、reason_code=output_contract_violation | result値なし。通常再実行で上書きしない |
| 実行/commitの確定不能 | result GET 409 workflow_execution_uncertain | workflow_execution_uncertain | needs_reconciliation、reason_code=strict_execution_uncertain | ledger/worker照合。自動retry/rePOSTしない |
| strict projectionがresponse cap超過 | 413 workflow_response_too_large | 既存response_too_large | run状態は不変 | partial responseを返さない。legacy codeは変更しない |

既存HTTPの404 missing definition、403 authorization、412/428 precondition、409 request_id_conflict/workflow_capacity_reached/workflow_needs_reconciliation、503 service failureと既存MCPのprefixed error namesは継続します。strict input invalid等、reservation前の400/413はrunを作らない境界です。reservation後に外部効果やstateが不確実なら、通常のvalidation errorへ変換せず既存workflow_needs_reconciliationとsafe reason_codeを使います。

MCPの新規errorはHTTP statusを埋め込まず、既存structured error envelopeに次のallowlist fieldsだけを含めます。

~~~json
{
  "error": "workflow_execution_uncertain",
  "reason_code": "strict_execution_uncertain",
  "workflow_id": "<workflow id>",
  "run_id": "<run id>",
  "status": "needs_reconciliation"
}
~~~

### 7. Public projection、permissions、resource cap

D1はvariable valueを返す公開inspector endpoint/toolを追加しません。Strict run/task projectionに必要な場合はframe count、mutation count、declaration count、latest versionなど明示許可したbounded metadataに限り、value_json、input、prompt、secret、全mutation historyは返しません。Read-only inspectionはvariables.sqliteを欠落時に作成・初期化しません。D8がGUI editor/inspector、pagination、詳細authを設計するときもこの境界を前提にします。

MCPの既存read/write/execute permission分類を維持します。contractのlist/get/graphはreadのまま、validate/create/updateはwrite、workflow_run_startはexecuteです。Strict contractの存在を理由にread permissionを引き上げたり、write clientへread権限を追加したりしません。

適用するcapはHTTP全request 128 KiB、YAML 64 KiB、HTTP definition response 1 MiB、MCP response 256 KiB、strict contract 64 KiB、normalized input 96 KiB、output 64 KiB等、既存capとADR 0005/D1 resource tableを保ちます。contractはcombined HTTP request body内で計上します。Strict projection追加後も合計response capを検査し、個別fieldのcap通過だけで成功扱いしません。Legacy bodyのoversize error code/response形をstrict向けに一括変更しません。

## Failure matrixと互換ゲート

| 状況 | 安全な公開結果 |
| --- | --- |
| strict contractなしのlegacy revision | legacy behavior、v1 ETag、既存response/error shape |
| strict contract/schema不正 | 400、保存・revision追加なし、値をredact |
| stale/cross-mode If-Matchまたはexpected_revision | 既存precondition error、mutationなし |
| strict run input invalid | reservationなし。MCP/HTTPで安全なinput error |
| run開始後にbinding/variable ledgerを照合できない | needs_reconciliation、run_id保持、success/resultを返さない |
| output Schema違反 | failed/output_contract_violation、未検証result非公開 |
| business decisionがREJECTEDでoutput Schema valid | status=succeeded、検証済みJSON result内にREJECTED |
| status=unknownまたは不確実な外部効果 | unknown/reconciliationを維持し、自動再送しない |
| strict detail/metadataがresponse cap超過 | 全文・partialを返さずstrict response-too-large mapping |

Strict enable前にHTTP/MCP status、definition、run-start、worker/recovery、task/event、result、manager projectionが同じdefinition mode/contract bindingを確認できることを受入対象とします。Legacy run、旧ETag、既存mutation/request id ledgerとclient再送はv1 identityのまま回帰確認します。

## 受入テスト案

| Case | 受入条件 |
| --- | --- |
| Optional contract compatibility | 既存HTTP/MCP route/toolでcontract fieldだけを追加でき、未指定legacy requestのexact key setを保つ |
| Contract wire validation | contract_version=1、必須key/Schema dialect/variable fieldsを検査し、null、unknown key、secret、bad Schemaを拒否 |
| Legacy create/update | legacy response、revision、v1 ETag、HTTP header/bodyの値とMCP形をbaselineと一致させる |
| Legacy list/graph/revision GET | summary/detail/graphのfield、pagination/cursor、ETagを保持し、strict keyをnullで追加しない |
| Strict validate | valid/source/canonical/max-jobs/contract hashを返し、binding_digestを返さない。失敗はwriteなし |
| Legacy-to-strict update | contractを明示した新immutable revisionを作りv2 ETagを発行 |
| Strict update omission/null | 両transportがworkflow_contract_required、downgradeやrevision writeなし |
| Same strict contract update | 上書きせず新revisionを作成し、mutation digest/replay identityが異なるrevisionを識別 |
| V2 ETag calculation | canonical fields/domain/UTF-8/hash/quotingが期待値一致し、contract/revision/YAML identityの変化を検知 |
| HTTP ETag CAS | v1/v2 valid/stale/cross-mode、missing 428、malformed 400の旧HTTP mappingを検査 |
| MCP update CAS | expected_revisionを維持しstale MCP precondition codeを返し、If-Matchを要求しない |
| ETag publication | GET header/bodyのETag一致、strict v2 strong validator、legacy v1 replay不変 |
| Strict GET/detail/list/graph | detailのみcontract bodyを含み、summary/graphはbounded hash metadataのみ |
| Request mutation idempotency | contract presence/canonical bytes/revision/expected validatorをdigestへ結び、同一strict mutation replayは再適用しない |
| Legacy idempotency | 旧v1 mutation/request IDとarchived ETagを新digestへ再解釈しない |
| Strict start mode selection | pinned revision rowからserverがlegacy/strictを選び、caller contract/run_format_versionを拒否 |
| Input missing/null/default | input省略とnullを区別し、default適用後のsame normalized valueが同じrequest identityになる |
| Invalid input reservation | schema/size/secret invalid inputはrequest/run reservation前に止まる |
| Different normalized request | 同じclient_request_idでもnormalized value/bindingが違えばrequest_id_conflict |
| Strict start response | strict metadataを追加しlegacy start response形とstatusを変えない |
| Output violation vs business REJECTED | invalid outputはfailed/no result、valid REJECTEDはsucceeded/result内のbusiness value |
| Uncertain execution | needs_reconciliation/reason_codeを返し、自動Workflow retry/Codex rePOSTなし |
| Status/result legacy regression | legacy queued/running/succeeded/failed/reconciliation body/statusをbaseline一致 |
| Secret/redaction | public error/status/event/graphにinput、prompt、variable value、token、tracebackがない |
| MCP permissions | read/write/execute分離を維持し、strict supportのため権限昇格しない |
| Error mapping | HTTP status/body、MCP error envelope、run status、reason_code、run_id有無とretry可否を対応表どおり検査 |
| Response bounds | HTTP definition 1 MiB/request 128 KiB、MCP 256 KiB、contract/input/output capsの境界とoversize mapping |
| D8 compatibility | read-only projectionはbounded metadataのみで、D8 editor/inspector用にvariable value公開を約束しない |

## Alternatives

| Alternative | 判断 |
| --- | --- |
| 全strict operation用に新しいv2 HTTP route/MCP toolを作る | 採用しません。既存permission/client surfaceを二重化せず、optional contractとstrict-only response fieldsで段階拡張します |
| legacy updateでcontract省略/nullをstrict解除として解釈 | 採用しません。transport差やold client retryでstrict definitionを意図せずdowngradeするため拒否します |
| 全legacy responseへcontract=null、format version、reason_codeを追加 | 採用しません。厳密なlegacy field-set/ETag/body compatibilityを壊します |
| HTTP ETagとMCP expected_revisionを一つに統一 | 採用しません。HTTP header preconditionとMCPの既存引数CASのwire contractを保持します |
| OUTPUT/業務REJECTED/UNKNOWNを同じ失敗statusにまとめる | 採用しません。検証済み業務結果、既知runtime failure、不確実な状態を分けて再送誤りを防ぎます |

## Open Questions

1. HTTP/MCPのshared DTO validator、既存endpoint内のoptional field許可リスト、strict-only response fieldsのserializer配置とgolden compatibility fixture。
2. contract/metadata増加時のlist/detail paging、summary field order/canonical rendering、response cap直前の選択的省略・error rendering手順。legacy responseのfield omissionは変更しない。
3. 各内部失敗をstable reason_codeとevent fieldへ写像する全reader/worker/managerの実装位置・更新順。公開status値そのものは既存enumを維持する。
4. Contract presence/canonical hash/expected validatorを既存mutation request ledgerへ組み込む具体的payload versionと、旧mutation ledgerを再計算しない実装fixture。
5. strict v2利用後のbackup retention/offline restore gateはADR 0003/0004/0005の残論点、外部副作用後の補償・人手レビューはD1 Open Question 14に残る。これらはこの公開契約案で自動復旧や再送可能としない。

数値・wire serializer・reason/eventの細部はレビュー対象です。本ADRはProposedであり、実装済みHTTP/MCP契約を示しません。
