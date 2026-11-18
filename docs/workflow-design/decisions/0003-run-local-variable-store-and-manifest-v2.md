# ADR 0003: run-local変数DBとmanifest v2

- **Status:** Proposed（レビュー待ち）
- **対象:** Workflow D1詳細設計
- **関連:** [D1詳細設計](../phases/D1-input-output-variables.md)、[アーキテクチャ](../architecture.md)、[ADR 0002: contractのrevision保存と正規化](0002-contract-storage-and-normalization.md)

このADRはChatGPTが設計したD1第2回の候補を記録します。基本仕様 v0.2を変更せず、D1はDRAFTのままです。2026-11-19の合意に従い、D1必須条件はsingle-DB transaction、replay-safe ledger、既存互換と許可済みroot内のpath境界です。Global Mutex、明示NTFS DACL/owner SID、全ancestor/sidecar handle検査はDEFERRED / NON-BLOCKINGです。本ADRはProposedのままで、schema migration、DB/API変更、実装承認を意味しません。

## Context

Workflowのdefinition revisionとrequest ledgerは既存のSQLite DBに保存され、workerはDBOS step/checkpointで回復します。strict contract、normalized input、型付き変数の追加では、それらのidentityと変数状態を実行中に一貫して照合する必要があります。

definition DB、request DB、DBOS DB、node-event DBは別の責務を持ち、ファイルシステム上のrun snapshotもSQLite transactionとは別境界です。DBOS checkpointと変数mutationを一つの物理transactionにすることもできません。過去stepのreplayは、その後のmutationが進めた最新stateを巻き戻さず、当該mutationが記録した結果を再現する必要があります。

既存legacy runのmanifest/request digestやCodex job adapterのMAYBE_SENT再送規則は、新しいstrict runの識別・回復方式で再解釈できません。

## Decision

以下をD1のProposed設計として扱います。

### 1. run-local変数DB

各Workflow run directoryに新規・独立したvariables.sqliteを置きます。配置例は $LOCALAPPDATA/CodexBridge/workflows/run-<runid>/variables.sqlite です。definitions.sqlite、requests.sqlite、run-local dbos.sqlite、node-event DBの既存tableへ変数値を直接混在させません。

変数DBはVariableState、ScopeFrame、VariableMutation、RunBinding専用schemaを持ち、提案user_versionは1です。必須のwrite境界はsingle-DB `BEGIN IMMEDIATE` transactionで、stateとmutation ledgerを同じtransactionでcommitします。unique execution key、CAS/version/digest、schema/user_version、`integrity_check`、`foreign_key_check`を整合検査します。journal_mode=WAL、synchronous=FULL、foreign_keys=ONを使い、busy_timeout値は未決です。read-only inspectorはreadonlyで開いてDBを作成・初期化しません。run directory pathが許可済みroot内にあることは既存のpath boundary defenseで確認し、run/workflow identityとRunBindingを照合します。新しい全ancestor/sidecar reparse、Win32 handle/file-ID、TOCTOU、NTFS DACL/owner SID検査は従来候補でありDEFERREDです。

正確なDDL、index、CHECK制約と具体的なdata/resource budgetはD1のOpen Questionsに残します。Windows ACLや全祖先handle検査はD1必須条件ではありません。

### 2. definitions.sqlite v1→v2 migration

現行definitions.sqliteはuser_version=1で、schema checkerはtable名の完全一致を検証します。strict contract rowを追加するため、単一SQLite write transactionでv1からv2へ移行し、workflow_definition_contractsを作成します。rowはdefinition_idとrevisionの複合keyで親のimmutable revisionを参照し、contract_json_canonical、contract_sha256、binding_digest、created_atを持ちます。

既存revision row、primary key、YAML hash、legacy ETagは保持します。legacy revisionにはcontract rowを作らず、workflow_definition_revisionsへcontract列を後付けしません。migrationのcommitでPRAGMA user_version=2とv2 schemaを整合させます。途中crashは全rollbackまたは全完了とし、中間schemaを許容しません。schema mismatchはfail closedです。

