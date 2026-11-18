# D1: Workflow共通入出力契約・変数・スコープ

- **文書状態:** DRAFT / Proposed（レビュー待ち）
- **基本仕様:** v0.2は合意済み。本書のProposed decisionsは詳細設計候補であり、設計承認・実装完了を意味しません。
- **関連:** [README](../README.md)、[要件](../requirements.md)、[アーキテクチャ](../architecture.md)、[ADR 0001](../decisions/0001-structured-loops.md)、[ADR 0002](../decisions/0002-contract-storage-and-normalization.md)、[ADR 0003](../decisions/0003-run-local-variable-store-and-manifest-v2.md)、[ADR 0004](../decisions/0004-sqlite-ddl-and-schema-migration.md)、[ADR 0005](../decisions/0005-startup-exclusion-resource-limits-windows-acl.md)

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

**Proposed migration:** 現行definitions.sqliteはPRAGMA user_version=1で、現行schema checkerは既存3テーブルの完全一致を検査します。strict contract用table追加には単一SQLite write transactionによるv1→v2 migrationを用い、既存revision row、primary key、YAML hash、legacy ETagを保持します。v2でworkflow_definition_contractsを追加し、PRAGMA user_version=2とv2 schemaを検査します。既存legacy revisionにcontract用NULL rowは作らず、行を欠落させます。既存workflow_definition_revisionsへcontract列を後付けしません。新規definition DBはv2 schemaを一括作成します。

migration途中のクラッシュはtransaction rollbackか全完了のどちらかとし、中間schemaを受け入れません。起動時schema不一致はfail closedです。運用案はwriter停止、SQLite online backupとその検証、upgrade、対応版restart、schema・既存revision・legacy ETag検証の順です。WAL使用時に主DBファイルだけをコピーする方法はbackupに使いません。v2へupgradeしたDBと旧プロセスの同時運用はサポートしません。SQL案、schema validator、upgrade/rollback gateは[ADR 0004](../decisions/0004-sqlite-ddl-and-schema-migration.md)に記載します。

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

既存のsaved Workflow startは選択revision、client request id、workspace、input_dataを使います。D1で別のmain/sub呼び出し口を作りません。現行のHTTP start request上限は128 KiB、OWS YAML上限は64 KiBです。strict runはmanifestのrun_format_version=2とrequests.sqliteのstrict binding rowを相互検証し、binding_digestとnormalized input hashを持つversioned request-digest経路を使います。run_format_versionがないmanifestはlegacy v1として扱い、legacy digestの計算と検証は変更しません。契約追加後の総量・保存・応答上限は後続設計に残します。

strict v2 request_digestは名前空間付きcanonical JSON（codexbridge-workflow-request-v2）から一度だけ算出します。Workflow canonical content、workspace、max_live_codex_jobs、definition ID/revision、YAML source SHA、contract SHA、binding digest、normalized input SHA、および他の実行identityを含め、requests ledgerとmanifest検証で同一規則を使います。具体的なcanonical payload field namesは後続設計事項です。

strict manifestの追加field案はrun_format_version=2、definition_id、definition_revision、contract_version=1、contract_sha256、binding_digest、normalized_input_sha256、variable_store.filename=variables.sqlite、variable_store.schema_version=1です。secret、variable value、promptはmanifestに保存しません。request binding rowの案はworkflow_request_contract_bindingsをtable名とし、run_idをkeyにworkflow_id、definition_id、definition_revision、run_format_version、contract_version、contract_sha256、binding_digest、normalized_input_sha256、request_digestを保持します。いずれも既存legacy manifest/request rowを遡及変更しません。

現行requests.sqliteにはPRAGMA user_versionによる版管理がありません。提案schema v1への移行ではuser_version=0の既存DBを、workflow_requests、workflow_request_sources、既存index、columns、foreign keys、row整合性について検査してから新binding tableを追加します。既存row/indexは維持し、legacy rowにはbindingを作りません。未知table/indexがあるuser_version=0 DBをblind migrationしません。旧binaryはrequests.sqliteのuser_versionを検査しないため、version値だけで旧writer混在を防げません。対応reader/worker/managerへの更新と旧process停止・起動排他の具体的なguardが必要です。詳細は[ADR 0004](../decisions/0004-sqlite-ddl-and-schema-migration.md)のProposed案を参照します。

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

### 8.1 run-local変数DBと概念的な永続モデル

**Proposed:** 各Workflow run directoryに独立したvariables.sqliteを新規作成します。配置例は $LOCALAPPDATA/CodexBridge/workflows/run-<runid>/variables.sqlite です。definitions.sqlite、requests.sqlite、run-localのdbos.sqlite、node-event DBの既存tableへ変数値を混在させません。variables.sqliteの専用schemaはVariableState、ScopeFrame、VariableMutation、RunBindingを持ちます。物理table名はrun_binding、scope_frames、variable_states、variable_mutationsで、前掲の大文字名は論理model名です。完全なDDL例とindexは[ADR 0004](../decisions/0004-sqlite-ddl-and-schema-migration.md)に記載します。

DBはPRAGMA user_version=1、想定journal_mode=WAL、synchronous=FULL、busy_timeout設定、foreign_keys=ON、write時BEGIN IMMEDIATEを候補とし、接続時に4 table・index・columns・primary/foreign keys・user_versionを厳格に検査します。PRAGMA integrity_checkとforeign_key_checkを起動時検査案に含めます。JSON canonicalization/hash/schema検証はapplicationで行い、SQLite JSON1に依存しません。読取専用inspectionはmode=ro接続とし、DBを新規作成・初期化しません。run_dirのcanonical path、run ID、workflow identityとRunBindingの所有関係を毎回照合し、symlink、junction、その他reparse pointを拒否します。Windowsでの安全なファイル作成とACL設定はOpen question 11に残します。明示的なDB容量上限も未決です。

Windows native strict runの新規run directory、variables.sqliteとSQLite WAL/SHM sidecarにはprotected explicit NTFS DACL、owner SID、ACL検証を提案します。既存state_root/legacy runのACLを自動変更せず、不適切なACLや保護を検証できない環境ではstrict runを開始しません。mode=0o700/os.chmodのみをWindows ACL保護とは見なしません。詳細と制約は[ADR 0005](../decisions/0005-startup-exclusion-resource-limits-windows-acl.md)に記します。

variables.sqlite transactionとDBOS step/checkpointは別transactionです。run directory上の新規DBに初期状態を準備し、requests.sqliteのrequest reservationとbinding rowを別のSQLite transactionで確定するため、両者をまたぐatomic commitはありません。

