# D1: Workflow共通入出力契約・変数・スコープ

- **文書状態:** DRAFT（ユーザーの設計レビュー待ち）
- **基本仕様:** v0.2は合意済み。以下の具体設計は提案であり、最終承認ではない。
- **関連:** [README](../README.md)、[要件](../requirements.md)、[アーキテクチャ](../architecture.md)、[ADR 0001](../decisions/0001-structured-loops.md)

## 1. 背景と目的

現行WorkflowはOWSのYAML、input_data、jq式、単一context、task実行結果を使います。保存済みdefinition revisionと実行requestには既にdigest・revision bindingがありますが、Workflow共通のinput/output JSON Schema、型付き変数宣言、scope frame、再実行で二重適用しない変数mutation ledgerはありません。

D1の目的は、呼び出し元がGUI、MCP、HTTP API、別Workflowのいずれでも、同じimmutable revisionに対し同一の検証・値の受渡し・変数更新を適用できる契約を定義することです。Workflowをmain/subという種類で分割しません。メインフローの既存input_dataとJSON resultの形、OWS v1.0.3の公式Schema、既存revisionの意味を維持します。

本書は将来の実装に使える設計案です。実装コード、DB migration、API変更はこの文書の対象外です。

## 2. 範囲と設計境界

### D1で扱う

- revision固定のinput Schema、output Schema、variable declarations
- 既存OWS YAMLとCodexBridge側のversioned contract sidecar/envelopeの対応付け、digest照合
- contractなしのlegacy modeと、明示opt-inするstrict contract
- JSON Schema Draft 2020-12を使った入力・出力検証と、入力の一度だけの正規化
- 型付きJSON値、Workflow単位と構造的scope frame、明示的参照と更新
- Workflow run、scope frame、変数値、version/digest、mutation execution keyの永続モデル案
- DBOS step/checkpointとmutation台帳の回復境界
- 公開投影、redaction、エラー分類、D1受入条件

### D1が実装しない

- 汎用Agent node、Agent用promptテンプレート構文、動的prompt（D2）
- Repeat-Until等の条件付き反復の実装（D5）。D1はscope frameとiteration vectorが将来利用できる契約だけを設けます。
- 親から子Workflowを実際に起動する実行機能（D6）。D1は引数・戻り値の境界を定義します。
- 複数Issueの実運用・集計（D7）。Issue/Attemptは汎用runtimeのhard-coded概念にしません。
- GUI編集画面（D8）
- 既存OWS set/export、旧revision、Codex MAYBE_SENT台帳の変更

## 3. 用語

| 用語 | 意味 |
| --- | --- |
| contract | あるWorkflow revisionに固定されたinput JSON Schema、output JSON Schema、variable declarationsの一組 |
| sidecar / envelope | OWS YAMLを拡張せず、CodexBridge側でcontractをrevisionと一緒に保存する方式案 |
| strict contract | revisionが明示的にcontractを持つopt-in実行 |
| legacy mode | contract未指定の既存revisionを従来のOWS検証・実行規則で扱う互換mode |
| scope frame | run内の構造的な変数有効範囲。Workflow、loop iteration、nested block、subflow call contextを表す |
| 論理scopeラベル | Workflow、Issue、Attemptなど、用途を説明するscope名。汎用interpreterに業務固有のscopeを埋め込まない |
| execution_key | 一つのmutationを再実行時に同一と識別する決定的キー |
| normalized input | Schema defaultなどの入力正規化を一度適用し、その後のvalidationとrequest digestに使う値 |
| 公開projection | Graph、MCP、Console、通常ログに返す、機密値を含まない情報 |

## 4. Contractとrevision data model案

### 4.1 共通contractの概念形

次は概念モデルです。実際のYAML key、JSON key、DB列名は未決定です。

    WorkflowRevisionContract
      input_schema: JSON Schema Draft 2020-12
      output_schema: JSON Schema Draft 2020-12
      variables: VariableDeclaration[]

    VariableDeclaration
      name: Workflow内の宣言名
      scope: 論理scope labelと構造scope上の所有位置
      type/schema: JSON値の型と許可範囲
      initial/default: 省略可能な明示初期値
      sensitivity: 公開値かsecret候補か（secret保存の採否は未決定）
      export: Workflow output等に出す場合の明示規則

