# Workflowアーキテクチャ: 現行実装とターゲット

## 状態

本書の「現行」は確認したソース上の実装を示します。「ターゲット」「案」は基本仕様 v0.2またはD1以降の設計方針で、未実装の内容を含みます。基本仕様は [requirements.md](requirements.md)、詳細案は [D1 DRAFT](phases/D1-input-output-variables.md)、ループ方針は [ADR 0001](decisions/0001-structured-loops.md)、contract保存は [ADR 0002](decisions/0002-contract-storage-and-normalization.md)、run-local変数DBとmanifest v2は [ADR 0003 Proposed](decisions/0003-run-local-variable-store-and-manifest-v2.md) を参照してください。

## 現行でできること

### OWS定義とinterpreter

- src/codex_bridge/workflows/schema/workflow.yaml はOWS v1.0.3のSchema IDとJSON Schema Draft 2020-12を宣言しています。現在の配布Schemaとpoc/ows/schema/workflow.yamlは同じ内容です。実行時はローカルSchemaを参照します。
- src/codex_bridge/workflows/interpreter.py の対応taskは do、set、switch、for、call です。一般的なWhileやRepeat-Until、Break/Continueはまだありません。
- データフローは現行の input_data、各taskのjq input/output/export、contextを使います。contextは単一のWorkflow context値です。set taskは型付き変数領域への代入ではなく、データフロー上の値を生成します。
- 現行directiveにはcontinue、exit、および同じdo scope内の名前付きジャンプがあります。ジャンプ先は同一scopeに限定され、ジャンプ循環は拒否されます。
- for は配列を反復してtask scopeを実行できますが、汎用のAttempt scopeや構造化Break/Continueではありません。
- codex-job liveはmax_live_codex_jobsの1/2/3設定ごとに固定の検証形を持ちます。3-job形では一つのtop-level forと既定の3 taskパターンなどを検証します。任意の汎用ループ中で任意数のCodex jobを実行する機能ではありません。

### workerと回復

- src/codex_bridge/workflows/worker.py はDBOS workflowとnamed DBOS stepで実行します。task stepのcheckpointはdefinition/input/context/variables等のdigestを照合します。
- Codex job adapterは送信・pollingの永続台帳とcheckpointを使います。送信が不確実なMAYBE_SENTやNEEDS_RECONCILIATIONを表現し、単純な再送と混同しません。
- このjob adapterのMAYBE_SENT処理は外部Codex jobの送信結果に関する既存規則です。D1の変数mutation ledgerやWorkflow全体のUNKNOWN判定とは別の状態機械として維持します。

### 定義・起動・操作

- definitions.py はOWS YAMLを検証し、definition revision、source SHA-256、canonical SHA-256を保存します。更新はexpected revisionを照合し、新しいrevisionとして追記します。
- service.py はclient request id、request digest、workflow runの予約と再送時の同一request確認を行います。同じrequest idで内容が異なるとconflictになります。起動履歴はsaved definition ID/revision/source hashに結び付きます。
- HTTP APIとMCPにはdefinitionのvalidate/create/update/get/list、saved revisionのrun start、run/list/detail/result/status、task graph/eventの操作があります。
- ConsoleにはYAMLからWorkflowを起動する画面、状態・結果・実行グラフの表示があります。GUIでtask graphを作成・編集するエディターや汎用Loopの操作面は現行機能ではありません。

## 現行コンポーネントの責務図

呼び出し元（Console / HTTP / MCP）<br>
→ Workflow API・MCP adapter<br>
→ WorkflowService（input/YAML検証、request idとrequest digest、run予約）<br>
→ DefinitionRepository（immutable OWS revisionとhash）<br>
→ DBOS worker（interpreter、task checkpoint）<br>
→ OWS task / Codex job adapter（Codex送信台帳・status・reconciliation）<br>
→ run result・status・graph/event projection

この図は現行の概略です。revision従属contract row、型付き変数store、構造化ループはまだ加わっていません。

## ターゲットの責務案

D1以降では、現行の入口・定義・worker境界を保ちながら、次の責務を段階的に加えます。

呼び出し元（GUI / MCP / API / Workflow）<br>
→ 共通requestとrevision解決<br>
→ definitions.sqlite（immutable YAML revision + revision従属contract row）<br>
→ 入力正規化・input/output Schema検証 + versioned request digest<br>
→ run directory（immutable YAML/input/manifest v2 snapshot + 独立variables.sqlite + 既存dbos.sqlite）<br>
→ requests.sqlite（request row + strict v2 contract bindingを同一transactionで予約）<br>
→ OWS interpreterと構造化scope frame<br>
→ DBOS step/checkpoint + variables.sqliteのmutation ledger<br>
→ 明示return/exportとoutput contract検証<br>
→ 既存JSON result形を維持したrun履歴・限定公開projection

