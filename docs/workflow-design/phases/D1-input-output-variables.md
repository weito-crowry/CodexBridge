# D1: Workflow共通入出力契約・変数・スコープ

- **文書状態:** DRAFT / Proposed（レビュー待ち）
- **基本仕様:** v0.2は合意済み。本書のProposed decisionsは第1回の詳細設計候補であり、設計承認・実装完了を意味しません。
- **関連:** [README](../README.md)、[要件](../requirements.md)、[アーキテクチャ](../architecture.md)、[ADR 0001](../decisions/0001-structured-loops.md)

## 1. 背景と目的

現行WorkflowはOWSのYAML、input_data、jq式、単一context、task実行結果を使います。保存済みdefinition revisionと実行requestには既にdigest・revision bindingがありますが、Workflow共通のinput/output JSON Schema、型付き変数宣言、scope frame、再実行で二重適用しない変数mutation ledgerはありません。

D1の目的は、呼び出し元がGUI、MCP、HTTP API、別Workflowのいずれでも、同じimmutable revisionに対し同一の検証・値の受渡し・変数更新を適用できる契約を定義することです。Workflowをmain/subという種類で分割しません。メインフローの既存input_dataとJSON resultの形、OWS v1.0.3の公式Schema、既存revisionの意味を維持します。

本書は将来の実装に使える設計案です。実装コード、DB migration、API変更はこの文書の対象外です。

## 2. 範囲と設計境界

### D1で扱う

- revision固定のinput Schema、output Schema、variable declarations
- 既存OWS YAMLと同一SQLite DBのrevision従属contract rowの対応付け、digest照合
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
| revision contract row | OWS YAMLと分離し、CodexBridge側の同一SQLite DBでcontractを親revisionに結び付けるProposed方式 |
| strict contract | revisionが明示的にcontractを持つopt-in実行 |
| legacy mode | contract未指定の既存revisionを従来のOWS検証・実行規則で扱う互換mode |
| scope frame | run内の構造的な変数有効範囲。Workflow、loop iteration、nested block、subflow call contextを表す |
| 論理scopeラベル | Workflow、Issue、Attemptなど、用途を説明するscope名。汎用interpreterに業務固有のscopeを埋め込まない |
| execution_key | 一つのmutationを再実行時に同一と識別する決定的キー |
| normalized input | Schema defaultなどの入力正規化を一度適用し、その後のvalidationとrequest digestに使う値 |
| 公開projection | Graph、MCP、Console、通常ログに返す、機密値を含まない情報 |

## 4. Contractとrevision data model案

### 4.1 共通contractの概念形

次はstrict contract v1の論理JSON形です。OWS YAMLのtop-level keyにも公式配布Schemaにも追加しません。

    {
      "contract_version": 1,
      "input_schema": { "$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object", "additionalProperties": true },
      "output_schema": { "$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object", "additionalProperties": true },
      "variables": []
    }

    VariableDeclaration
      declaration_id: 安定した宣言識別子
      name: Workflow内の宣言名
      owner_scope: root workflowまたは静的scope path
      value_schema: JSON値の型と許可範囲
      initial: 省略可能な明示初期値
      sensitivity: publicまたはsecret

contractはWorkflowの種類や呼び出し元に依存しません。すべての呼び出し元が同じrevisionとcontractを解決して、同じ検証・初期化・出力検証を通ります。

### 4.2 保存とbinding

**Proposed:** OWS YAMLはそのまま保存し、CodexBridge拡張contract JSONを既存WorkflowDefinitionRepositoryと同じSQLite DB内の新規revision従属テーブルに保存します。別ファイルsidecarやOWS YAMLへの属性追加は行いません。

テーブルの概念名は workflow_definition_contracts とし、workflow_definition_revisions のrevisionを外部キーで参照します。