contractはWorkflowの種類や呼び出し元に依存しません。すべての呼び出し元が同じrevisionとcontractを解決して、同じ検証・初期化・出力検証を通ります。

### 4.2 保存とbinding

**設計案:** OWS YAMLはそのまま保存し、CodexBridge側のversioned contract sidecar/envelopeをdefinition revisionに対応付けてimmutableに保存します。既存のOWS SchemaへCodexBridge独自propertyを黙って追加しません。

概念上、保存revisionは次の組を持ちます。

    definition_id
    revision
    source_yaml
    source_yaml_sha256
    contract_payload
    contract_sha256
    binding_digest
    created_at

既存definition repositoryが持つYAML source hashとcanonical hashを維持します。contract hashはcontractのcanonical JSON表現から計算し、binding digestはrevision identity、YAML hash、contract hash、envelope versionを結び付ける案です。canonical JSONの厳密な形式やfield順序は実装前に定めます。

revisionを読み出すときは、保存されたYAML・contractそれぞれのdigestとbindingを照合してから使います。definitionとcontractの一方だけを新しいrevisionへ置換することは許しません。過去runの再読込でも、そのrunに記録されたdefinition_id/revisionとdigestを使います。

contract未指定の保存済みrevisionはlegacy modeとします。新規strict contractは明示opt-inにします。legacy YAMLを一括再検証・再保存して新しいdefaultやunknown-property規則を適用しません。legacy revisionに後からcontractを追加する方法、update時にcontractを省略した場合の扱いはOpen questionsです。

**未決定の保存事項:** sidecar/envelopeの直列化keyとversion文字列、DB列または別artifactの選択、DB migration番号、公開URL、revision update DTO、backup/restore単位は設計レビュー後に決めます。

## 5. JSON Schema検証と入力正規化

### 5.1 Schema dialectと対象

strict contractは既存のjsonschemaライブラリに合わせJSON Schema Draft 2020-12を使う案です。対象型はstring、number、integer、boolean、object、array、nullです。対象制約はtype、required、additionalProperties、enum、items、defaultを含めます。追加キーワードは採用範囲が決まるまで暗黙に実装済みと扱いません。

Schemaの $ref を使って外部URL、ファイル、その他のネットワーク資源を取得することは禁止します。draft案ではcontract文書内のfragment参照だけを候補とし、内部参照の許可範囲はOpen questionです。validatorには外部resolverを渡さず、検証中にネットワークを使いません。

### 5.2 Missing、null、default

- missing propertyは、明示値JSON nullとは異なる状態として扱います。
- nullはSchemaの型宣言にnullが含まれる場合にだけ有効です。
- JSON Schema validator自体がdefaultを値に挿入すると仮定しません。
- **提案:** defaultを有効にするstrict contractでは、入力正規化段階で欠けている位置へ一度だけ適用し、正規化後の値をSchema validation、実行、request digestで共通利用します。validator呼び出しのたびにdefaultを追加しません。
- default自身がSchemaに不適合ならcontractを無効とします。明示nullをdefaultで上書きしません。
- nested object、array item、親object全体のdefaultを再帰的に適用する範囲は未確定です。

### 5.3 Unknown propertyとrequest id

objectのunknown fieldは各object SchemaのadditionalPropertiesに従います。legacy modeの既定挙動は従来のままです。strict contractでadditionalProperties未指定をどう扱うか（JSON Schema標準の挙動を採るか、Schema authorに明示を求めるか）はOpen questionであり、新しい拒否規則を確定事項にしません。

request idの冪等性に使うdigestは、正規化後input、固定revision identity、YAML/contract binding、workspace等の既存実行identityを含める案です。同じrequest idで正規化結果とidentityが同じなら既存runの結果を返し、異なる場合はrequest conflictにします。Schema defaultを適用する場合、正規化前の異なる表記でも正規化後の値が同じなら意味的に同一requestとなります。

## 6. Workflow input/outputと親子境界

### 6.1 公開契約案

次の名前は論理API案であり、確定したwire field名ではありません。

    validate_revision(revision, raw_input)
      -> normalized_input, revision_identity, contract_digest

    execute_revision(revision_identity, normalized_input, client_request_id)
      -> existing Workflow JSON result shape

    validate_output(revision_identity, output_value)
      -> valid output / contract violation