run directory/variables.sqliteとrequests.sqliteは別の永続transaction境界です。run binding、manifest、DB schema/owner、mutation ledgerとcheckpointを照合して回復し、跨DB atomic commitを仮定しません。

Workflowをmain/subに分けず、同じ入口・contract検証を使います。別Workflow呼び出しは、呼び出し元と明示的な引数・戻り値境界の違いです。

## D1のOWS互換設計方針

- OWS v1.0.3の公式YAML Schemaを変更・拡張してCodexBridge固有contractを表すことはしません。
- D1 ProposedではCodexBridge側のcontract JSONを新規revision従属テーブル workflow_definition_contracts に保存し、WorkflowDefinitionRepositoryと同一SQLite DBを使います。OWS YAML top-levelと公式Schemaは変更せず、別ファイルsidecarも使いません。
- 現行definitions.sqliteはuser_version=1でtable名完全一致のschema検査を行うため、contract table追加にあたりv1→v2の単一transaction migrationを提案します。既存revision/hash/legacy ETagは保持し、legacy revisionにcontract rowを作りません。user_version=2を扱えない旧プロセスはfail closedする運用候補です。
- contract rowは親revisionを参照し、revision・contract row・mutation ledgerを一つのSQLite write transactionでcommitする候補です。contractのcanonical hashと既存YAML source/canonical hashを保持し、binding digestを照合します。
- contractだけを新revisionにすり替えたり、最新contractで古いrunを解釈し直したりしません。strict ETagはcontract identityを含む候補ですが、wire表現と移行影響は後続設計に残します。
- JSON Schemaの外部参照を取得せず、検証中の外部ネットワークアクセスを禁止します。Schema資源はrevisionに含まれる値とローカル実装資源に限定します。
- contract未指定のrevisionはlegacy modeで従来のYAML検証・実行を続けます。legacyからstrictへはcontractを明示した新revisionを作り、strict updateでもcontractを省略できない候補です。既存revisionの一括再解釈をしません。
- D1変更はOWS set / input / output / exportや既存ジャンプ表現を上書きしません。新しい変数機能とは別名・別契約のまま共存させます。

## D1依存図

immutable definition revisionとrequest identity<br>
→ revisionに固定したcontractの検証<br>
→ 入力の一度だけの正規化<br>
→ runごとのWorkflow scope frame<br>
→ D5が導入するiteration vectorに対応できる構造scope frame<br>
→ DBOS replayと照合可能なvariable mutation ledger<br>
→ 明示export / returnと出力Schema検証

D2 AgentノードはD1の型付き参照を使い、prompt文字列の構文はD2で定めます。D5の構造化ループはiteration vectorを提供します。D6の子Workflow呼び出しはD1のinput/output契約とreturn境界を使います。D7のIssue処理はIssue/Attemptラベルをscope frameに対応させます。D8は公開値projectionとrun/graph履歴を利用します。

## D1の互換境界と残る検討事項

- メインフローのinput_dataとJSON result形は維持します。Contract適用で既存呼び出しwire formatを変更するかは別途明示します。
- legacy definition mutation digest、legacy request id再送規則、service.pyの _verified_active_workspace を含むlegacy manifest v1検証を維持し、strict runだけrequests.sqlite binding rowとmanifest run_format_version=2を相互検証する経路に分ける候補です。過去manifest/request digestを再解釈しません。strict v2へ未対応のworker/recover/manager readerが残る間はstrict runを生成しません。
- strict runではnormalized input digestとcontract/revision bindingを名前空間付きv2 request digestに固定し、run directory内のvariables.sqliteでDBOS recoveryを行います。requests予約後に変数DBが欠落・破損すれば再作成せず停止します。run directory orphanをWorkflowとして自動復活させません。
- 現行APIのrequest body上限は128 KiB、OWS YAML上限は64 KiBです。requests.sqlite migration詳細、DDL/制約、secret future storage、Schema/値/DB/応答の総上限、Windows ACL、公開API/error codes、v2利用後のbackup/rollbackはD1 DRAFTのOpen questionsです。
- D1は既存Codex MAYBE_SENTの意味や再送禁止規則を変更しません。