| レコード | 必須の論理情報 |
| --- | --- |
| VariableState | run_id、scope_frame_id、variable_name、type、value、version、value_digest、機密区分 |
| ScopeFrame | run_id、frame_id、parent_frame_id、stable_scope_path、iteration vector、call context、frame status |
| VariableMutation | execution_key、payload_digest、before_digest、after_digest、before/after version、commit outcome |
| RunBinding | run_id、workflow/definition identity、revision、contract version/hash、binding digest、normalized input digest、owner、store schema version、初期化状態 |

ユーザーの未追跡ファイルやOWS汎用contextへvariable stateを混ぜません。strict v1ではsensitivity=secretの宣言を持つWorkflowと、secret値を含むと明示分類されたinputを受け付けず、明示拒否します。secret valueの保存方式は将来の別設計です。

VariableState、ScopeFrame、VariableMutation、RunBindingはrun-local variables.sqliteで管理します。各mutationのstate更新と台帳記録は同一transactionにします。run directoryはrunごとに所有され、別run間で変数DBを共有しません。DDL、SQLite CHECK/UNIQUE/FK/indexとapplication validatorの境界はADR 0004のProposed案に従います。

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
3. 既存行がありpayload_digestも同じなら、そのledger行に記録済みのafter value/digest/versionを、そのmutationの結果として返します。ledger payload/hash/結果の自己整合、version chain、RunBinding、DBOS checkpointとの対応を検証します。後続mutationがcommit済みならVariableStateは歴史的after valueから先へ進んでいて正当なため、現在のlatest stateと過去mutationのafter digestが同一であることを要求しません。
4. 既存行が同じkeyで別payloadなら、runをNEEDS_RECONCILIATION相当の状態にし、再適用せずconflictを返します。
5. 初回mutationに限りbefore version/digestを現在値とCAS照合し、operation、型、scope、Schemaを検証します。
6. 新しい値・version/digestとmutation ledgerを同じtransactionでcommitします。どちらかだけをcommitしません。
7. DBOS stepのcheckpointにはexecution_keyとpayload/before/after digestを含めます。workflow側はcheckpointから復帰した後も対応ledgerとの一致を確認してから次のnodeへ進みます。過去mutationのreplayでlatest VariableStateを古いafter valueへ戻したり、更新を再加算・再appendしたりしません。

### 8.4 Crash/replay境界

DBOS checkpointと変数SQLite transactionを一つの物理transactionにできるとは仮定しません。execution_key ledgerがその境界をつなぎます。

| クラッシュ位置 | 永続状態 | Replay時の処理案 |
| --- | --- | --- |
| transaction開始前 | ledgerも値も未変更 | 同じstepが初回mutationとして実行 |
| transaction中、commit前 | transaction rollbackでledger・値とも未変更 | 同じkeyで初回実行 |
| SQLite commit後、DBOS checkpoint前 | ledgerと値はcommit済み、checkpointは未完了 | step再実行は同一key・payloadを読み、after stateを返して二重更新しない |
| 後続mutationがcommit済みの後、過去stepを再生 | ledger chainとlatest stateは後続versionまで進行 | 過去ledgerに記録した結果を返し、latest stateとの同値を要求せず、巻戻し・再適用しない |
| DBOS checkpoint後 | checkpointとledgerがある | checkpoint identity/digestとledgerを照合して続行 |
| 同一keyでpayload違い | 既存ledgerと再生要求が不一致 | conflict、値を上書きせずneeds_reconciliationへ |
| checkpointとledgerの片方欠落・digest不一致、chain矛盾 | durable記録間に矛盾 | 自動修復や再計算をせずneeds_reconciliationへ |

既存Codex MAYBE_SENTは外部送信の不確実性です。変数更新がcommit済みかの確認にMAYBE_SENTを流用しません。

## 9. 実行処理順序

strict contractの一回のsaved Workflow runは、次の順序を通る案です。

1. 指定されたimmutable definition revisionを読み、OWS YAML、revision従属contract row、各digestとbindingを検証する。
2. contract有無を確認する。ない場合はlegacy modeへ移り、従来検証を保つ。ある場合のみstrict経路へ進む。
3. Schema dialect、許可キーワード、内部参照、declaration一意性、default自身、scope宣言を検証する。
4. raw inputに正規化を一度適用する。missing/nullを保ち、採用されたdefaultだけを追加する。
5. normalized inputをinput Schemaで検証する。invalidならWorkflowを起動しない。
6. strict v2用request digestを作り、run directoryとimmutable YAML/input/manifest snapshotを準備します。manifestはrun_format_version=2とcontract/revision/binding/normalized input identityを持ち、input.jsonは正規化済みinputです。秘匿値はmanifestに記録しません。
7. run-local variables.sqliteを作成し、schemaとownerを検査したうえでRunBindingのstatus=INITIALIZEDを記録します。
8. requests.sqliteのrequest rowとworkflow_request_contract_bindings rowを同一request DB transactionで予約します。strict reservationが確定する前にworkerへ実行を渡しません。
9. worker開始時およびrecover時にrequest binding、manifest、immutable YAML/input、contract revision/hash、variables.sqlite schema/owner/RunBinding、mutation ledgerとDBOS checkpointを照合してから再開します。
10. 各mutationをDBOS named stepとSQLite mutation ledgerで実行します。
11. Workflow終了時に明示return/exportのJSON候補を作り、output Schemaで検証します。validなら既存JSON result shapeに沿って結果を確定し、不正ならFAILEDとして扱い親へ戻しません。
12. Graph/MCP/UI/通常ログは許可済みメタ情報だけの公開projectionを作り、入力・prompt・variable・outputの実値を含めません。

run directory準備後、requests.sqlite予約前にcrashするとorphan directoryが残る可能性があります。orphanをWorkflowとして自動復活させず監査対象とします。cleanupはrequest reservationや他の実行が存在しないことを確認した後に限り候補とし、その確認前に自動削除しません。strict request予約後にvariables.sqliteが欠落・破損していればneeds_reconciliationで停止し、再作成・worker実行・外部job再送を行いません。request bindingがある場合はrecover経路でもv2を強制します。manifest/binding不一致、未知version、owner/schema/ledger不整合はUNKNOWNまたはNEEDS_RECONCILIATIONとして止め、成功・受入成功と返しません。DB間atomic commitは仮定しません。

## 10. 公開・内部API契約案

以下はD1レビュー用の論理契約で、現行public endpointの変更宣言ではありません。

