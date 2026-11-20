# ADR 0007: OWS call adapterによるstrict変数操作

- **Status:** Proposed（レビュー待ち）
- **対象:** Workflow D1詳細設計
- **関連:** [D1詳細設計](../phases/D1-input-output-variables.md)、[D1実装スライス計画](../phases/D1-implementation-plan.md)、[ADR 0002: contract保存と正規化](0002-contract-storage-and-normalization.md)、[ADR 0003: run-local変数DBとmanifest v2](0003-run-local-variable-store-and-manifest-v2.md)、[ADR 0006: 公開HTTP/MCP契約と互換](0006-public-api-mcp-etag-error-contract.md)

このADRは、OWS v1.0.3のCallFunctionを使ってCodexBridge strict Workflowの型付き変数を操作する候補を記録します。D1/本ADRはレビュー前のDRAFT/Proposedで、call adapter、jq projection、変数store連携は未実装です。

## Contextと現行境界

公式配布Schemaのworkflow documentはdocumentとdoを要求し、CallFunctionはreserved protocol namesを除くfunction名の文字列callと任意のobject withを表現できます。一方、現行interpreter.pyの_validate_action(operation='call')はmock-jobとcodex-jobだけを許し、ows setは既存dataflow値を生成するだけです。ows set/contextを永続型付き変数storeの代替として使いません。

したがって、Schema上有効な任意function名をそのまま実行可能にせず、CodexBridgeが明示allowlistしたstrict-only target一つを加えます。公式OWS YAML Schema、配布ファイル、OWS task typeを変更しません。既存legacy revisionのcall validation/動作、mock-job/codex-job、set、context、output/exportは維持します。

## Decision（Proposed）

### 1. strict-only allowlisted function

Strict contract revisionに限ってOWS CallFunction target codexbridge-variableを追加します。これはOWS標準に新しいtask typeを足す変更ではなく、既存call function nameのCodexBridge所有allowlist entryです。

- strict runner/validator modeは、保存済みimmutable definition revisionにcontract rowがあり、そのcontract/bindingが検証済みの場合だけこのfunctionを許可します。
- legacy revisionで同じcall名を検出した場合は現行subsetどおりunsupportedとして拒否します。legacyに新しい変数runtimeや$cbvarsを暗黙導入しません。
- mock-jobとcodex-jobは従来の検証、実行、MAYBE_SENT処理を維持します。
- 未知call targetを任意import/function dispatchに渡しません。adapterはrun-local variable stateだけを扱い、任意network access、任意file write、外部MCP invocationを行いません。
- strict callを全relevant validator/worker/recovery/readerが識別するまでstrict executionを有効化しません。versioned strict-only validation pathがlegacy schema/runner modeを横断して緩めないことを確認します。

### 2. call argumentsとscope

ows with objectはoperationごとに次の引数だけを許します。各callはoperation、declaration_id、scopeを必須とし、scopeの値はD1ではworkflowだけです。

| Operation | Required arguments | Optional arguments |
| --- | --- | --- |
| initialize | operation, declaration_id, scope, value | なし |
| assign | operation, declaration_id, scope, value | なし |
| increment | operation, declaration_id, scope | by（省略時1） |
| decrement | operation, declaration_id, scope | by（省略時1） |
| append | operation, declaration_id, scope, value | なし |
| object_set | operation, declaration_id, scope, path, value | なし |
| object_delete | operation, declaration_id, scope, path | なし |
| variable_delete | operation, declaration_id, scope | なし |

operation-specific以外の余分なkey、未知operation、欠落keyは拒否します。declaration_idはstrict contractの静的宣言へ照合します。callerはrun_id、frame_id、任意のscope pathをwithから指定・偽装できません。runtimeは検証済みrun bindingからrun identityとroot WorkflowFrameを解決します。root frameと宣言の対応が不明・不一致ならmutationしません。

increment/decrementのbyはboolを除く有限かつ非負のJSON numberです。省略時は1です。decrementはbyで指定された絶対量を引き、負数byは拒否します。対象変数の既存value_schemaに従い、integer/numberや値域を厳密検証し、booleanをnumberとして扱わずNaN/Infinityは拒否します。

initialize/assign/append/object_setのvalueは必須です。appendはarrayだけを対象にします。object_set/object_deleteのpathはRFC 6901 JSON Pointerです。object_setはD1のobject更新規則に従い、存在する親objectと最終value_schemaを検証します。variable_deleteは値/pathを受け付けず、明示tombstone操作です。すべてのoperationはD1の宣言・型・quota・transaction規則に従い、validation失敗時はstate/ledgerを更新しません。