| 列 | 意味 |
| --- | --- |
| definition_id | 親definition |
| revision | 親immutable revision |
| contract_json_canonical | contract JSONのcanonical直列化値 |
| contract_sha256 | canonical JSON UTF-8 bytesのSHA-256 |
| binding_digest | YAML revisionとcontractを結ぶdigest |
| created_at | 保存時刻 |

複合primary keyは (definition_id, revision) とします。YAML source hashと既存canonical hashは既存revisionに保持します。contract_sha256は現行 _canonical_json と同じ決定的直列化（sort_keys、UTF-8、allow_nan=false、compact separators）から計算します。

binding_digestは同じcanonical JSON方式によるSHA-256とし、definition_id、revision、YAML source SHA、YAML canonical SHA、revisionの max_live_codex_jobs、contract_version、contract_sha256を含めます。親revisionの作成または更新、contract rowの追加、mutation request ledgerの更新は、同じSQLite write transactionでcommitします。保存途中に一部だけが残る状態にしません。

legacy revisionにはcontract rowを作らず、従来の検証とrequest identityを維持します。contractだけを同一revisionへ更新せず、contract変更は新revisionを作成します。strict ETagはcontractを含むversioned identityに拡張しますが、wire表現と既存ETag移行影響は後続設計に残します。

### 4.3 create/updateとcontract指定

- legacy create/updateでcontractが省略された場合は従来どおりlegacy revisionとして扱います。
- legacy revisionをstrictにする場合はcontractを明示し、新revisionとして保存します。
- strict revisionのupdateでcontractを省略した場合は拒否します。既存contractを暗黙継承したり、legacyへ黙って戻したりしません。
- 明示nullはcontract解除指示として解釈しません。strictからlegacyへのdowngradeを暗黙に許しません。
- contractを含むmutation digestにはcontractの有無、canonicalな厳密値、対象definition/revisionを含めます。legacy requestの既存digest、同一request idの再送・照合規則は変えません。
- 新旧APIを別routeにするか、同一endpointのoptional fieldにするかは後続設計で決めます。

## 5. JSON Schema検証と入力正規化

### 5.1 Schema dialectと対象

strict v1のinput_schemaとoutput_schemaはrootが type: object の名前付き引数・戻り値Schemaです。これによりstrict呼び出しの形を一定にします。既存legacy revisionのroot scalar outputは読み替えず従来どおりにします。各Schemaは $schema にDraft 2020-12を明示し、保存時に既存jsonschemaライブラリの Draft202012Validator.check_schema を通します。

strict v1で許容する基本keyword候補は、type、properties、required、additionalProperties、items、enum、const、default、$defs、$ref、minLength、maxLength、minimum、maximum、minItems、maxItems、title、description、$schemaです。Schema内で検証意味を持つ未許可keywordをvalidatorが黙って無視することは禁止し、contract compile時に拒否します。pattern、oneOf、anyOf、allOf、not、dependencies等は初期strict v1では使わず、D1後続拡張候補とします。

$refは同一contract Schema内の #/$defs/<name> だけを許可し、参照先が存在する非循環参照に限ります。外部URL・file・network、$dynamicRef、再帰参照は拒否します。深さ・Schema節点数などの数値上限は実装開始前の明示承認ゲートに残します。

### 5.2 Missing、null、default

- missing propertyは、明示値JSON nullとは異なる状態として扱います。
- nullはSchemaの型宣言にnullが含まれる場合にだけ有効です。
- JSON Schema validator自体がdefaultを値に挿入すると仮定しません。入力のdeep copyだけを正規化し、Schemaやvalidatorの出力をmutationしません。
- defaultはinput_schema内のpropertiesにあるmissing propertyの補完だけに使います。root defaultは入力値に適用しません。明示nullをdefaultで上書きしません。
- 実在するnested object、および親property defaultで新しく作られたobjectを一度だけ再帰的に正規化します。配列要素は作成しませんが、既存配列要素がobjectならそのpropertiesを正規化できます。
- default自身が対応するproperty Schemaに不適合なら、definition保存時に拒否します。output_schemaにはdefault補完を行いません。
- 正規化後の値をinput validation、strict idempotency digest、Workflow実行で共通利用します。