| 契約 | 入力 | 成功結果 | 失敗の扱い |
| --- | --- | --- | --- |
| revision contract load | definition_id、revision | YAML/contract/binding identity | revision欠落はNOT_FOUND、hash不整合は保存不整合 |
| input normalize/validate | 固定contract、raw JSON | normalized JSON、normalization digest | Schema不正・必須欠落・型不一致は実行前拒否 |
| strict run reserve | request row、run_format_version=2、contract binding | reserved run identity | requests DB内で両rowを同一transactionにし、不整合はreconciliation |
| strict run recover | request binding、manifest、definition/contract、variables store | 検証済み再開位置 | 欠落・破損・version/hash/owner不一致なら停止し、storeを再作成しない |
| variable read | run/frame selector、declaration selector | typed JSON value、version/digest | missing/ambiguous/invalid scopeは明示エラー |
| variable mutate | execution_key、payload、expected before identity | commit済みvalue/version/digest | stale before、同key異payloadはconflict |
| child input boundary | fixed child revision、explicit argument JSON | child用normalized input | D6で実装。D1では契約のみ |
| output validate/return | fixed contract、candidate JSON | 検証済みJSON result | contract違反はFAILED、親へ返さない |

Graph、MCP、Consoleの公開値はrun status、frame/node identity、宣言された型、version/digest等を必要範囲に限定します。値の取得・編集UIや各APIの具体フィールドは後続設計です。

## 11. バリデーションと互換性

### 11.1 Schema・binding検証

- Draft 2020-12以外のdialectはstrict contractで受け付けません。
- malformed Schema、未許可のvalidation keyword、required additionalPropertiesの欠落、remote/recursive $ref、digest mismatch、revision不一致を実行前に検出します。
- definitions.sqlite v1→v2 migration後はuser_versionとschemaを厳格検査します。requests.sqliteのbinding rowとmanifest run_format_versionを相互確認し、片側の欠落・改変をlegacy v1として読み替えません。
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
- strict runは全read/recover経路でrequest binding・manifest v2・RunBinding・store schema/ownerを検証します。未対応readerはstrict runを作成・再開できません。
- 過去execution_keyのreplayはそのledger記録の結果を返し、latest VariableStateとの同一性を要求しません。version chainやcheckpointとの矛盾はneeds_reconciliationにします。

## 12. 機密性、公開projection、リソース上限

- prompt、input、変数値、output値の秘匿対象は通常ログ、Graph、MCP、UIに出しません。通常のexecution eventは実値ではなくstatus、stable identity、必要なdigest等を記録します。
- 機密変数の保存valueと公開projectionは別経路にします。公開projectionは値をredactし、例外文・Schema errorにも入力値を埋め込みません。
- 既存のCodexBridge tokenをWorkflow変数へ自動注入しません。
- strict v1の初期実装では、sensitivity=secretの変数宣言とsecretを含むと明示された入力を受け付けず、明示的に拒否します。CodexBridge tokenを自動注入しません。secret保存・暗号鍵管理は将来の別設計です。
- 公開projectionは入力実値、秘密値、prompt等をGraph/MCP/UI/通常ログや例外へ漏らさず、値をredactします。
- Graph/MCPのv2 projectionは許可されたidentity・status・type・version等のメタ情報に限定し、変数の実値は公開しません。
- 既存実装のlimitは変更しません。strict v1には以下の追加Proposed初期値を設け、重なる境界にはmin(existing_limit, strict_limit)を適用します。すべてレビュー前の提案値で、基本仕様の確定値ではありません。

| 対象 | 現行上限（維持） |
| --- | ---: |
| HTTP API request body全体 | 128 KiB |
| Workflow YAML/source | 64 KiB |
| Codex job prompt | 16 KiB UTF-8 |
| Workflow result | 64 KiB |
| run input file | 2 MiB |
| active Workflow | 同時2件 |
| Workflow task数 | 10,000 |

| strict v1 resource | Proposed初期上限 | 検査時点 |
| --- | ---: | --- |
| canonical contract JSON | 64 KiB UTF-8 | definition compile/save前 |
| normalized input JSON / input.json | 96 KiB | default適用・正規化後、run admission |
| output JSON | 64 KiB | result確定前 |
| 単一variable value / mutation after value | 64 KiB UTF-8 | initialize/各mutation前後 |
| 単一mutation payload | 64 KiB UTF-8 | mutation admission |
| run内の現存variable値合計 | 2 MiB UTF-8 | BEGIN IMMEDIATE内、mutation前後 |
| variable declarations | 256 / run | contract compile |
| scope frames | 1,024 / run | frame作成前 |
| mutation ledger rows | 10,000 / run | 新規ledger insert前 |
| variables.sqlite main DB | 64 MiB | write admission/checkpoint管理 |
| variables.sqlite WAL | 16 MiB high-water目標 | checkpoint監視・writer抑制 |
| JSON Schema nesting depth | 24 | contract compile |
| Schema nodes合計 | 512 | contract compile |
| local $ref展開深度 | 16 | contract compile |
| Schema object property数合計 | 512 | contract compile |
| input/output/variable JSON depth | 32 | admission・mutation・output |
| JSON object property数 | 1,024 / object | admission・mutation・output |
| JSON array length | 10,000 / array | admission・mutation・output |

Workflow API body全体はcontract fieldを含めて既存128 KiB以内にします。YAMLやcontract等を個別上限内に収めても、body全体の上限は別に確認します。normalized inputはraw body/inputの大きさにかかわらずdefault適用後にも96 KiBで検査します。既存2 MiB input file制限、64 KiB result、64 KiB YAML等は引き続き適用され、該当するstrict capと小さい方を採ります。prompt 16 KiB、active Workflow 2件、task 10,000の現行制限を広げません。

JSON byte capはUTF-8 canonical encoding（ensure_ascii=false、sort_keys=true、separators=(',', ':'), NaN不許可）を基準にします。duplicate JSON keys、NaN/Infinity、許可外Schema keyword、cyclic refを拒否します。Schema/resource capsはdefinition compile、input raw bodyとnormalized valueはrun admission、variable値/payload/合計/ledger/databaseはmutation transaction内、outputは成功確定前に検査します。WAL 16 MiBは高水位目標で、上限前checkpointとwriter抑制を行います。長時間reader等によりcheckpoint不能な場合のSHM/WAL物理上限と最終保証は未確定です。logical quota、SQLite max_page_count、checkpoint/backpressureの組合せは実装時に検証します。

mutationが上限超過する場合はBEGIN IMMEDIATE内で適用前に検出し、変更なしrollbackとcapacity errorにします。既存state/ledgerを消さず、Workflowを正常完了・受入PASSにしません。read-only inspectorは全履歴を一括返却せずpagination/response capを用います。MCP/HTTP応答の具体数値はOpen question 12に残します。