I3では新しい隔離一時directory/test DBだけでmigration codeを実装・検証し、共有DBや本番DBを変更しません。`user_version`やSQLite writer lockは旧binaryが新schemaへアクセスすることを防がず、異なるDB間のatomicityも提供しません。旧binaryとの安全共存や自動upgradeは約束しません。将来共有DBを移行する場合は別途オペレーター承認を要し、既知writer/worker停止を確認し、SQLite online backupと検証、schema互換確認、成功後のlegacy identity検証を行います。停止を確認できなければ移行しません。旧Bridge本番起動時の自動migrationと旧schemaへの自動downgradeは禁止です。migration SQLと実番号は実装時に詰めます。

### 3. requests.sqlite strict binding

strict runのみを識別する新規workflow_request_contract_bindings tableをrequests.sqliteへ追加する案です。run_idをkeyとしてworkflow_id、definition_id、definition_revision、run_format_version=2、contract_version=1、contract_sha256、binding_digest、normalized_input_sha256、request_digestを保持します。request rowとbinding rowはrequests.sqlite内の同一transactionでinsertします。legacy requestにはbinding rowを作らず、既存のrequest id同一性・再送規則を維持します。

definitions.sqlite、requests.sqlite、run directory間には単一atomic transactionがありません。revisionとsnapshotのSHA照合、request binding、fail-closed回復で境界をつなぎます。requests.sqlite既存schemaとの互換・migrationとFKの具体形は未確定です。

### 4. manifest v2とreader互換ゲート

manifestにrun_format_versionがない既存runはlegacy v1として扱い、既存manifest/request digestを一切再計算・再解釈しません。strict run manifestはrun_format_version=2を必須とし、既存run identityに加えてdefinition_id、definition_revision、contract_version=1、contract_sha256、binding_digest、normalized_input_sha256、variable_store filename=variables.sqlite、schema_version=1を持ちます。secretや変数実値はmanifestに含めません。input.jsonには正規化済みinputを保存します。

strict request digestはcodexbridge-workflow-request-v2名前空間付きcanonical JSONとし、Workflow canonical content、workspace、max_live_codex_jobs、definition identity/revision、YAML SHA、contract/binding SHA、normalized input SHA、その他のrun identityを含めて一度だけ作ります。request ledgerとmanifest検証は同じdigest規則を使います。既存legacy digestの計算は不変です。

run_format_versionだけでstrict/legacyを判断せず、manifestとrequests.sqlite binding rowを相互検証します。binding rowがあるのにmanifest versionがない/1、binding rowなしでmanifestがversion 2、未知version、revision/hash/input/store identityの不一致はNEEDS_RECONCILIATIONとして扱います。legacy request rowをstrict runとして読み替えません。

strict run生成前に、_verified_active_workspace、_inspect_run、worker entry/start/recover、task projection、managerを含む全readerがv2判定とbinding照合に対応する必要があります。既存managerのstrict control key検査を黙って緩めません。未対応readerがある間はstrict runを生成しません。

### 5. 作成・起動・recovery順

strict runの作成・回復順は次の通りです。

1. immutable definition/contract revisionと全digestを検証します。
2. inputを一度正規化し、strict v2 request digestを確定します。
3. run directoryとimmutable YAML/input/manifest snapshotを準備します。
4. variables.sqliteを初期化し、schemaとlogical run/workflow binding検査後にRunBinding status=INITIALIZEDを保存します。
5. requests.sqliteのrequest rowとstrict binding rowを同一transactionで予約します。
6. worker開始前およびrecover時にrequest binding、manifest、revision/contract、immutable input、variables.sqlite schema/RunBinding、mutation ledgerとDBOS checkpointを照合します。

run directory準備後、request reservation前にcrashした場合、orphan directoryが残ることがあります。orphanをWorkflowとして自動復活させず、監査対象とします。cleanupはrequest reservationや他の実行が存在しないことを確認した後に限り候補とし、その確認前に自動削除しません。strict reservation後にvariables.sqliteが欠落・破損した場合はneeds_reconciliationで停止し、DBを再作成せず、worker起動やCodex job再送を行いません。

Filesystem、variables.sqlite、requests.sqliteをまたぐatomic commitは仮定しません。同一SQLite内のwriter lockはそのDBのtransactionだけを調停し、旧binary/別controllerの新schemaアクセスや複数DB間整合性を保証しません。どの照合点でも不一致があればUNKNOWN/NEEDS_RECONCILIATIONで止め、成功・受入成功として返しません。Codex adapterの既存MAYBE_SENT state machineは変更せず、再送しません。

### 6. mutation replay