### 5.3 Unknown propertyとrequest id

strict v1のすべてのobject SchemaはadditionalPropertiesを明記します。値はboolean、または許可keywordに従うSchemaとします。標準上の暗黙trueをfalseへ書き換えません。legacy modeの既定挙動は従来どおりで、新strict規則を既存revisionへ遡及適用しません。

strict runはstrict v2のversioned manifest/request-digest経路を使い、固定binding_digestとnormalized input hashを明示的に加えます。同じ正規化後値と同じrevision bindingは同一requestとして照合します。過去manifest/request digestを再解釈せず、service.pyの _verified_active_workspace を含む現行legacy v1検証経路はそのまま維持します。未知version、hash不一致、revision不一致はfail closedとします。manifest内の具体的なversion discriminator fieldはOpen questionに残します。

## 6. Workflow input/outputと親子境界

### 6.1 公開契約案

次の名前は論理API案であり、確定したwire field名ではありません。

    validate_revision(revision, raw_input)
      -> normalized_input, revision_identity, contract_digest

    execute_revision(revision_identity, normalized_input, client_request_id)
      -> existing Workflow JSON result shape

    validate_output(revision_identity, output_value)
      -> valid output / contract violation

既存のsaved Workflow startは選択revision、client request id、workspace、input_dataを使います。D1で別のmain/sub呼び出し口を作りません。現行のHTTP start request上限は128 KiB、OWS YAML上限は64 KiBです。strict runはversioned v2 manifest/request-digest経路にbinding_digestとnormalized input hashを加え、legacy v1 manifestの検証と既存request digestは再解釈しません。v1/v2の具体的な識別fieldと契約追加後の総量・保存・応答上限は後続設計に残します。

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

### 7.1 宣言・値の型・参照

各宣言は declaration_id（安定識別子）、name、owner_scope（root workflowまたは静的scope path）、value_schema、optional initial、sensitivityを持ちます。変数値はJSON値で、string、integer、number、boolean、array、object、nullをvalue_schemaで検証します。null可否は型に明記し、missingをnullと読み替えません。

各read/writeは declaration_id と該当frame_identityを明示します。親scopeを暗黙探索したり、子から親へ暗黙書込みしたりしません。同名変数は異なるowner_scope間では許可しますが、declaration_id重複または同一scope内の同名宣言はdefinition検証時に拒否します。

正式なWorkflow内参照式とprompt template構文はD2で定めます。D1では「型付き宣言とscope指定を持つ値を読み書きする」という抽象契約を定めます。

### 7.2 論理scopeと汎用frame

runtimeは汎用WorkflowFrame、NestedFrame、LoopIterationFrameを持ち、後続の親子Workflow実装向けにSubflowCallFrameを追加可能な形とします。IssueとAttemptはそのframeに付ける論理ラベルであり、Python runtimeへ業務名をhard-codeしません。

    WorkflowFrame(run_id)
      NestedFrame(parent_frame_id, stable_node_path)
        LoopIterationFrame(parent_frame_id, stable_loop_path, iteration_index)
        NestedBlockFrame(parent_frame_id, stable_node_path)
        SubflowCallFrame(parent_frame_id, child_revision, call_context)

各frameは一意のframe_identityを持ち、Issue/Attemptの次iterationへ値が漏れません。frame entry時に宣言を初期化し、frame exit後のローカル値は明示exportなしでは親scopeへ出しません。現行OWSのdo/for scopeと変数frameが衝突しないよう、static node pathからframe_identityへの明示adapterを設計します。OWS既存input/context/set意味は変更しません。

Workflow scopeはrun内で有効です。Issue値は該当Issue frame、Attempt値はそのRepeat-Until iteration frame内で有効です。複数loopやnested blockが加わってもruntimeの汎用frameで表します。