### 3. value評価とread projection

with.valueは、現行ows setが使うJSON literalと許可済みjq式の評価規則を可能な範囲で再利用します。新しいprompt/template言語や独自式parserを追加しません。値の評価に失敗した場合は、そのmutation stepを永続化前に失敗させます。

Read用の専用OWS taskは設けません。Strict execution内のread-only jq projection候補として、current root frameのpresent宣言値だけを表す$cbvarsを提供します。

~~~json
{
  "workflow": {
    "review_decision": "ACCEPTED"
  }
}
~~~

~~~jq
$cbvars.workflow["review_decision"]
~~~

上記は論理projection例で、raw SQLite row、tombstone、digest、全run値を外へ出しません。未初期化または削除済み宣言をJSON nullへ読み替えず、参照エラーとして区別します。明示scopeとdeclaration_idを解決したroot frame snapshotだけを対象にします。callerから物理frame identityを受け取りません。既存$context、$input、$output、ows context JSON、set/exportの意味を変更せず、変数状態とcontextを暗黙同期しません。$cbvarsはstrict revision内に限定し、legacy runnerへ注入しません。

正式なjq/compiler injection構文、projectionのread snapshot consistency、missing参照を実行エラーに変換する地点は実装gate/Open Questionsに残します。D2の動的prompt templateとは異なるD1のWorkflow data read境界です。

### 4. Schema-validだが現行ではunsupportedな最小例

次のdocument/do/call/with形はOWS v1.0.3公式Schema上のCallFunction形に沿います。call function名codexbridge-variableはreserved protocol namesではありません。しかし現行interpreterのcall allowlistにはなく、現在実行可能なWorkflow例ではありません。strict revision向けadapterを実装し、versioned strict validator/runnerが有効になるまでは実行を許可しません。

~~~yaml
document:
  dsl: "1.0.3"
  namespace: codexbridge
  name: variable-call-example
  version: 1.0.0
do:
  - update_decision:
      call: codexbridge-variable
      with:
        operation: assign
        declaration_id: review_decision
        scope: workflow
        value: "ACCEPTED"
~~~

OWSがこのcallをSchema-validにすることと、CodexBridge interpreterが現在実行をサポートすることは別です。OWS validatorとinterpreter support tableの両方を確認し、legacy定義のvalidation結果を変えません。

この文書ターンでは、添付OWS v1.0.3 Schemaを使った隔離in-memory validationがPASSし、現行interpreterの_validate_action(operation='call')は期待どおりunsupported call targetとして拒否することを確認しました。DB変更、Workflow実行、Codex job、MCP呼出しは行っていません。これはsampleの静的schema/validator照合であり、pytest等のコードテストではありません。

### 5. DBOS execution keyとcall result

各callはrun binding、stable OWS node path、root frame identity、単一callのmutation ordinal 0からexecution_keyを作り、I4 variable mutation ledgerへ渡します。後続sliceで一つのnode stepから複数mutationを発行する場合はmutation ordinalを個別に割り当てます。

DBOS step再生時は同一key/payloadをledgerと照合し、保存済みhistorical after value/digest/versionを使ってそのcall結果を返します。後続mutationによりlatest variable stateが進んでいても古いafter valueへの一致要求・上書き・巻戻し・再appendはしません。同key異payload、chain不整合、binding/frame不一致はfail closed/needs_reconciliationにします。これはCodex adapterの外部送信MAYBE_SENT状態機械とは別です。

codexbridge-variableのcall resultは既存OWS CallFunctionのdataflow/output規則に合わせます。結果値の具体的projectionは実装前に確定しますが、永続変数からOWS contextへの暗黙書込み・全変数exportは行いません。明示task output/exportがある場合だけ既存OWS規則に従います。

## Failure / error behavior

| Failure | Proposed behavior |
| --- | --- |
| legacy revisionでcodexbridge-variableを呼ぶ | 既存legacy interpreter subsetに従いunsupportedとして拒否。$cbvars注入なし |
| strict revisionで未知call/operation/余分なargument | safe validation/runtime error、mutationなし |
| contract宣言・scope・root frameを解決できない | state writeせずfailedまたは既存reconciliation境界へ渡し、成功値を公開しない |
| by型/範囲、value_schema、pointer、jq式に不適合 | mutation transaction前に拒否し、値をerror/logへ含めない |
| DBOS replay、同key異payload、ledger conflict | 台帳結果を検証し、conflictはneeds_reconciliation。二重適用しない |
| presentでない変数を$cbvarsから読む | missing/nullを混同せず明示的参照エラー |
| run binding/variables DB/schema/ACL/path不整合 | strict実行を止め、DBを勝手に作成・修復・公開しない |