初回mutationだけがexpected before version/digestに対するCASを行い、VariableState更新とVariableMutation ledger記録をvariables.sqlite内の同一transactionでcommitします。

既存execution_keyと同じpayloadのreplayでは、そのledger rowに記録済みのafter_value、after_digest、versionを当該mutation結果として返します。ledger payload/hash/resultの自己整合、version chain、RunBinding、DBOS checkpointとの一致を検査します。

後続mutationがcommit済みならlatest VariableStateが古いafter resultより先のversionへ進んでいるのは正常です。historical replayはlatest stateが古いafter digestと一致することを要求せず、最新値へ書き戻したり、increment/appendを再適用したりしません。同じkeyの異なるpayload、chain conflict、digest不整合、DB corruptionはNEEDS_RECONCILIATIONとして停止します。MAYBE_SENTを変数mutationの再現に流用しません。

## Consequences

- 変数状態の所有範囲がrun directoryとrun identityに固定され、別runのstate混在を避けられます。
- request reservationと変数DBは別DB/Filesystem境界のため、orphan・予約後の欠落・partial setupを明示したrecovery分岐が必要です。
- manifest v2を読む全readerの互換実装が済むまでstrict runを有効化できません。
- 過去stepのreplayはhistorical mutation resultとlatest stateを区別します。後続versionが存在すること自体をconflictにしません。
- migrationとv2利用後のbackup/rollback運用は別途確定が必要です。strict v2使用後に旧schemaへ安全にdowngradeできるとは仮定しません。
- I3は隔離test DBだけでmigrationを検証し、共有/production DB変更には別途operator approvalと停止・backup・compatibility・legacy identity gatesが必要です。
- strict v1 secret入力・secret variableは前回PD-10通り拒否します。Graph/MCPは許可済みmetadataだけを公開し、変数実値を返しません。

## Failure matrix

| 条件 | 提案処理 |
| --- | --- |
| definitions migration中のcrash | SQLite transaction rollbackまたは全commit。部分schemaを起動時に許可しない |
| 旧binaryや別controllerが新schemaへアクセスする可能性 | `user_version`/SQLite lockだけで排除したと見なさない。I3は隔離DBのみ。共有DB移行は別途承認とwriter/worker停止が確認できなければ行わない |
| request bindingとmanifest versionの片側欠落・不一致 | NEEDS_RECONCILIATION。legacy downgradeとして扱わない |
| 予約前のrun directory orphan | 自動復活せず監査対象。reservation不存在を確認するまで削除しない |
| reservation済みstrict runでvariables.sqlite欠落/破損 | DBを再生成せずworkerを起動しない。NEEDS_RECONCILIATION |
| 保存pathが許可済みroot外、path traversal、run binding不一致 | 既存path boundary defense/RunBinding照合で拒否 |
| SQLite commit前のmutation crash | transaction rollback後に同じkeyを初回実行 |
| mutation commit後・DBOS checkpoint前のcrash | ledgerの保存済み結果を返し、更新を再適用しない |
| 後続mutation後に古いstepをreplay | historical resultを返す。latest VariableStateを巻き戻さない |
| 同一keyの異payload、ledger chain/digest不整合 | stateを上書きせずNEEDS_RECONCILIATION |
| recovery binding/store検証中に状態が不確定 | UNKNOWN/NEEDS_RECONCILIATION。成功を返さず、Codex POSTしない |

## Alternatives

| 案 | 判断 |
| --- | --- |
| state-root shared variables DB | 今回は採用しません。runごとのstate ownershipと隔離がDB内部のrun keyだけに依存するためです。容量・運用集約上の利点は別途比較可能です |
| dbos.sqliteのtableへ変数値を保存 | 今回は採用しません。DBOS内部stateとアプリ変数台帳の更新・schema所有責務を混在させないためです |
| 変数をJSON/file snapshotだけで保存 | 今回は採用しません。複数mutationの原子台帳、execution_key一意性、version chain、replay検査を単一transactionで保持する候補としてSQLiteを選びます |

## 未決定事項

ADR Proposed状態を解消する前に、D1のOpen Questions 9〜12、14に記載したDDL/制約、requests.sqlite migration、resource budget、公開field/status、backup/rollback、外部副作用処理を具体化します。Global Mutex、Windows DACL/owner SID、全ancestor/sidecar handle/file-ID gateはDEFERRED / NON-BLOCKINGです。これらを本ADRが確定済みとするものではありません。