### 7.3 提供する変数操作

| 操作 | 提案動作 | 型・欠落の扱い |
| --- | --- | --- |
| initialize | frame entryでinitialがあれば一度設定 | initialをvalue_schemaで検証。省略時はmissingのまま |
| assign | 宣言済み変数へ値を設定 | 設定前と更新後にvalue_schemaを検証 |
| increment / decrement | numberまたはintegerを加減算 | numberは有限値、integerは厳密整数。booleanは数値でない |
| append | 配列末尾に一要素を追加 | array Schemaを検証。同値要素の追加は異なるexecution_keyなら許容 |
| object_set | RFC 6901 JSON Pointerでobject値を設定 | 既存親object上の新しい末尾keyは作成可能。存在しない親は作らず、更新後全体をvalue_schemaで検証 |
| object_delete | RFC 6901 JSON Pointerでobject値を削除 | required property削除等でSchema違反になる場合はrollback |
| variable_delete | variable値をtombstone化 | 明示操作。更新後Schemaに違反する削除はrollback |

欠落・型不一致・nullをmissingとして扱う操作はfailします。RFC 6901のpointerを明示し、配列値のunique-by/deduplicationはD1対象外です。同じexecution_keyのreplayでappendを二重適用することは禁止します。

## 8. 永続化とDBOS回復設計案

### 8.1 概念的な永続モデル

DB table名・列名を確定する前の論理モデルです。

| レコード | 必須の論理情報 |
| --- | --- |
| VariableState | run_id、scope_frame_id、variable_name、type、value、version、value_digest、機密区分 |
| ScopeFrame | run_id、frame_id、parent_frame_id、stable_scope_path、iteration vector、call context、frame status |
| VariableMutation | execution_key、payload_digest、before_digest、after_digest、before/after version、commit outcome |
| RunContractBinding | run_id、definition_id/revision、YAML hash、contract hash、binding digest、normalized input digest |

ユーザーの未追跡ファイルやOWS汎用contextへvariable stateを混ぜません。strict v1ではsensitivity=secretの宣言を持つWorkflowと、secret値を含むと明示分類されたinputを受け付けず、明示拒否します。secret valueの保存方式は将来の別設計です。

VariableState、ScopeFrame、VariableMutationは同じrunを所有する永続SQLite DBで管理し、各mutationのstate更新と台帳記録を同一transactionにします。DBの配置をrun-directory単位にするかstate-root shared DBにするかはOpen questionです。

### 8.2 mutation identity

execution_keyは、次のidentityを決定的に結合する案です。

    run_id
    + fixed definition revision and contract binding
    + stable node path
    + full vector of all enclosing loop iterations
    + scope/frame identity
    + subflow call context (when present in a later phase)
    + mutation ordinal within the node

1つのnode pathだけでは複数iterationや同一node内の複数mutationを区別できず、ランダムUUIDだけではreplay時に同じ操作を特定できません。execution_keyは同一runの同一mutationを表すため、DBOS replayで安定して再生成します。

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

1. 指定されたimmutable definition revisionを読み、OWS YAML、revision従属contract row、各digestとbindingを検証する。
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

- Draft 2020-12以外のdialectはstrict contractで受け付けません。
- malformed Schema、未許可のvalidation keyword、required additionalPropertiesの欠落、remote/recursive $ref、digest mismatch、revision不一致を実行前に検出します。
- contractとOWS YAMLは同じimmutable revisionとして照合します。
- Schemaが使うJSON値にNaN/Infinity等のJSON外数値を認めません。
- $refは同一Schema内の #/$defs/<name> への非循環参照だけを解決し、外部ネットワーク経由の参照解決を行いません。
- Schema深度・節点数等の具体上限は、実装開始前に明示承認を得ます。

### 11.2 Input/output