- 数値はstrict v1のProposed初期値であり、レビュー後は後続revisionで変更可能です。これらはD2/D5のCodex job時間予算と別のdata/resource budgetです。
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
| v2 manifest/request binding不在・不一致・未知version | RUN_BINDING_RECONCILIATION_REQUIRED | UNKNOWN/NEEDS_RECONCILIATIONで停止し、実行・成功扱いしない |
| 予約済みstrict runのvariables.sqlite欠落・破損・owner不一致 | VARIABLE_STORE_RECONCILIATION_REQUIRED | DBを再作成せずworkerを起動しない |
| variables.sqliteが未知user_version/schemaまたはrun directoryがreparse point | VARIABLE_STORE_INVALID | 初期化せずfail closed |
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
| D1-42 | definitions.sqlite v1→v2 migration | 既存revision row、PK、hash、legacy ETagを維持し、user_versionとschemaをv2として検証 |
| D1-43 | 定義migrationのwrite transaction中にcrash | 全rollbackまたは全commitとなり、中間table/schemaを残さない |
| D1-44 | legacy revisionとstrict revisionのcontract row | legacyは行なし、strictのみ親revision FK付きworkflow_definition_contracts rowを持つ |
| D1-45 | v2 definitions.sqliteを旧プロセスが開く | user_version/schema不一致を検出してfail closedし、既存データを変更しない |
| D1-46 | strict request rowとworkflow_request_contract_bindings | requests.sqlite内の同一transactionで両方保存し、legacy requestにbinding rowを作らない |
| D1-47 | strict manifest v2とinput snapshot | 必須identity fieldとvariables.sqlite filename/schema versionを持ち、秘匿実値を含めず、input.jsonはnormalized input |
| D1-48 | binding row/manifestのversion削除・変更・片側欠落 | downgradeとしてlegacy解釈せずneeds_reconciliation、workerを開始しない |
| D1-49 | strict v2 request digestの再計算 | namespaceと固定revision・binding・normalized input identityが一致し、legacy digestは不変 |
| D1-50 | request予約前のrun directory orphan | Workflowとして自動復活させず監査対象にし、reservation未作成を確認するまで自動削除しない |
| D1-51 | reservation済みstrict runでvariables.sqlite欠落・破損 | storeを再作成せずneeds_reconciliation、worker起動・Codex POSTなし |
| D1-52 | variables.sqliteのunknown user_version/schema、readonly inspection | schema不一致はfail closedし、readonly inspectionはDB作成・初期化をしない |
| D1-53 | run directoryがsymlink/junction/reparse point | identity/path検証で拒否し、外部pathを開かない |
| D1-54 | variables.sqlite接続とRunBinding owner検査 | run/workflow identity、foreign key、想定pragma、store schemaを照合してからread/write |
| D1-55 | SQLite mutation commit前・commit後checkpoint前のDBOS crash | rollback時は未反映、commit後はledger結果を返し、いずれも二重更新しない |
| D1-56 | 後続mutation後に過去stepを再replay | 過去ledgerのafter value/digest/versionを返し、latest stateとの誤conflict・巻戻しを起こさない |
| D1-57 | 同一execution_keyで異payload、version chain/ledger digest矛盾 | stateを上書きせずneeds_reconciliationにし、自己整合性を検出 |
| D1-58 | replayされたappend/incrementとnode内複数mutation | mutation ordinalを含むkeyで結果を一度だけ反映し、append/加算を重複させない |
| D1-59 | strict runのworker entry/start/recover/task projection/manager reader | 全readerがv2 bindingとmanifestを相互検証し、未対応readerはstrict runを扱わない |
| D1-60 | legacy v1 request、manifest、digest、全reader | v1の既存照合と表示を維持し、strict v2へ再解釈しない |
| D1-61 | variables.sqlite/RunBindingの不一致とread-only inspector | UNKNOWN/NEEDS_RECONCILIATIONとして停止し、状態再作成や成功投影を行わない |
| D1-62 | Codex adapter MAYBE_SENTの既存回復 | 既存状態機械を維持し、v2 run reconciliationを理由に不確実なPOSTを再送しない |
| D1-63 | definitions migrationの運用手順 | writer停止、backup、v1→v2 upgrade、対応版restart、schemaとlegacyデータ検証の順で確認 |
| D1-64 | upgrade transaction中の失敗と事前backupからの復元 | migrationが全rollbackすることを確認し、退避backupを検証可能にする |
| D1-65 | strict v2使用後のdowngrade/rollback判断 | v2データを保持したまま旧schemaへ戻せると仮定せず、backup retentionとrollback方針の確定前に運用移行を完了扱いにしない |
| D1-66 | WAL中のdefinitions/requests backup | SQLite online backup APIで整合snapshotを取得・検証し、主DBファイル単純コピーを使わない |
| D1-67 | definitions v1事前schema/data gate | user_version=1、既存3 table/columns/index/FK/hash/revisionを厳格検査し、不明object・writer混在を拒否 |
| D1-68 | definitions v1→v2 DDLとversion更新 | 新contract table、columns/PK/FK/index、integrity/FK checkを検証しuser_version=2でcommit |
| D1-69 | definitions migrationの各中断点 | CREATE後・user_version更新後・commit前のcrashはいずれも全rollbackし中間状態を残さない |
| D1-70 | definitions既存revision/hash/legacy ETag保全 | migration前後のrowと既存identityを一致させ、legacy contract rowを作らない |
| D1-71 | contract rowとrevision/mutation ledger書込み | strict revision、contract row、mutation ledgerを同一definitions write transactionでcommit/rollback |
| D1-72 | strict ETag v2と既存legacy ETag | strict側だけcontract identityを含め、既存v1値と照合規則を維持 |
| D1-73 | requests user_version=0の既知旧schema migration | 既存workflow_requests/sources、既存index/rowを保持しbinding tableだけ追加してversion 1へ移行 |
| D1-74 | user_version=0 requests DBの未知table/index/shape | blind migrationせずfail closedして調査可能にする |
| D1-75 | 新規requests DBの初期化 | 対応版で全table/indexを一括作成してuser_version=1にし、IF NOT EXISTSのみの初期化にしない |
| D1-76 | request/source/bindingのstrict reservation | 三rowをrequests.sqliteの同一transactionで確定し、失敗時は全rollback |
| D1-77 | 旧binaryとrequests schema v1の混在 | user_versionだけに依存せず、instance/startup gateが旧writer混在を防ぐことを検証 |
| D1-78 | binding FKとrequest row/application identity | run_id FK、workflow/request digest/definition revision/source SHAを照合し、orphanやtamperを拒否 |
| D1-79 | variables.sqlite v1のphysical schema validator | 4 table、columns/PK/FK/index、user_version、integrity_checkとforeign_key_checkを検査 |
| D1-80 | scope_frames root/parent lifecycle | runごとのrootをpartial unique indexで一つに制限し、composite parent FKを確認。acyclic/path/status規則はapplicationで検査 |
| D1-81 | variable state missing/null/tombstone | initial missing row、JSON null string、present=0 tombstoneを区別し、CHECKとdigest validatorを検証 |
| D1-82 | variable mutation version chain | before/after unique key、before version 0のdigest NULL規則、tombstone after JSON NULL、同version競合を検証 |
| D1-83 | mutation CASと原子commit | 新keyは現state identityをCASし、variable_states更新とledger insertを同transactionでcommit/rollback |
| D1-84 | 後続mutation後の古いkey replay | 保存済みhistorical after resultを返し、latest stateを要求・上書き・巻戻ししない |
| D1-85 | JSON canonicalization/schema/digest検証 | application validatorで行い、SQLite JSON1が無効でもDDL・store読書きが成立 |
| D1-86 | readonly inspectionとstore path | mode=roでDBを作成せず、symlink/junction/reparse pointを拒否 |
| D1-87 | unknown user_version/schema/object | variables/definitions/requestsそれぞれschema mismatchでfail closedし、既存dataを変更しない |
| D1-88 | definitions/requests複数DB backup consistency | 全writer停止後に各SQLite online backupとrun metadata snapshotを取り、個別・組合せの復元確認を記録 |
| D1-89 | rollout時のstrict writer有効化 | 全reader version-aware、旧process停止、両DB migrationとlegacy identity検査後に限りstrict runを許可 |
| D1-90 | definitions成功後requests migration失敗 | strict runを止め、strict runが存在しないことを確認せず自動rollbackしない。安全なrestore条件を検査 |
| D1-91 | strict run開始後のrollback要求 | 旧binary/schemaへの自動downgradeを禁止し、forward repairまたは承認済みoffline restore/data-loss扱いへ分岐 |
| D1-92 | DDL smokeと外部副作用境界 | SQLite :memory:でDDL/制約/FKを確認し、docs taskでは実Codex POSTやpersistent user DB変更を行わない |
| D1-93 | 同一state_root/SIDの新対応controllerを別ログオンsessionから同時起動 | Global Mutexを共有し一方だけが所有し、他方は有限待機またはfail-closed。RDP/fast user switching等を含め、既存processはkillしない |
| D1-94 | Mutex取得の有限待機・timeout・abandoned状態 | WAIT_ABANDONED等は成功扱いせず、request/manifest/DB/worker状態を照合してから進行 |
| D1-95 | migration前/新process起動後の旧Bridgeと自動再起動 | GUI/autostart/service/scheduled/manual経路と未終了workerの不存在を確認するまでmigration/strict enable不可 |
| D1-96 | Mutexを無視する旧binary・未知writer・quiescence不明 | Mutex単体で排除したと見なさず、状態を証明できなければfail closed |
| D1-97 | state_root/user SID別のGlobal lock identity・named object squatting | 同root/SIDならsessionをまたいで同じGlobal Mutex、別root/SIDなら別identityを確認し、owner/DACL/object不一致時はstrict enableを拒否。Local fallbackなし |
| D1-98 | 通常GUI/API/MCP clientの複数接続 | client自身はMutexを取らず、単一controllerを共有して要求を処理 |
| D1-99 | controllerが起動したrun固有worker | workerはcontroller Mutexを取得せず、認証済みrun/worker identityを検証。live workerが残ればupgradeを拒否 |
| D1-100 | Windows NTFS run directory/variables.sqlite ACL | protected DACLのowner、現User SIDとLOCAL SYSTEMのみのACE、canonical ACLを検査 |
| D1-101 | Everyone/Users/Authenticated Users/別SIDへの広いgrant | strict start前に拒否し、値や秘密をerror/logへ出さない |
| D1-102 | 既存state_root/legacy runのACL不備 | 既存ACLを自動変更せずstrict runを停止。明示的管理者修復の範囲を案内 |
| D1-103 | state_root/ancestor/run/database/WAL/SHMのreparse point | どの対象pathでも検出したらSQLiteを開かず拒否 |
| D1-104 | run/database file name collisionとTOCTOU | create-exclusiveとhandle-based owner/file-ID照合で置換・path swapを拒否 |
| D1-105 | SQLiteが作るWAL/SHMのDACL/owner | 保護directoryから正しいACLを継承し、生成後に検証が完了するまでworkerを開始しない |
| D1-106 | NTFS以外、security descriptor/identity API不在・失敗 | POSIX/WSL/Dockerへ暗黙切替せずstrictをfail closed |
| D1-107 | Administrators/backup privilegeや同一user rogue process | 保護対象として遮断できると主張せず、Threat modelの限界と一致 |
| D1-108 | 現行API/YAML/prompt/result/file/concurrency/task上限の回帰 | 128/64/16/64 KiB、2 MiB、同時2件、10,000 taskを変更せずstrict側は追加制約を適用 |
| D1-109 | contract/YAML/API全body境界 | contract 64 KiB、YAML 64 KiBを個別検査し、合計API body 128 KiBも独立検査 |
| D1-110 | raw requestとdefault適用後normalized input | API body 128 KiBに加えnormalized JSON/input.json 96 KiBを両方検査 |
| D1-111 | output、単一variable value、mutation after value/payload | 各64 KiB境界の直前/一致/超過を検査し、既存result capも維持 |
| D1-112 | run aggregate/current value、declaration、frame、ledger count | 2 MiB、256、1,024、10,000の境界で挿入前検査 |
| D1-113 | Schema depth/node/ref/property caps | 24、512、16、512の境界でcompile時に拒否・許可を判定 |
| D1-114 | JSON depth/object property/array capsと不正JSON | 32、1,024/object、10,000/arrayの境界、duplicate key、NaN/Inf、cyclic refを検査 |
| D1-115 | variables.sqlite main DB/WAL/SHM物理サイズ | 64 MiB main、16 MiB WAL high-water、長時間reader/checkpoint不能とSHM上限の未確定を含め最終容量保証を検証 |
| D1-116 | definition/start/mutation/output/projection各段階のcap検査 | canonical UTF-8 byte数で検査し、normalization後や更新後の増加を見落とさない |
| D1-117 | mutation中のaggregate/DB/ledger上限超過 | BEGIN IMMEDIATE内で変更前にrollbackし、state/ledger保持、成功/acceptance扱いにしない |
| D1-118 | read-only inspectorの履歴/HTTP-MCP projection | pagination/response capを使い、一括全履歴と変数実値を返さない |
| D1-119 | strict cap errorとsecret扱い | valueをerror/logへ埋め込まずPD-10のsecret拒否を維持 |
| D1-120 | limit導入後の既存設定・外部job境界 | 既存prompt/time budgetを変更せず、このdocs taskでCodex POST/MCP dogfoodを行わない |
| D1-121 | contract wire object/version/field allowlist | contract_version=1とinput/output/variablesを検証し、null・unknown field・secretを拒否 |
| D1-122 | contractなしのlegacy create/update response | 旧HTTP/MCP body、header、v1 ETag、request mutation identityにstrict fieldを追加しない |
| D1-123 | strict validate success/error | contract_version/hashを返しbinding previewは返さず、不正contractは保存・revision追加なし |
| D1-124 | legacy revisionからstrict update | 明示contractが新immutable revisionとstrict v2 ETagへ結び付き、旧revisionは不変 |
| D1-125 | strict updateでcontract省略/null | HTTP/MCP workflow_contract_required、downgradeなし、repository mutationなし |
| D1-126 | HTTP/MCP mutation idempotencyとcontract/revision binding | If-Matchをimmutable revision/contract hash/bindingへ完全照合後に共通expected_revisionへ正規化し、MCP expected_revisionと同じdigestにする。raw ETag/transport名は除外。同mutation_request_id・同source/profile/contract/revisionはcross-transport replay、異revision/contractはconflict、legacy digestは1 bitも変更しない |
| D1-127 | strict ETag v2 digest | domain、definition/revision、YAML、max-jobs、contract/binding fieldsからstrong lowercase SHA-256を再計算 |
| D1-128 | HTTP If-Match / MCP expected_revision CAS | stale/cross-modeは既存precondition error、HTTP missing=428/malformed=400、MCPはexpected_revisionを維持 |
| D1-129 | legacy GET/list/graph/revision metadata | exact body/ETag/cursorをbaselineと比較し、contract-null key等を追加しない |
| D1-130 | strict detail/list/graph projection | detailのみcontract本体、summaryはcontract hash/binding metadataのみを含みcap内に収まる |
| D1-131 | ETag header/body consistency | strict detailのHTTP header/bodyが同じv2値、legacy v1の既存一致を維持 |
| D1-132 | HTTPとMCPのlegacy error compatibility | 既存transport固有error names/string/envelope/statusを相互renameしない |
| D1-133 | strict run modeとrevision binding | pinned revisionからserverがmode決定し、client指定contract/run_format_versionを拒否 |
| D1-134 | strict run input omission/null/default | omissionはobject defaultsを一度適用、explicit nullはinvalid、same normalized valueはsame digest |
| D1-135 | strict input invalid/異なるnormalized request | invalidはreservation前で停止し、同じclient_request_idの異normalized inputはrequest_id_conflict |
| D1-136 | strict/legacy run-start response | strict専用binding metadataだけ追加し、legacy fixed fields/body/statusをexact維持 |
| D1-137 | output contract違反と業務REJECTED | invalid outputはfailed/no result、Schema valid REJECTEDはsucceeded/result内に残す |
| D1-138 | uncertain send/commit/ledger回復 | needs_reconciliationとsafe reason_codeを返し、Workflow retry/Codex再POSTを行わない |
| D1-139 | strict HTTP error table | HTTP status/code/run_id/state/retry mappingとreservation前後境界を対応表どおりにする |
| D1-140 | strict MCP error envelope | 同じstrict codeをstructured {error,reason_code,workflow_id,run_id,status} allowlistで返しHTTP statusを混ぜない |
| D1-141 | strict reason_code/event redaction | input/prompt/variable/secret/tracebackを含めず、legacy event bodyを変えない |
| D1-142 | run/result/task public projection | bounded allowlist metadataのみを出しvariable valuesと全ledger historyを公開しない |
| D1-143 | MCP read/write/execute permission regression | definition read、mutation write、run start executeの既存区分を保ち権限昇格しない |
| D1-144 | HTTP/MCP request/response size caps | 128 KiB/1 MiB/256 KiBとstrict contract/input/output capsをcombined payload込みで検査 |
| D1-145 | legacy clientとstrict clientの併存 | unknown request fields拒否を維持し、旧clientのexact response/ETag/replayとstrict clientの追加fieldを両立 |
| D1-146 | 同時strict update race | 同じexpected revisionからの並行mutationは一つだけcommitし、他方は既存CAS conflictとなる |