既存のsaved Workflow startは選択revision、client request id、workspace、input_dataを使います。D1で別のmain/sub呼び出し口を作りません。現行のHTTP start request上限は128 KiB、OWS YAML上限は64 KiBです。契約追加後の総量と保存・応答上限は互換性を確認してから決めます。

Workflow呼び出し元はinput Schemaを使って一度だけ正規化・検証します。成功時にDBOS runへ渡すinput_dataは正規化済み値です。既存メインフローのinput_dataとJSON resultの形は維持し、Schemaが値の意味やwire shapeを勝手に変えません。

### 6.2 親子呼び出し契約

D1は実サブWorkflow起動を実装しません。契約案として、将来の親ノードはchild revisionを固定し、明示した引数JSONをchild input Schemaへ渡します。childは自身のcontractとscopeで実行され、検証済みoutputだけが明示return/exportとして親へ渡ります。

- 親scopeから子ローカル変数への暗黙書き込みを禁止します。
- 子が親値を使う場合は引数値として受け取ります。
- 親が戻り値を使う場合は明示的に受け取り先へ代入・exportします。
- 同じ名前の変数が親子にあっても名前だけで参照先を推測しません。

### 6.3 出力検証

strict contractのWorkflowは正常終了前にoutput Schemaでresult候補を検証します。検証に失敗したrunは成功とせず、確定した契約違反としてFAILEDにします。失敗値を親Workflowへ返しません。外部処理の状態が不確定であるUNKNOWNと、既に確定した不正outputを混同しません。既に発生したGit等の外部副作用の補償はD1の対象外です。

## 7. 変数型・宣言・scope frame

### 7.1 値の型と参照

変数値はJSONに直列化できるtyped valueです。宣言はJSON Schemaの対象型を用いて値を検証する案です。欠けた値はJSON nullに読み替えません。読み取り時に宣言済みscopeに値が存在しなければMissingVariableとして扱います。

参照は宣言nameに加えてscope selectorまたはdeclaration identityを明示します。実行場所から暗黙に最も近い同名変数を探しません。同名宣言の衝突を曖昧に解決せず、無効または曖昧なscope参照を検証エラーにします。

正式なWorkflow内参照式とprompt template構文はD2で定めます。D1では「型付き宣言とscope指定を持つ値を読み書きする」という抽象契約を定めます。

### 7.2 論理scopeと汎用frame

論理scope labelはWorkflow（run固有）、Issue（Issue loop iteration）、Attempt（Repeat-Until iteration）です。runtime実装はIssueやAttemptという業務名を埋め込まず、以下のような一般的frameを持つ案です。

    WorkflowFrame(run_id)
      LoopFrame(parent_frame_id, stable_loop_path, iteration_index)
        NestedBlockFrame(parent_frame_id, stable_node_path)
        SubflowCallFrame(parent_frame_id, child_revision, call_context)

Issue/Attemptは対応するloop frameに付く論理ラベルです。各iterationは一意のframe identityを持ち、次のIssueやAttemptへ値が漏れません。frame entry時に宣言を初期化し、frame exit後のローカル値は明示exportなしでは親scopeへ出しません。

Workflow scopeはrun内で有効です。Issue値は該当Issue frame、Attempt値はそのRepeat-Until iteration frame内で有効です。複数loopやnested blockが加わってもruntimeの汎用frameで表します。

### 7.3 提供する変数操作

| 操作 | 提案動作 | 型・欠落の扱い |
| --- | --- | --- |
| 初期化 | declaration defaultまたは明示値をframe entry時に一度設定 | default/valueを宣言Schemaで検証。defaultなしの未初期化値はmissingのまま |
| 代入 | 指定scopeの宣言済み変数へ値を設定 | 設定前に型・Schemaを検証 |
| 加算 / 減算 | 数値値を更新 | booleanをnumberとして扱わない。型を厳密に保ちNaN/Infを拒否 |
| array append | 配列末尾へ一要素を追加 | 配列型を検証。実行replayはexecution_key台帳で二重適用を防止 |
| object部分更新 | 指定objectの明示path/keyを更新 | 対象がobjectであることを確認。存在しない親objectを暗黙生成しない案 |
| 削除 | 変数自体または明示object pathを削除 | 欠落とJSON nullを区別。未宣言変数や無効pathはエラー |