- required欠落と明示nullは別の検証結果にします。
- strict v1のinput/output rootはobjectです。enum、const、items、nested object type、各objectに必須のadditionalPropertiesを許可keywordの規則で検証します。
- input defaultだけをdeep copyのpropertiesへ一度注入します。nested objectや既存array内のobjectを対象とし、新しいarray要素は作りません。outputにはdefaultを適用しません。
- input contract invalidは開始拒否、output contract invalidは成功扱いしないFAILEDとし、親へ返しません。
- contractなしのlegacy definitionにはstrict root object、new validation keyword allowlist、default、unknown-property規則を遡及適用しません。legacy scalar outputも維持します。

### 11.3 変数・scope

- declaration name/type/scopeの重複や曖昧参照を事前に検出します。
- scope frameが存在しない、閉じたframeを読む、暗黙parent writeを行う参照は拒否します。
- missingを0、空文字、空配列、nullへ黙って変換しません。
- 加減算ではbooleanを数値扱いしません。numberは有限値だけ、integerは整数値だけを認めます。
- 配列/object操作の対象型とpathを確認し、更新結果も宣言Schemaで検証します。
- declaration_idとframe_identityをread/writeごとに指定します。同名変数はscopeをまたいで許し、同scope内の重複と識別子重複は拒否します。
- object_set/object_deleteはRFC 6901 pathを使い、更新後value_schemaに不適合ならmutation全体をrollbackします。
- execution_keyには同一node内のmutation ordinalも含め、同key異payloadまたはdigest不一致をfail closedにします。

## 12. 機密性、公開projection、リソース上限

- prompt、input、変数値、output値の秘匿対象は通常ログ、Graph、MCP、UIに出しません。通常のexecution eventは実値ではなくstatus、stable identity、必要なdigest等を記録します。
- 機密変数の保存valueと公開projectionは別経路にします。公開projectionは値をredactし、例外文・Schema errorにも入力値を埋め込みません。
- 既存のCodexBridge tokenをWorkflow変数へ自動注入しません。
- strict v1の初期実装では、sensitivity=secretの変数宣言とsecretを含むと明示された入力を受け付けず、明示的に拒否します。CodexBridge tokenを自動注入しません。secret保存・暗号鍵管理は将来の別設計です。
- 公開projectionは入力実値、秘密値、prompt等をGraph/MCP/UI/通常ログや例外へ漏らさず、値をredactします。
- 現行のHTTP Workflow requestは128 KiB、YAMLは64 KiBです。Schema、contract、input/output/valueの合計、depth/node数、HTTP/MCP応答などの具体上限は実装開始前に承認が必要です。
- JSON Schema validatorで外部ネットワーク参照を行いません。

## 13. エラー分類案

HTTP/MCP status codeと公開error stringは未確定です。以下は内部分類と処理結果の設計案です。

| 条件 | 分類案 | run/resultへの扱い |
| --- | --- | --- |
| definition revisionがない | NOT_FOUND | 起動前エラー |
| YAML/contract digestまたはbindingが不一致 | REVISION_INTEGRITY_ERROR | 起動拒否、保存不整合 |
| Schema dialect/形が無効 | CONTRACT_INVALID | 起動前エラー |
| 未許可validation keyword、objectのadditionalProperties欠落 | CONTRACT_INVALID | compile時または保存時に拒否 |
| remote/recursive $ref または外部resource | CONTRACT_REFERENCE_FORBIDDEN | 起動前拒否、ネットワーク取得なし |
| defaultがproperty Schemaに不適合 | CONTRACT_INVALID | definition保存時に拒否 |
| required field missing、type/enum/items不一致 | INPUT_CONTRACT_INVALID | run起動前拒否 |
| 同一request idでnormalized input/identityが違う | REQUEST_ID_CONFLICT | 既存runを再利用せずconflict |
| legacy revisionにcontractなし | LEGACY_MODE | 従来の検証経路で続行 |
| strict updateでcontract省略、またはnullによる解除要求 | CONTRACT_REQUIRED | revision mutationを拒否 |
| declaration未定義、value missing、無効scope | VARIABLE_REFERENCE_INVALID | 該当step failure |
| bool加算、型不一致、有限数でない、RFC 6901 path不正 | VARIABLE_OPERATION_INVALID | mutationをcommitしない |
| stale before digest/version | VARIABLE_STATE_CONFLICT | retryで上書きせずconflict |
| execution_key再利用でpayload違い | MUTATION_ID_CONFLICT | needs_reconciliation相当 |
| checkpoint/ledger不一致 | MUTATION_RECONCILIATION_REQUIRED | 次nodeへ進まない |
| output Schema違反 | OUTPUT_CONTRACT_VIOLATION | runをFAILED、親へ返さない |
| strict v1でsecret変数またはsecret入力を宣言 | SECRET_VALUE_UNSUPPORTED | 明示拒否し、保存・実行しない |
| 設定上限超過 | RESOURCE_LIMIT_EXCEEDED | 実行前または更新前に拒否 |
| Codex外部送信が不確定 | 既存MAYBE_SENT / UNKNOWN | 変数mutation conflictと混同せず再送しない |