D1実装時の検証計画にはpytest、Ruff、mypy、compileall、および隔離したWindows smokeを含めます。D1-42以降はmigration・run lifecycle・回復・security・resource・公開HTTP/MCP契約の受入項目です。この文書のみの変更ではコードテストを実行しません。

## 15. D1出口条件

D1実装の出口では、一つの保存済みWorkflowについて、revision固定contract検証、入力正規化、変数初期化・代入・数値/配列/object変更・削除、output validation、明示exportがDBOS回復下でも再現できる必要があります。same execution_key replayでmutationを二重適用せず、競合時は上書きせず状態を照合可能にします。

この出口条件はD2の汎用Agent nodeや動的prompt、D5のRepeat-Until実装、D6の実subflow起動、D7の複数Issue運用、D8のGUI編集をD1へ持ち込みません。既存OWS動作・旧revision・Codex MAYBE_SENT再送規則は維持します。

## 16. Proposed decisions（レビュー対象）

以下はD1詳細設計の提案（レビュー対象）です。基本仕様 v0.2を変更せず、ユーザーの設計レビュー後に確定します。PD-01..PD-23は従来候補を維持し、PD-24..PD-29は第4回のsingle-controller排他、Windows ACL/path、strict resource cap、PD-30以降は第5回の公開HTTP/MCP契約・ETag v2・error/status互換案です。DDL例は[ADR 0004](../decisions/0004-sqlite-ddl-and-schema-migration.md)、起動/ACL/cap設計は[ADR 0005](../decisions/0005-startup-exclusion-resource-limits-windows-acl.md)、公開契約は[ADR 0006](../decisions/0006-public-api-mcp-etag-error-contract.md)にあります。