配列値の意味的な重複を一般にdeduplicateするかは確定しません。同じmutationのreplay二重追加は必ず防ぎます。異なるexecution keyから同値を追加できるか、明示unique-by操作を設けるかはOpen questionです。

## 8. 永続化とDBOS回復設計案

### 8.1 概念的な永続モデル

DB table名・列名を確定する前の論理モデルです。

| レコード | 必須の論理情報 |
| --- | --- |
| VariableState | run_id、scope_frame_id、variable_name、type、value、version、value_digest、機密区分 |
| ScopeFrame | run_id、frame_id、parent_frame_id、stable_scope_path、iteration vector、call context、frame status |
| VariableMutation | execution_key、payload_digest、before_digest、after_digest、before/after version、commit outcome |
| RunContractBinding | run_id、definition_id/revision、YAML hash、contract hash、binding digest、normalized input digest |

ユーザーの未追跡ファイルやOWS汎用contextへvariable stateを混ぜません。Secret valueの保存方式が決まるまではsecret値を受け付けない、または拒否する案です。

### 8.2 mutation identity

execution_keyは、次のidentityを決定的に結合する案です。

    run_id
    + fixed definition revision / binding digest
    + stable node path
    + full vector of all enclosing loop iterations
    + nested scope path
    + subflow call context (when present)

1つのnode pathだけでは複数iterationを区別できず、ランダムUUIDだけではreplay時に同じ操作を特定できません。execution_keyは同一runの同一mutationを表すため、DBOS replayで安定して再生成します。

### 8.3 原子的更新手順

変数stateとmutation ledgerは同じDB transaction内で更新します。単に値をSQLiteへ保存しても、DBOS stepの再実行でincrementやappendが二重適用される問題は解決しません。

1. DBOS named stepがexecution_key、payload_digest、期待するbefore version/digest、operationと値を構成します。
2. workerがSQLite write transactionを開始し、同じexecution_keyのledger行を探します。
3. 既存行がありpayload_digestも同じなら、保存済みafter version/digest/valueを検証して記録済み結果を返します。mutationを再適用しません。
4. 既存行が同じkeyで別payloadなら、runをNEEDS_RECONCILIATION相当の状態にし、再適用せずconflictを返します。
5. 初回ならbefore version/digestを現在値と照合し、operation、型、scope、Schemaを検証します。
6. 新しい値・version/digestとmutation ledgerを同じtransactionでcommitします。どちらかだけをcommitしません。
7. DBOS stepのcheckpointにはexecution_keyとpayload/before/after digestを含めます。workflow側はcheckpointから復帰した後も対応ledgerとの一致を確認してから次のnodeへ進みます。

### 8.4 Crash/replay境界

DBOS checkpointと変数SQLite transactionを一つの物理transactionにできるとは仮定しません。execution_key ledgerがその境界をつなぎます。

| クラッシュ位置 | 永続状態 | Replay時の処理案 |
| --- | --- | --- |
| transaction開始前 | ledgerも値も未変更 | 同じstepが初回mutationとして実行 |
| transaction中、commit前 | transaction rollbackでledger・値とも未変更 | 同じkeyで初回実行 |
| SQLite commit後、DBOS checkpoint前 | ledgerと値はcommit済み、checkpointは未完了 | step再実行は同一key・payloadを読み、after stateを返して二重更新しない |
| DBOS checkpoint後 | checkpointとledgerがある | checkpoint identity/digestとledgerを照合して続行 |
| 同一keyでpayload違い | 既存ledgerと再生要求が不一致 | conflict、値を上書きせずneeds_reconciliationへ |
| checkpointとledgerの片方欠落・digest不一致 | durable記録間に矛盾 | 自動修復や再計算をせずneeds_reconciliationへ |

既存Codex MAYBE_SENTは外部送信の不確実性です。変数更新がcommit済みかの確認にMAYBE_SENTを流用しません。

## 9. 実行処理順序

strict contractの一回のsaved Workflow runは、次の順序を通る案です。