## 14. D1受入テストマトリクス

以下はD1を将来実装するときに必要な具体受入項目です。現在の設計文書作成ターンでのコードテスト実行を意味しません。

| ID | ケース | 期待結果 |
| --- | --- | --- |
| D1-01 | valid input/output Schemaと値 | 正規化・検証後にstrict run開始 |
| D1-02 | malformed Schema、異なるdialect、未許可validation keyword | check_schema/compile時に拒否し、silent ignoreしない |
| D1-03 | required property missingと明示null | missingとnullを別扱いし、null可否に従う |
| D1-04 | property defaultの再帰適用 | input deep copyのmissing propertyへ一度だけ適用し、nested/既存array内objectを扱う。root default・output defaultは適用しない |
| D1-05 | nested object、array items、enum、const、許可範囲制約 | valid/invalid値をSchema通りに判定 |
| D1-06 | 全objectのadditionalProperties | 省略は拒否。true/false/subschemaの明示値どおりに判定し、trueをfalseへ書き換えない |
| D1-07 | 同じrequest id、異なるraw表現だがnormalized value同じ | 同一identityなら同じrunへreplay |
| D1-08 | 同じrequest id、normalized valueまたはrevision bindingが異なる | conflictとして新規実行しない |
| D1-09 | 旧immutable revisionを再取得、後続revisionを追加 | 旧YAML/contract/hash/bindingが不変 |
| D1-10 | contract未指定legacy definition、legacy scalar output、旧request id replay | 既存検証・出力・digest/replay規則を維持 |
| D1-11 | Workflow / Issue / Attempt相当の異なるloop frame | 値がframe間で分離し、frame exit後に暗黙参照できない |
| D1-12 | declaration_id/frame_identity参照、同名宣言 | 別scopeの同名は許可、同scopeの同名・重複IDは拒否 |
| D1-13 | initialize、assign、increment、decrement | 宣言Schemaと有限number/integerの厳密型を保つ |
| D1-14 | booleanの加算、missingへの暗黙加算、型不一致 | 更新せず明示エラー |
| D1-15 | append、RFC 6901 object_set/object_delete、variable_delete | 親object/pathを確認し、更新後Schema違反はrollback、deleteはtombstone |
| D1-16 | 同一append/incrementのstep replay、同一node複数操作 | mutation ordinalで識別し同一execution_keyを一度だけ反映 |
| D1-17 | SQLite transaction commit前のcrash | ledgerと値が共に未反映、replayで一回反映 |
| D1-18 | SQLite commit後・DBOS checkpoint前のcrash | replayで保存済みafter valueを読み、二重加算/追加しない |
| D1-19 | checkpointとledgerの両方がある回復 | key/payload/before/after digestを照合して続行 |
| D1-20 | 同一execution_keyで異なるpayload | overwriteせずconflict、needs_reconciliation相当 |
| D1-21 | before digest/versionが古い | mutationせず状態conflict |
| D1-22 | 子Workflow return境界の契約模擬 | child outputをSchema検証し、明示returnのみ親へ渡す |
| D1-23 | wrong output contract | runはFAILED、成功JSONとして返さず親にも返さない |
| D1-24 | secret variable/input、token、公開projection | strict v1はsecretを明示拒否し、tokenを注入せず、機密値を通常log/Graph/MCP/UI/例外に出さない |
| D1-25 | external/recursive $ref、$dynamicRef | network accessなしで拒否。非循環 #/$defs/<name> だけを許可 |
| D1-26 | body、YAML、Schema depth/complexity、value size上限 | 実装前承認した上限で境界判定し、既存128 KiB/64 KiBと整合 |
| D1-27 | OWS set/input/output/export、旧revision/manifest v1、既存jump regression | 既存動作と実行結果を維持 |
| D1-28 | definition revision、contract row、mutation request ledgerのcreate/update | 3者の保存を同一SQLite write transactionでcommitまたはrollback |
| D1-29 | contract_sha256とbinding_digest | canonical JSONと指定identity要素から決定的に再計算 |
| D1-30 | legacy create/updateからstrict化、strict update省略/null、strictからlegacy | 明示contractのないstrict mutationを拒否し、legacy request id規則を維持 |
| D1-31 | strict input/output root scalar、legacy root scalar output | strict scalarは拒否し、legacy結果は従来どおり |
| D1-32 | output schemaにdefaultがある値 | outputを補完せず、そのままvalidation |
| D1-33 | manifest/request digest version選択 | legacy v1検証は不変、strict v2を別経路で照合、未知versionはfail closed |
| D1-34 | strict/legacy ETag | strict ETagはcontract identityを含み、legacy v1 ETag照合は維持 |
| D1-35 | Schema property default自体の型不一致 | definition保存時に拒否し、Schema/input原本をmutationしない |
| D1-36 | local $defs参照と循環参照 | 存在する非循環参照のみ許可し、cycleは拒否 |
| D1-37 | pattern/oneOf等の初期非対応keyword | strict v1ではcompile時に拒否 |
| D1-38 | OWS do/for scopeと変数frameのadapter | 既存scope/OWS contextと衝突せず、prompt/jq記法を変更しない |
| D1-39 | object_setの新規末尾key、欠けた親、required delete | 既存親だけ許し、Schema違反deleteはrollback |
| D1-40 | 異なるexecution_keyで同じ値をappend | 同値追加を許し、unique-by/dedupを行わない |
| D1-41 | strict sensitivity=secret宣言とsecret入力 | 明示拒否し、secret保管・実行へ進まない |