| ID | Proposed decision | 対応する論点 |
| --- | --- | --- |
| PD-01 | OWS YAML/schemaを変更せず、contract_version 1の論理JSONを同一SQLite DBのworkflow_definition_contractsへrevision従属で保存する。canonical contract hashとYAML source/canonical hashを保持し、同一write transactionでrevision・contract row・mutation ledgerをcommitする。 | 旧Q1 |
| PD-02 | binding_digestはdefinition_id、revision、YAML source/canonical SHA、max_live_codex_jobs、contract version/hashを結ぶ。strict ETagはcontractを含め、contract変更は新revisionとする。 | 旧Q1/Q2 |
| PD-03 | legacy mutationはcontract非指定の従来意味を維持する。legacyからstrictは明示contract付き新revision、strict updateは省略/nullを拒否し、strictからlegacyへの暗黙downgradeを許さない。mutation digestはcontract有無・値・revisionを含める。 | 旧Q2 |
| PD-04 | strict v1のinput/output rootはobject。許可keywordを明示し、未許可validation keywordはcompile時に拒否する。$refは同一Schema内の非循環 #/$defs/<name> のみとし、$dynamicRefや外部参照等を拒否する。 | 旧Q3/Q6 |
| PD-05 | strict v1の全objectでadditionalPropertiesを必須明記し、暗黙trueをfalseへ改変しない。input properties defaultだけをdeep copyへ一度適用し、nested/existing-array objectを正規化する。output defaultとroot default補完は行わない。 | 旧Q4/Q5 |
| PD-06 | 変数宣言はdeclaration_id、name、owner_scope、value_schema、optional initial、sensitivityを持つ。read/writeはdeclaration_idとframe_identityを明示し、同scope重複は拒否、別scope同名は許す。 | 旧Q7 |
| PD-07 | initialize、assign、increment、decrement、append、object_set、object_delete、variable_deleteを論理操作として定義する。object pathはRFC 6901、schema違反deleteはrollback、同じ値の別key appendは許容しunique-byは対象外。 | 旧Q8 |
| PD-08 | mutation execution_keyにはrun/revision binding、stable node path、全loop iteration vector、scope/frame、将来のsubflow call context、node内mutation ordinalを含める。SQLite mutation ledgerでDBOS checkpointとのreplay境界をつなぎ、同じkeyの再実行は記録済みmutation結果を返す。過去mutationの結果とlatest VariableStateは一致しない場合があり、latest stateを巻き戻さない。MAYBE_SENTは流用しない。 | 旧Q8 |
| PD-09 | strict入力は一度正規化してからvalidation・実行・idempotency digestに使用する。strict v2にbinding digestとnormalized input hashを加え、過去manifest/request digestとlegacy v1検証を再解釈しない。 | 旧Q13 |
| PD-10 | strict v1初期実装はsecret変数とsecret入力を明示拒否する。token自動注入やplaintext secret保存を行わず、secret storage/key管理は別設計とする。 | 機密性候補 |
| PD-11 | 変数状態はrun directory内の独立variables.sqliteに保存し、VariableState/ScopeFrame/VariableMutation/RunBinding専用schemaとuser_version=1を使う。DBOS/requests/definitions/node-event DBへ混在させず、run/workflow ownerとrun directoryを検証する。 | 旧Q9の配置判断 |
| PD-12 | definitions.sqlite v1→v2を単一write transactionで移行し、既存revision/PK/hash/legacy ETagを保持してworkflow_definition_contractsを追加する。legacy revisionのcontract rowは欠落とし、user_version/schema検証後に対応版のみ運用する。 | 旧Q9のdefinition migration |
| PD-13 | strict requestだけrequests.sqlite内のworkflow_request_contract_bindingsへrun_format_version=2、revision/contract/binding/normalized-input/request digestを保存し、request rowと同一transactionで確定する。legacy requestはbinding rowなし。 | 旧Q9のrequest binding |
| PD-14 | strict manifestはrun_format_version=2とrevision/contract/binding/normalized-input/store identityを記録し、request binding rowと全readerで相互検証する。bindingとmanifestの欠落・downgrade・未知versionはfail closed。legacy v1 digest・reader挙動は不変。 | 旧Q9のmanifest識別 |
| PD-15 | strict作成順はimmutable revision/contractとnormalized input検証、v2 digest、snapshot準備、run-local store初期化、request+binding予約、worker owner検証とする。別DB/Filesystem間atomicityを仮定せず、orphanは自動復活させず監査し、reservation不存在を確認するまで削除しない。予約後のstore欠落・破損は再作成せずreconciliationで停止する。 | 旧Q9のlifecycle |
| PD-16 | 同一execution_keyのhistorical replayは台帳保存済みafter resultを返し、payload/hash/chain/RunBinding/checkpointを検証する。後続mutation後のlatest stateを過去after digestと一致要求せず、巻戻し・再適用をしない。 | 旧Q9のreplay詳細 |
| PD-17 | v2の不確定なbinding/store/recovery状態を成功・受入成功にしない。read-only inspectorはstoreを作成せず、Graph/MCPは許可済みmetadataのみ公開する。Codex MAYBE_SENT状態機械とsecret拒否方針は変更しない。 | 旧Q9のreader・公開境界 |
| PD-18 | definitions.sqlite v1→v2はwriter停止、online backup/検証、v1 exact-schema/data gate後に単一transactionでworkflow_definition_contractsを追加し、version/schema/rowsを照合してcommitする。WAL主DBファイル単純copyはbackupに使わない。 | 旧Q9 migration具体化 |
| PD-19 | requests.sqliteの既存user_version=0は既知legacy schema/objects/rowsを厳格検査してから単一transactionでbinding tableを追加しuser_version=1にする。未知objectsは拒否し、version-aware readerと旧binary排他gateはversion fieldだけに依存しない。 | 旧Q9 request migration |
| PD-20 | run-local variables.sqlite physical schemaはrun_binding、scope_frames、variable_states、variable_mutationsの4 tableとADR0004のCHECK/FK/partial-unique/unique/history indexesを使う。JSON値のcanonical/hash/schema検査はapplication側で行いJSON1に依存しない。 | 旧Q9 variable DDL |
| PD-21 | variable stateはmissing row、JSON null文字列、present=0 tombstoneを区別する。mutation ledgerはhistorical after valueを保持し、CAS、線形version chain、state更新とledger insertを一transactionで行う。 | 旧Q9 ledger DDL |
| PD-22 | 各DBはuser_version・object/table/column/index/PK/FK・integrity/FK checkを厳格検査する。起動時のschema mismatch/未知objectはfail closedし、readonly inspectionでDB作成しない。 | 旧Q9 schema validator |
| PD-23 | migration/rollbackは全writer停止、SQLite online backup、version-aware binary展開、各DB migration、legacy identity検証、reader対応後のstrict enableの順とする。strict run開始後は旧binary/schemaへの自動rollbackを行わずforward repairまたは承認済みoffline restoreを使う。 | 旧Q9 operational gate |
| PD-24 | 新対応controllerはDB open/migration前にstate_rootのcanonical absolute pathと現user SIDのSHA-256 identifierに結び付く Global 名前付きMutexを取得し、controller寿命中保持する。CreateMutexExWには明示DACL付きSECURITY_ATTRIBUTESを渡しdwFlags=0とする。mutexはthread-ownedのため取得owner threadを保持し、同じthreadだけがreleaseする。DACL/owner検証または取得失敗、Global作成不可ならstrict enableを止め、Localへfallbackしない。abandoned時は状態照合を必須とする。 | 旧Q9 controller exclusion |
| PD-25 | Mutexは旧binaryを止めない。全legacy起動経路とworkerの停止を確認し、互換owner/旧writer不存在が証明できない場合migration/strict enableしない。Mutexだけで旧binary混在を防ぐと主張しない。 | 旧Q9 old-version gate |
| PD-26 | workerはcontroller Mutexを取得しない。controller認証済みrun/worker起動identityとbindingを検査し、live workerが残る間はupgradeしない。通常GUI/API/MCP clientは共有controllerを使い、Mutexを個別取得しない。 | 旧Q9 worker lifecycle |
| PD-27 | strict D1の新規run directory/variables.sqlite/WAL/SHMはNTFS protected DACL、owner SID、canonical ACLを検査し、state_root・ancestor・run/database/sidecarのreparse pointを拒否する。create-exclusive/handle-based file-ID照合を行い、既存ACLを自動変更しない。 | 旧Q11 Windows boundary |
| PD-28 | 本節のresource tableをstrict v1のProposed初期値とし、既存limitを変えず重複制約にはmin(existing_limit, strict_limit)を適用する。API body、normalized input、schema/value/aggregate/store capsをそれぞれ明示検査する。 | 旧Q11 numerical limits |
| PD-29 | cap超過は変更前に検出し安全なcapacity errorで止め、既存state/ledgerを保持して成功/acceptanceとしない。WAL/SHMを含む物理上限保証とWindows ACL raceは実装dogfoodで検証する未決ゲート。 | 旧Q11 enforcement |
| PD-30 | 既存HTTP definition create/validate/updateとMCP definition create/validate/updateへoptional top-level contract objectを追加する。contract_version=1、input_schema、output_schema、variablesのみを受け、JSON null/unknown key/unsupported versionとstrict v1のsecretを拒否する。 | 第5回contract wire |
| PD-31 | contract absentのlegacy mutationは従来どおり。legacyからstrictは明示contract付き新revision、strict updateはcontract必須、missing/nullでworkflow_contract_required、strict-to-legacy downgrade禁止。同じcontractのupdateも新immutable revisionとする。Strict mutation digestはsource/profile/contract payload、共通expected_revision、definition mode/binding identityで計算し、raw If-Match/transport名を含めない。Legacy digestは1 bitも変更しない。 | 第5回CRUD互換 |
| PD-32 | HTTP POST validate/MCP workflow_definition_validateはoptional contractを受ける。legacy response key setを維持し、strict成功時はcontract_version/contract_sha256を追加するがrevision未確定のbinding_digest previewは返さない。Invalid Schema/contractは安全errorでwriteしない。 | 第5回validate |
| PD-33 | Legacy GET/list/graph responseとETagを変更せず、strict detailだけcontract本体とbinding hashを含める。strict list/graphはcontract_version/hash/binding_digest summaryだけを追加し、契約未指定時はnull keyを追加しない。 | 第5回read projection |
| PD-34 | Strict definitionはstrong ETag v2を使い、domain-separated canonical JSONにdefinition/revision/YAML/max-jobs/contract/binding identityを含める。HTTP If-Matchはimmutable revisionに解決してETag/contract hash/bindingを完全照合し、CAS前に共通expected_revisionへ正規化する。MCP expected_revisionも同じ表現へ正規化し、stale時は各transportの既存precondition errorを保つ。Legacy v1 ETag/digestは不変。ETag v2とrun_format_version=2は異なるversion domain。 | 第5回ETag |
| PD-35 | HTTP saved-run start/MCP workflow_run_startは既存required argsとoptional inputを保ち、pinned revisionからserverがlegacy/strictを選ぶ。callerはcontract/run_format_versionを選べない。strict inputはobject/null invalid、default後normalized inputをdigestし、同normalized default値は同じidempotency identity、異値は既存conflictとする。 | 第5回run start |
| PD-36 | Strict成功runは既存response fieldsにrun_format_version、contract_version/hash、binding_digestを追加し、legacy responseは不変。Output Schema検証前にsucceeded/resultを公開せず、invalid outputはfailed/output_contract_violation、業務REJECTEDはvalid result内に置く。 | 第5回result |
| PD-37 | Runtime status enumを維持し、strict-only safe reason_codeをstatus/detail/eventへ追加可能にする。UNKNOWNや外部効果/ledger不確定時はneeds_reconciliationを用い自動retry/POSTしない。error codeはtransport固有のlegacy namesを保ち、表のstrict additionsだけを追加する。 | 第5回status/error |
| PD-38 | D1 public projectionはvariable value/prompt/input/secret/tracebackを返さず、必要時もallowlist metadata/countだけとする。MCP read/write/execute permissionとHTTP/MCP response capsを維持し、variable inspector/editorとGUI詳細はD8へ残す。 | 第5回projection/auth |
## 17. Open questions