1. 指定されたimmutable definition revisionを読み、OWS YAML、contract sidecar、各digestとbindingを検証する。
2. contract有無を確認する。ない場合はlegacy modeへ移り、従来検証を保つ。ある場合のみstrict経路へ進む。
3. Schema dialect、許可キーワード、内部参照、declaration一意性、default自身、scope宣言を検証する。
4. raw inputに正規化を一度適用する。missing/nullを保ち、採用されたdefaultだけを追加する。
5. normalized inputをinput Schemaで検証する。invalidならWorkflowを起動しない。
6. 固定revision、contract binding、normalized input、既存request identityを含むrequest digestを作る。同じrequest idの同一digestは既存runへreplayし、異なるdigestはconflictにする。
7. Workflow frameと変数宣言を初期化して永続化する。loop/nested frameはD5等が将来入る位置で親から分離して作る。
8. 各mutationをDBOS named stepとSQLite mutation ledgerで実行する。
9. Workflow終了時に明示return/exportのJSON候補を作り、output Schemaで検証する。
10. validなら既存JSON result shapeに沿って結果を確定する。不正ならFAILEDとして扱い、親への戻り値を出さない。
11. Graph/MCP/UI/通常ログは公開projectionを作り、入力・prompt・variable・outputの秘匿値を含めない。

## 10. 公開・内部API契約案

以下はD1レビュー用の論理契約で、現行public endpointの変更宣言ではありません。

| 契約 | 入力 | 成功結果 | 失敗の扱い |
| --- | --- | --- | --- |
| revision contract load | definition_id、revision | YAML/contract/binding identity | revision欠落はNOT_FOUND、hash不整合は保存不整合 |
| input normalize/validate | 固定contract、raw JSON | normalized JSON、normalization digest | Schema不正・必須欠落・型不一致は実行前拒否 |
| variable read | run/frame selector、declaration selector | typed JSON value、version/digest | missing/ambiguous/invalid scopeは明示エラー |
| variable mutate | execution_key、payload、expected before identity | commit済みvalue/version/digest | stale before、同key異payloadはconflict |
| child input boundary | fixed child revision、explicit argument JSON | child用normalized input | D6で実装。D1では契約のみ |
| output validate/return | fixed contract、candidate JSON | 検証済みJSON result | contract違反はFAILED、親へ返さない |

Graph、MCP、Consoleの公開値はrun status、frame/node identity、宣言された型、version/digest等を必要範囲に限定します。値の取得・編集UIや各APIの具体フィールドは後続設計です。

## 11. バリデーションと互換性

### 11.1 Schema・binding検証

- Draft 2020-12以外のdialectはstrict contractで受け付けない案です。
- malformed Schema、未対応キーワード、remote $ref、digest mismatch、revision不一致を実行前に検出します。
- contractとOWS YAMLは同じimmutable revisionとして照合します。
- Schemaが使うJSON値にNaN/Infinity等のJSON外数値を認めません。
- 外部ネットワーク経由の参照解決を行いません。

### 11.2 Input/output

- required欠落と明示nullは別の検証結果にします。
- enum、items、nested object type、additionalPropertiesを定義済みSchemaのルールで検証します。
- defaultは正規化段階に限って一度だけ注入します。default適用結果をvalidationとdigestに再利用します。
- input contract invalidは開始拒否、output contract invalidは成功扱いしないFAILEDとし、親へ返しません。
- contractなしのlegacy definitionには新validation、default、unknown-property規則を遡及適用しません。

### 11.3 変数・scope

- declaration name/type/scopeの重複や曖昧参照を事前に検出します。
- scope frameが存在しない、閉じたframeを読む、暗黙parent writeを行う参照は拒否します。
- missingを0、空文字、空配列、nullへ黙って変換しません。
- 加減算ではbooleanを数値扱いしません。numberは有限値だけ、integerは整数値だけを認めます。
- 配列/object操作の対象型とpathを確認し、更新結果も宣言Schemaで検証します。
- execution_keyは同じrun/revision/node/iteration/callに対して安定し、payload違いをconflictにします。

## 12. 機密性、公開projection、リソース上限