失敗はWorkflow runtime statusとADR 0006のsafe reason_code/error projectionで表現し、受入条件のREJECTEDとruntime failed/UNKNOWNを混同しません。MAYBE_SENTのCodex外部jobを再送する理由にしません。

## 受入テスト案

1. OWS v1.0.3 YAML Schemaは例のCallFunction/call/withを受け入れるが、現行interpreter fixtureはcodexbridge-variableをunsupportedとして拒否する。
2. Strict revisionだけがcodexbridge-variableを許可し、legacy revisionでは拒否する。mock-job/codex-jobの既存形と挙動は変わらない。
3. Operation別required/optional key、unknown operation/key、余分なargument、caller指定run/frame IDを検証し、不正入力ではstate/ledgerにwriteしない。
4. declaration_idは同じstrict revisionの宣言に一致し、scope=workflow/root frameだけを解決する。存在しないid、Issue/Attempt/Loop/Subflow scopeは拒否する。
5. initialize/assign/increment/decrement/append/object_set/object_delete/variable_deleteを各value_schemaで検証し、JSON null/missing、bool/number、integer、finite numberを混同しない。
6. by省略=1、by=0/正の有限numberを扱い、bool/負/NaN/Infinityを拒否する。decrementは非負byの絶対量を減らす。
7. JSON literal/jq式のvalueは現行setと同じ許可規則を使い、テンプレート記法や未許可jq機能を増やさない。
8. RFC 6901 pointerのescape、欠落parent、required property delete、object_set後Schema違反を検査し、失敗時にtransactionをrollbackする。
9. $cbvarsはstrict root frameのpresent valueだけをread-onlyで示し、missing/tombstoneをnullにせず、legacyや他scopeに出ない。
10. DBOS step commit前/後checkpoint crash、same-key replay、後続mutation後のhistorical after再生、同key異payload、chain conflictで二重mutation/巻戻しがない。
11. adapter call result、explicit output/export、ows context/setの既存dataflowを照合し、暗黙context同期をしない。
12. invalid call、quota/error、reconciliationでvariable value/prompt/input/secret/tracebackをHTTP/MCP/UI/log/eventへ漏らさず、実Codex POSTを行わない。

## Alternatives

| Alternative | 判断 |
| --- | --- |
| 新しいOWS task type/公式YAML schema keyを追加 | 採用しません。公式OWS schemaとdefinition interchangeを変更せず、CallFunction内のstrict-only allowlist targetを使います |
| OWS setを永続変数代入として流用 | 採用しません。既存setはdataflow valueを作る機能であり、declaration/scope/DBOS ledger/typed variable semanticsとは異なります |
| 任意のexternal function/plugin/MCP callへdispatch | 採用しません。D1変数mutationの外部副作用を増やし、run-local transaction/replay保証を損ないます |
| 各変数操作ごとにOWS専用task typeを設ける | 採用しません。OWS schema変更と複数の互換surfaceを要するため、既存CallFunctionの引数契約へ閉じます |

## Open Questions

1. strict-only call targetを認識するvalidator/runner modeをどのversioned callsiteで選ぶか、全worker/recovery/readerが対応済みと判定する方法。
2. Runtimeからimmutable run bindingとroot frame_identityを受け取るadapter interface、および$cbvars read snapshot consistencyを保証する読み取り境界。
3. 既存ows setと同一と認めるjq expression/literal evaluatorの境界、jqへ$cbvarsを注入する実装点、missing/tombstone参照errorの内部code。
4. adapter callの戻り値を既存CallFunction dataflow outputへ対応させるmappingと、explicit output/export時のschema検証順。
5. isolated Windows下でのDBOS crash/replay test位置、runner/OWS validatorの回帰fixture、UI/MCP/log projectionのredaction test配置。Issue/Attempt/Loop/Subflow frameとdynamic promptはD5/D6/D2以降に残します。

本ADRはProposedです。実装開始、OWS schema変更、legacy call許可、D1最終承認を意味しません。