次は未確定事項として残し、実装前に決定・承認します。run-local DB、DDL/schema version、binding、作成・回復順序、historical replay、migration/rollback、controller排他、resource capはPD-01..29へ、公開route/tool、contract wire delta、ETag v2、run/result/error projectionの提案はPD-30..38へ移しました。以下には実装詳細または別途レビューが必要な部分だけを残し、項目番号は前版Open questionsの参照番号を維持します。

9. Global Mutex名のcanonicalization/hash input framingとWin32 API wrapper、owner threadの寿命・同一thread release、RDP/fast user switching/serviceをまたぐ排他の実証、controller起動/終了への組込み、旧GUI/autostart/service/scheduled/manual再起動経路を封じるinstance guard、live workerとcontroller認証済みrun起動identityの具体mechanism。Global作成/DACL/owner検証/取得に失敗した場合はstrict enableせずLocalへfallbackしません。requests.sqliteは現行でuser_version管理がなく、旧binaryは新Mutexを知らないため、version/Mutexだけでは旧writer排除になりません。quiescenceを証明できない場合はstrict enableしません。
10. 将来secretを扱う場合の暗号化、key ownership/rotation、およびstrict v1での拒否を公開するerror mapping。secret機能を初期実装へ含めません。
11. 表12のresource capはProposed初期値です。WAL/SHM高水位と長時間reader時のcheckpoint/backpressure、PRAGMA max_page_count/論理quotaとの組合せで64 MiB main DBを超えない保証、NTFS SECURITY_DESCRIPTOR/DACL/ACE継承/owner確認とhandle-based file-ID検証の実装・Windows dogfood測定は未決定です。ACL/API/identity検査不能時のstrict fail-closed境界を実装前に確認します。
12. 既存route/toolへのoptional contract追加、legacy responseの厳密維持、strict ETag v2、error/status対応、D8へ渡すprojection方針はPD-30..38に記録しました。shared DTO serializer、厳密なfield allowlist fixture、strict-only metadata追加時のresponse cap/paging実装、strict v2利用後のbackup retention/offline restore手順は未確定です。
14. Output violation=failed/output_contract_violation、実行・ledger不確実=needs_reconciliation/safe reason_codeという公開意味はPD-36..37に記録しました。内部causeから全reason_code/eventへの細かな写像、外部副作用発生後の補償・人手レビュー・再実行方針は未確定です。

上記のOpen questionsはこのDRAFTと実装対象を区別するために残します。解決前に未決定の値やAPIを確定仕様として扱いません。