- prompt、input、変数値、output値の秘匿対象は通常ログ、Graph、MCP、UIに出しません。通常のexecution eventは実値ではなくstatus、stable identity、必要なdigest等を記録します。
- 機密変数の保存valueと公開projectionは別経路にします。公開projectionは値をredactし、例外文・Schema errorにも入力値を埋め込みません。
- 既存のCodexBridge tokenをWorkflow変数へ自動注入しません。
- secret valueをplaintext DB保存する方式は未決定です。保管方式が定まっていない限り、secret値を扱えない／宣言時または入力時に拒否する案です。暗号化・key管理を既定済みと見なしません。
- 現行のHTTP Workflow requestは128 KiB、YAMLは64 KiBです。Schema/sidecar、input/output、variable value、Graph/MCP projectionの合計、最大深度、Schema node数、参照展開量には既存上限と整合する上限が必要です。具体値は未確定であり、このDRAFTでは新しい数値上限を設定しません。
- JSON Schema validatorで外部ネットワーク参照を行わず、Schema complexity制限も実装時に計測可能な形で定義します。

## 13. エラー分類案

HTTP/MCP status codeと公開error stringは未確定です。以下は内部分類と処理結果の設計案です。

| 条件 | 分類案 | run/resultへの扱い |
| --- | --- | --- |
| definition revisionがない | NOT_FOUND | 起動前エラー |
| YAML/contract digestまたはbindingが不一致 | REVISION_INTEGRITY_ERROR | 起動拒否、保存不整合 |
| Schema dialect/形が無効 | CONTRACT_INVALID | 起動前エラー |
| remote $ref または外部resource | CONTRACT_REFERENCE_FORBIDDEN | 起動前拒否、ネットワーク取得なし |
| required field missing、type/enum/items不一致 | INPUT_CONTRACT_INVALID | run起動前拒否 |
| 同一request idでnormalized input/identityが違う | REQUEST_ID_CONFLICT | 既存runを再利用せずconflict |
| legacy revisionにcontractなし | LEGACY_MODE | 従来の検証経路で続行 |
| declaration未定義、value missing、無効scope | VARIABLE_REFERENCE_INVALID | 該当step failure |
| bool加算、型不一致、有限数でない | VARIABLE_OPERATION_INVALID | mutationをcommitしない |
| stale before digest/version | VARIABLE_STATE_CONFLICT | retryで上書きせずconflict |
| execution_key再利用でpayload違い | MUTATION_ID_CONFLICT | needs_reconciliation相当 |
| checkpoint/ledger不一致 | MUTATION_RECONCILIATION_REQUIRED | 次nodeへ進まない |
| output Schema違反 | OUTPUT_CONTRACT_VIOLATION | runをFAILED、親へ返さない |
| secretが未対応 | SECRET_VALUE_UNSUPPORTED | 受理・保存・公開しない |
| 設定上限超過 | RESOURCE_LIMIT_EXCEEDED | 実行前または更新前に拒否 |
| Codex外部送信が不確定 | 既存MAYBE_SENT / UNKNOWN | 変数mutation conflictと混同せず再送しない |

## 14. D1受入テストマトリクス

以下はD1を将来実装するときに必要な具体受入項目です。現在の設計文書作成ターンでのコードテスト実行を意味しません。