D1実装時の検証計画にはpytest、Ruff、mypy、compileall、および隔離したWindows smokeを含めます。実装時に既存CI・適用可能なコマンドを再確認します。この文書のみの変更ではこれらを実行しません。

## 15. D1出口条件

D1実装の出口では、一つの保存済みWorkflowについて、revision固定contract検証、入力正規化、変数初期化・代入・数値/配列/object変更・削除、output validation、明示exportがDBOS回復下でも再現できる必要があります。same execution_key replayでmutationを二重適用せず、競合時は上書きせず状態を照合可能にします。

この出口条件はD2の汎用Agent nodeや動的prompt、D5のRepeat-Until実装、D6の実subflow起動、D7の複数Issue運用、D8のGUI編集をD1へ持ち込みません。既存OWS動作・旧revision・Codex MAYBE_SENT再送規則は維持します。

## 16. Proposed decisions（レビュー対象）

以下はD1詳細設計第1回の確定候補です。基本仕様 v0.2を変更せず、ユーザーの設計レビュー後に確定します。各項目は旧Open questions 1〜8、13への回答候補を具体化しています。

| ID | Proposed decision | 対応する論点 |
| --- | --- | --- |
| PD-01 | OWS YAML/schemaを変更せず、contract_version 1の論理JSONを同一SQLite DBのworkflow_definition_contractsへrevision従属で保存する。canonical contract hashとYAML source/canonical hashを保持し、同一write transactionでrevision・contract row・mutation ledgerをcommitする。 | 旧Q1 |
| PD-02 | binding_digestはdefinition_id、revision、YAML source/canonical SHA、max_live_codex_jobs、contract version/hashを結ぶ。strict ETagはcontractを含め、contract変更は新revisionとする。 | 旧Q1/Q2 |
| PD-03 | legacy mutationはcontract非指定の従来意味を維持する。legacyからstrictは明示contract付き新revision、strict updateは省略/nullを拒否し、strictからlegacyへの暗黙downgradeを許さない。mutation digestはcontract有無・値・revisionを含める。 | 旧Q2 |
| PD-04 | strict v1のinput/output rootはobject。許可keywordを明示し、未許可validation keywordはcompile時に拒否する。$refは同一Schema内の非循環 #/$defs/<name> のみとし、$dynamicRefや外部参照等を拒否する。 | 旧Q3/Q6 |
| PD-05 | strict v1の全objectでadditionalPropertiesを必須明記し、暗黙trueをfalseへ改変しない。input properties defaultだけをdeep copyへ一度適用し、nested/existing-array objectを正規化する。output defaultとroot default補完は行わない。 | 旧Q4/Q5 |
| PD-06 | 変数宣言はdeclaration_id、name、owner_scope、value_schema、optional initial、sensitivityを持つ。read/writeはdeclaration_idとframe_identityを明示し、同scope重複は拒否、別scope同名は許す。 | 旧Q7 |
| PD-07 | initialize、assign、increment、decrement、append、object_set、object_delete、variable_deleteを論理操作として定義する。object pathはRFC 6901、schema違反deleteはrollback、同じ値の別key appendは許容しunique-byは対象外。 | 旧Q8 |
| PD-08 | mutation execution_keyにはrun/revision binding、stable node path、全loop iteration vector、scope/frame、将来のsubflow call context、node内mutation ordinalを含める。SQLite mutation ledgerでDBOS checkpointとのreplay境界をつなぎ、MAYBE_SENTは流用しない。 | 旧Q8 |
| PD-09 | strict入力は一度正規化してからvalidation・実行・idempotency digestに使用する。strict v2にbinding digestとnormalized input hashを加え、過去manifest/request digestとlegacy v1検証を再解釈しない。 | 旧Q13 |
| PD-10 | strict v1初期実装はsecret変数とsecret入力を明示拒否する。token自動注入やplaintext secret保存を行わず、secret storage/key管理は別設計とする。 | 機密性候補 |

## 17. Open questions

次は未確定事項として残し、実装前に決定・承認します。項目番号は前版Open questionsの参照番号を維持しています。

9. variable stateのDB schema/migration、stateをrun-directory DBまたはstate-root shared DBのどちらへ置くか、SQLite transaction接続、DBOS checkpointからledgerを照合する具体API。既存manifest v1とstrict v2を識別する具体fieldもここで決めます。
10. 将来secretを扱う場合の暗号化、key ownership/rotation、およびstrict v1拒否を公開するerror mapping。secret機能を初期実装へ含めません。
11. Workflow/input/output/value合計、深度、Schema節点数、参照解決量、HTTP/MCP応答・eventの数値上限。現行128 KiB API request、64 KiB YAML等と整合させ、実装開始前に明示承認を得ます。
12. HTTP/MCPの公開route、field、同一endpoint optional fieldか別APIか、strict ETagのwire表現と既存clientへの移行、Console投影、public error code。
14. output違反の公開status/event名と、外部副作用発生後のレビュー・再実行手順。

上記のOpen questionsはこのDRAFTと実装対象を区別するために残します。解決前に未決定の値やAPIを確定仕様として扱いません。