| ID | ケース | 期待結果 |
| --- | --- | --- |
| D1-01 | valid input/output Schemaと値 | 正規化・検証後にstrict run開始 |
| D1-02 | malformed Schema、異なるdialect、無効キーワード | 起動前にcontract error |
| D1-03 | required property missingと明示null | missingとnullを別扱いし、null可否に従う |
| D1-04 | Schema defaultがある値 | 正規化で一度だけ注入し、validationとrequest digestへ同じ値を使う |
| D1-05 | nested object型、array items、enum | valid/invalid値をSchema通りに判定 |
| D1-06 | additionalPropertiesあり/なし | 明示設定に従う。未指定規則は採択したOpen question回答で確認 |
| D1-07 | 同じrequest id、異なるraw表現だがnormalized value同じ | 同一identityなら同じrunへreplay |
| D1-08 | 同じrequest id、normalized valueまたはrevision bindingが異なる | conflictとして新規実行しない |
| D1-09 | 旧immutable revisionを再取得、後続revisionを追加 | 旧YAMLと旧contractが不変で同じbindingを返す |
| D1-10 | contract未指定legacy definition | 既存検証・実行規則を維持し新Schemaを適用しない |
| D1-11 | Workflow / Issue / Attempt相当の異なるloop frame | 値がframe間で分離し、frame exit後に暗黙参照できない |
| D1-12 | 同名変数を明示scopeで参照 | 指定scopeの値だけを返し、曖昧参照は拒否 |
| D1-13 | 変数初期化、代入、有限number加減算 | 宣言型・version/digestを保って更新 |
| D1-14 | booleanの加算、missingへの暗黙加算、型不一致 | 更新せず明示エラー |
| D1-15 | array append、部分object update/delete | 正しい要素/pathのみ更新し、Schema再検証 |
| D1-16 | 同一append/incrementのstep replay | 同じexecution_keyで一度だけ反映 |
| D1-17 | SQLite transaction commit前のcrash | ledgerと値が共に未反映、replayで一回反映 |
| D1-18 | SQLite commit後・DBOS checkpoint前のcrash | replayで保存済みafter valueを読み、二重加算/追加しない |
| D1-19 | checkpointとledgerの両方がある回復 | key/payload/before/after digestを照合して続行 |
| D1-20 | 同一execution_keyで異なるpayload | overwriteせずconflict、needs_reconciliation相当 |
| D1-21 | before digest/versionが古い | mutationせず状態conflict |
| D1-22 | 子Workflow return境界の契約模擬 | child outputをSchema検証し、明示returnのみ親へ渡す |
| D1-23 | wrong output contract | runはFAILED、成功JSONとして返さず親にも返さない |
| D1-24 | secret・token・入力値の投影 | tokenを注入せず、通常log/Graph/MCP/UI/例外に値を出さない |
| D1-25 | 外部URL $ref | networkアクセスなしで起動前拒否 |
| D1-26 | body、YAML、Schema depth/complexity、value size上限 | 実装時に決めた上限で境界値を判定し、既存128 KiB/64 KiBと整合 |
| D1-27 | OWS set/input/output/export、保存済み旧revision、既存jump regression | 既存動作と実行結果の互換性を維持 |

D1実装時の検証計画にはpytest、Ruff、mypy、compileall、および隔離したWindows smokeを含めます。実装時に既存CI・適用可能なコマンドを再確認します。この文書のみの変更ではこれらを実行しません。

## 15. D1出口条件

D1実装の出口では、一つの保存済みWorkflowについて、revision固定contract検証、入力正規化、変数初期化・代入・数値/配列/object変更・削除、output validation、明示exportがDBOS回復下でも再現できる必要があります。same execution_key replayでmutationを二重適用せず、競合時は上書きせず状態を照合可能にします。

この出口条件はD2の汎用Agent nodeや動的prompt、D5のRepeat-Until実装、D6の実subflow起動、D7の複数Issue運用、D8のGUI編集をD1へ持ち込みません。既存OWS動作・旧revision・Codex MAYBE_SENT再送規則は維持します。

## 16. Open questions

次の事項は本DRAFTで最終決定しません。

1. contract envelopeのversion文字列・serialisation key・保存先（revision row、sidecar artifact等）、migration番号、公開URL。
2. revision update時にcontractを必ず明示するか、既存contractを継承するか、contractなしrevisionへ追加する更新手順。
3. Draft 2020-12の対象keyword allowlist、内部 $ref / $defs の範囲、Schema metaschemaの保持・検証方法。
4. additionalProperties未指定時に標準挙動を採るか、strict contractではauthorへ明示を求めるか。
5. defaultをobject property、nested object、array itemへ適用する範囲と、変数declaration defaultとの優先順。
6. string/number等の細かなconstraint、JSON root scalarを既存saved-run APIで扱うか。
7. declaration構文、scope selector/reference syntax、同名宣言の許可範囲、object path表現。
8. object updateが既存key以外を追加できるか、arrayのvalue重複を許すか、unique-by操作を用意するか。
9. DB schema/migration、SQLite transaction connection、DBOS checkpointからledgerへ照合する具体API。
10. secret valueの暗号化、key ownership/rotation、未対応時のerror mapping。平文保存を前提にしない。
11. Workflow/input/output/value/depth/Schema-node/response/event上限の具体値と128 KiB API、64 KiB YAML、MCP/Graph上限との整合。
12. HTTP/MCPの公開fieldとstatus、Consoleでのcontract取得・redaction投影。
13. revision updateと過去run replayにおけるnormalized input・default適用記録の保存形式。
14. output違反の公開status/event名、および外部副作用が発生した後のレビュー・再実行手順。

この回答待ちで文書作成を止めず、上記はD1のレビュー入力として保持します。
