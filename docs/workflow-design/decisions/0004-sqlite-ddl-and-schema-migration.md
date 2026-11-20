# ADR 0004: SQLite DDLとschema migration

- **Status:** Proposed（レビュー待ち）
- **対象:** Workflow D1詳細設計
- **関連:** [D1詳細設計](../phases/D1-input-output-variables.md)、[アーキテクチャ](../architecture.md)、[ADR 0003: run-local変数DBとmanifest v2](0003-run-local-variable-store-and-manifest-v2.md)

このADRはD1第3回のDDL、schema validator、migration/運用gateの候補を記録します。D1全体はDRAFT、ADRはProposedです。2026-11-19の方針では、I3 migrationは隔離一時directory/test DBだけで実装・検証します。共有DB migrationは別途オペレーター承認が必要で、Global Mutex、明示NTFS DACL/owner SID、RDP/session、全ancestor/sidecar handle/file-ID/TOCTOU検証はD1必須gateではありません。本SQLやmigrationは実装済みではなく、このdocs-only作業では永続DBやアプリケーションschemaを変更しません。

## Context

現行definitions.sqliteはPRAGMA user_version=1で、_check_database_schema()は既存3 tableの完全一致を要求します。Workflow revisionは(definition_id, revision)複合primary keyで、YAML source/canonical hashとlegacy ETagを保持します。definitions.pyのwrite transactionはWAL、FULL、foreign keys、BEGIN IMMEDIATEを使います。

現行requests.sqliteにはworkflow_requests(client_request_id, workflow_id, run_id UNIQUE, request_digest, state等)、workflow_request_sources(run_id primary key, definition_id, revision, source_sha256)、workflow_requests_created_idx、workflow_requests_state_idxがありますが、PRAGMA user_version管理はありません。service.pyのCREATE TABLE IF NOT EXISTSだけでは未知schemaを排除できません。旧binaryはuser_versionを確認しないため、schema versionや同一DBのwriter lockだけでも旧writer混在は防げません。SQLite lockは複数DBの整合性も保証しません。

NodeEventWriterのschema validationを実装時の参考にできます。DBOS所有のrun-local dbos.sqliteには変更を加えません。変数状態は独立したrun-local variables.sqliteに保存します。

## Decision

以下をレビュー対象のProposed設計とします。

### 1. Schema ownershipと共通検査

各DBは独立したschema ownerとversionを持ちます。

| DB | 提案version | table構成 | owner |
| --- | --- | --- | --- |
| definitions.sqlite | v1からv2 | 現行3 table + workflow_definition_contracts | WorkflowDefinitionRepository |
| requests.sqlite | 未管理の既存user_version=0からv1 | 現行2 table + workflow_request_contract_bindings | WorkflowService |
| run-local variables.sqlite | 新規v1 | run_binding、scope_frames、variable_states、variable_mutations | Workflow variable store |
| run-local dbos.sqlite | 既存DBOS管理 | 変更しない | DBOS |

対応版は接続時にuser_version、table/column、PK、FK、indexと必要な制約を検査します。各DB transactionは`BEGIN IMMEDIATE`、unique key、必要なCAS/version/digestを使い、未知version・未知table/index・部分schemaは通常運用へ読み替えずfail closedします。PRAGMA integrity_checkがokであり、PRAGMA foreign_key_checkが行を返さないことを確認します。definitionsのlegacy data/hashes/ETag、requestsのlegacy rows/sources/indexesも各migration前後で照合します。

sha fieldの長さはSQL CHECKで確認し、64桁hex形式、canonical JSON直列化、SHA値の再計算、contract/schema意味検証はapplication validatorで行います。JSON値のcanonicalizationやdigest計算にSQLite JSON1を必要としません。tables/indexesの完全な期待値とPRAGMA結果を照合し、CREATE TABLE IF NOT EXISTSのみをschema validatorの代わりにしません。

### 2. definitions.sqlite v1→v2

将来共有definitions.sqliteを移行する場合の事前gateは次のとおりです。I3では同じmigration logicを隔離一時directory/test DBでのみ検証します。

1. 全Workflow writer/workerを停止し、他processのwriter混在がないことを確認します。
2. SQLite online backup APIで整合したsnapshotを作成し、backupを開いてschema、integrity、foreign keys、revision row/hash/ETagの検証を行います。WAL mode中の主DBファイルだけの単純copyをbackupとして扱いません。
3. PRAGMA user_version=1、既存3 tableとcolumns/indexes/FKs、revision composite keys、source/canonical hashes、legacy ETagsとrevision dataを厳格に検査します。既知のv1構成と違う場合は移行せずfail closedします。
4. 他プロセスのwriterが存在しない運用条件を維持して移行します。

接続準備ではtransaction開始前にforeign_keys=ON、journal_mode=WAL、synchronous=FULLを設定します。busy_timeout値は未決定です。migrationはBEGIN IMMEDIATEで開始し、transaction内でv1 schemaを再確認し、新tableを作成し、user_version=2を設定し、v2 schemaとintegrity/foreign-key結果をCOMMIT前に検査します。成功した時だけCOMMITします。途中crashは全rollbackとなり、部分schemaを残しません。

~~~sql
-- 接続準備: BEGINより前に実行。
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;

BEGIN IMMEDIATE;

-- ここでapplicationがv1 schemaとrevision identityを厳格に再検査。
CREATE TABLE workflow_definition_contracts (
    definition_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    contract_json_canonical TEXT NOT NULL,
    contract_sha256 TEXT NOT NULL CHECK (length(contract_sha256) = 64),
    binding_digest TEXT NOT NULL CHECK (length(binding_digest) = 64),
    created_at TEXT NOT NULL,
    PRIMARY KEY (definition_id, revision),
    FOREIGN KEY (definition_id, revision)
        REFERENCES workflow_definition_revisions (definition_id, revision)
        ON DELETE RESTRICT
);

PRAGMA user_version = 2;
-- COMMIT前にapplicationがv2 schema、integrity_check、foreign_key_check、
-- legacy rowの保持を検証。
COMMIT;
~~~

workflow_definition_contractsにはstrict revisionだけのrowを保存します。legacy revisionはrow欠落で表し、NULL rowを作りません。既存workflow_definition_revisionsの列は変更しません。新規definitions.sqliteはrepositoryの既存3-table schemaとcontract tableをv2として一括作成し、初期versionを2にします。contract、revision、定義mutation request ledgerは従来案どおり同一definitions DB write transaction内で確定します。strict ETagにはcontract identityを加えますが、legacy v1 ETagと照合は維持します。

### 3. requests.sqliteの旧schema v0→v1

現行DBはuser_version管理がないためSQLiteの初期値0を既存versionとして扱います。user_version=0の既存DBを移行する前に、workflow_requests、workflow_request_sources、created/state index、column/PK/FKと既存rowが既知のlegacy shapeに一致することを検査します。未知table/index、欠落または余分なschema object、row integrity不一致があればblind migrationせず拒否して調査します。既存table、row、indexは変更しません。legacy requestにはbinding rowを作りません。

対応版の新規DBは既存のrequest table/source table/indexとbinding tableを同じ初期化transactionで作成し、user_version=1にします。既存DB migrationはforeign_keysを有効にしてBEGIN IMMEDIATEを行い、既知schemaを再検査してbinding tableを追加し、user_version=1とschema/FK/dataを確認後にCOMMITします。

~~~sql
-- 事前にlegacy schema/dataを厳格検証し、foreign_keysを有効にしておく。
BEGIN IMMEDIATE;

CREATE TABLE workflow_request_contract_bindings (
    run_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    definition_id TEXT NOT NULL,
    definition_revision INTEGER NOT NULL CHECK (definition_revision >= 1),
    run_format_version INTEGER NOT NULL CHECK (run_format_version = 2),
    contract_version INTEGER NOT NULL CHECK (contract_version = 1),
    contract_sha256 TEXT NOT NULL CHECK (length(contract_sha256) = 64),
    binding_digest TEXT NOT NULL CHECK (length(binding_digest) = 64),
    normalized_input_sha256 TEXT NOT NULL
        CHECK (length(normalized_input_sha256) = 64),
    request_digest TEXT NOT NULL CHECK (length(request_digest) = 64),
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id)
        REFERENCES workflow_requests (run_id)
        ON DELETE RESTRICT
);

PRAGMA user_version = 1;
-- applicationがv1 object set、既存request/source rowとindex、
-- integrity_check、foreign_key_checkを検証。
COMMIT;
~~~

strict reservationはworkflow_requests、workflow_request_sources、workflow_request_contract_bindingsをrequests.sqliteの1 transactionでinsertします。applicationはそれらのworkflow_id、request_digest、definition_id/revision、source_sha256をimmutable run snapshotと照合します。legacy request/source rowにはbindingを追加しません。

旧binaryは新しいuser_versionを無視してCREATE TABLE IF NOT EXISTSを使う可能性があるため、version metadataやSQLite writer lockだけではwriter排他になりません。異なるSQLite DBをまたぐtransactionもありません。旧binaryと新schemaの安全共存は約束しません。I3は隔離DBに限定します。将来共有DBを移行する場合は別途オペレーター承認を得て、known writer/workerの停止確認、online backupと検証、schema互換確認、成功後のlegacy identity検証を行います。停止を確認できなければmigrationしません。旧Bridge本番起動時の自動migrationと旧schemaへの自動downgradeは禁止です。

### 4. variables.sqlite v1 DDL

Each run owns a new independent variables.sqlite. Connection setup enables foreign keys, WAL and FULL synchronous before opening the write transaction. The busy_timeout numeric value remains open. Initial schema creation uses one transaction and PRAGMA user_version=1. Open/status inspection uses read-only mode and never creates or initializes the file.

以下は提案するphysical schemaの完全なDDL例です。digest列は固定長SHA-256で、lowercase hex形式とdigest再計算をapplication codeで確認します。JSON値はcanonical JSON textとし、JSON nullは文字列nullで保存します。SQL NULLはmissingまたはtombstoneを表す別の値です。JSON1に依存しません。

~~~sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;

BEGIN IMMEDIATE;

CREATE TABLE run_binding (
    run_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    definition_id TEXT NOT NULL,
    definition_revision INTEGER NOT NULL CHECK (definition_revision > 0),
    contract_sha256 TEXT NOT NULL CHECK (length(contract_sha256) = 64),
    binding_digest TEXT NOT NULL CHECK (length(binding_digest) = 64),
    normalized_input_sha256 TEXT NOT NULL
        CHECK (length(normalized_input_sha256) = 64),
    run_format_version INTEGER NOT NULL CHECK (run_format_version = 2),
    store_schema_version INTEGER NOT NULL CHECK (store_schema_version = 1),
    status TEXT NOT NULL CHECK (status = 'INITIALIZED'),
    created_at TEXT NOT NULL
);

CREATE TABLE scope_frames (
    run_id TEXT NOT NULL,
    frame_id TEXT NOT NULL,
    parent_frame_id TEXT,
    scope_path TEXT NOT NULL,
    iteration_vector_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'CLOSED')),
    created_at TEXT NOT NULL,
    closed_at TEXT,
    PRIMARY KEY (run_id, frame_id),
    FOREIGN KEY (run_id)
        REFERENCES run_binding (run_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (run_id, parent_frame_id)
        REFERENCES scope_frames (run_id, frame_id)
        ON DELETE RESTRICT
);

CREATE UNIQUE INDEX uq_scope_frames_one_root
    ON scope_frames (run_id)
    WHERE parent_frame_id IS NULL;

CREATE INDEX idx_scope_frames_parent
    ON scope_frames (run_id, parent_frame_id);

CREATE TABLE variable_states (
    run_id TEXT NOT NULL,
    frame_id TEXT NOT NULL,
    declaration_id TEXT NOT NULL,
    name TEXT NOT NULL,
    present INTEGER NOT NULL CHECK (present IN (0, 1)),
    value_json TEXT,
    version INTEGER NOT NULL CHECK (version >= 1),
    value_digest TEXT NOT NULL CHECK (length(value_digest) = 64),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, frame_id, declaration_id),
    FOREIGN KEY (run_id, frame_id)
        REFERENCES scope_frames (run_id, frame_id)
        ON DELETE RESTRICT,
    CHECK (
        (present = 0 AND value_json IS NULL)
        OR (present = 1 AND value_json IS NOT NULL)
    )
);

CREATE UNIQUE INDEX uq_variable_states_scope_name
    ON variable_states (run_id, frame_id, name);

CREATE INDEX idx_variable_states_frame
    ON variable_states (run_id, frame_id, declaration_id);

CREATE TABLE variable_mutations (
    execution_key TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    frame_id TEXT NOT NULL,
    declaration_id TEXT NOT NULL,
    mutation_ordinal INTEGER NOT NULL CHECK (mutation_ordinal >= 0),
    operation TEXT NOT NULL CHECK (
        operation IN (
            'initialize',
            'assign',
            'increment',
            'decrement',
            'append',
            'object_set',
            'object_delete',
            'variable_delete'
        )
    ),
    payload_digest TEXT NOT NULL CHECK (length(payload_digest) = 64),
    before_version INTEGER NOT NULL CHECK (before_version >= 0),
    after_version INTEGER NOT NULL CHECK (after_version > 0),
    before_present INTEGER NOT NULL CHECK (before_present IN (0, 1)),
    after_present INTEGER NOT NULL CHECK (after_present IN (0, 1)),
    before_digest TEXT,
    after_digest TEXT NOT NULL CHECK (length(after_digest) = 64),
    after_value_json TEXT,
    committed_at TEXT NOT NULL,
    FOREIGN KEY (run_id, frame_id)
        REFERENCES scope_frames (run_id, frame_id)
        ON DELETE RESTRICT,
    UNIQUE (run_id, frame_id, declaration_id, after_version),
    UNIQUE (run_id, frame_id, declaration_id, before_version),
    CHECK (
        (before_version = 0 AND before_present = 0 AND before_digest IS NULL)
        OR
        (
            before_version > 0
            AND before_digest IS NOT NULL
            AND length(before_digest) = 64
        )
    ),
    CHECK (
        (after_present = 0 AND after_value_json IS NULL)
        OR (after_present = 1 AND after_value_json IS NOT NULL)
    )
);

CREATE INDEX idx_variable_mutations_history
    ON variable_mutations (run_id, frame_id, declaration_id, after_version);

PRAGMA user_version = 1;
-- COMMIT前にapplicationがtable/index/column/keyの完全一致、
-- integrity_check、foreign_key_checkを検証。
COMMIT;
~~~

run_bindingはapplication側で1 runあたり1 row、request binding/manifestとのlogical run/workflow identity一致を検証します。scope_framesのrootはpartial unique indexでrunごとに1件です。composite parent FKは参照先を検査しますが、acyclic性、親frame status、scope path、frame ID/iteration lifecycleはapplication validatorが検査します。loop frame IDはstableかつ決定的に生成する案です。

variable_statesの初期missingはrowなし、JSON nullはpresent=1とvalue_jsonがtext null、削除後はpresent=0のtombstone rowです。present/value_json整合はCHECKで制約します。同じframe内のname一意性はunique indexで制約します。declaration_idと閉じたframeの有効性はapplication layerで検証します。

variable_mutationsはafter_valueを履歴として保持します。before_version=0の場合だけbefore_digestをSQL NULLにし、before_present=0とします。tombstone resultはafter_present=0、after_value_json SQL NULLで表します。JSON null resultはafter_present=1、after_value_jsonにtext nullを保存します。after_digestは欠落状態を含めた値のdigestであり、digestの意味・再計算はapplicationが検証します。

UNIQUE after_versionとbefore_versionは同一run/frame/declarationのversion chain分岐を防ぎます。連番、期待before version/digestに対するCAS、payload digest、ledger chainの意味検査はapplicationが行います。state更新とmutation row insertは同一variables.sqlite BEGIN IMMEDIATE transactionでcommitします。historical replayは保存済みafter_value_json/digest/versionを返し、latest variable_statesとの一致を要求せず、最新値を巻き戻しません。variable_mutationsからvariable_statesへのFKは設けず、履歴保持とprune時の制約を増やしません。

### 5. Versioned schema validator

各DBを開く前に、pathが許可済みroot内にあること、user_version、expected object inventoryを確認します。variables.sqliteは既存path boundary defenseを使ってpath traversal/run外アクセスを拒否し、run/workflow identityを照合します。新たな全ancestor/sidecar reparse point、NTFS DACL/owner SID、handle/file-ID/TOCTOU inspectionはD1 gateにしません。read-only inspectionはmode=roとし、missing/unknown DBを作成しません。

| DB状態 | 期待する検査 |
| --- | --- |
| definitions v1 | user_version=1、現行の既知3 tableとcolumns/indexes/FKs、revision composite PK、legacy row/hash/ETag |
| definitions v2 | user_version=2、既存3 table + workflow_definition_contracts、PK/FK/columns/indexes、legacy identity保持 |
| requests legacy | user_version=0、workflow_requests/workflow_request_sources、created/state indexes、既知columns/keys/rows |
| requests v1 | user_version=1、既存2 table/indexとworkflow_request_contract_bindings、PK/FK/columns/rows |
| variables v1 | user_version=1、4 table、上記named/unique/partial indexes、PK/FK/CHECK、RunBinding identity |

未知table/index、余剰・欠落column、異なるPK/FK/CHECK、unknown user_version、integrity error、unexpected foreign-key rowをfail closedします。auto-indexはtable UNIQUE/PK制約から導出したindexとして検査し、固定名のuser indexと区別します。正確なCREATE SQLやPRAGMA一覧比較の実装方式はOpen Question 9に残します。

### 6. Upgrade、rollout、rollback gates

I3は以下の手順を既存の共有DBへ適用せず、新しい隔離一時directory/test DBでmigrationを実装・検証します。次の手順は将来共有DB migrationを別途オペレーターが承認した場合に限ります。

1. 既知の旧Bridge process、worker、その他のwriterを停止し、停止を確認します。requests.sqliteではuser_versionやSQLite writer lockだけで旧binaryを排除できません。停止を確認できなければmigrationしません。
2. definitions.sqliteとrequests.sqliteそれぞれにSQLite online backup APIを用い、backupを検証します。全actorを停止した同じquiescent期間にrun metadata/manifestもsnapshotします。複数DB/Filesystemのbackupは一つのatomic snapshotではないため、組を対応するsnapshot setとして識別し、restore drillで相互identityを検証します。
3. schema互換性を確認し、definitions migrationとrequests migrationを各DB内の独立したtransactionで実行します。一方だけ成功し他方が失敗した場合、strict runを有効にせず調査します。
4. migration後にuser_version、schema object inventory、integrity/FK check、legacy rows/source hashes/ETags/request identityを確認します。worker entry/start/recover、manager、task projectionを含むreaderがversion-awareと確認できてから、別途承認されたstrict enable手順へ進めます。
5. その後のisolation smokeはローカルSQLiteとWorkflow境界を確認します。Codex external POSTは既定で行いません。

definitions migration後にrequests migrationが失敗しても、strict runが作成された可能性を除外せず自動downgradeしません。strict run開始前で、writer停止中、対応するbackup setとrestore検証が済み、損失範囲が明示された場合に限ってoffline restoreを候補にします。strict run開始後に旧binary/schemaへ戻す自動rollbackは不可とし、forward repairまたは明示承認されたoffline restore/data-loss recoveryを別手順で行います。rollback時のbackup retentionと許容data lossの値はOpen Question 12に残します。

## Fault matrix

| 障害/条件 | 提案動作 |
| --- | --- |
| backup中にwriterが残る、またはWAL主DBだけをcopy | backup gate失敗としてmigrationを開始しない |
| definitions user_version/schema/hash不一致 | writer停止のままfail closedし、backupから勝手に置換しない |
| definitions CREATE後・version変更後・commit前にcrash | v1への完全rollback。部分table/versionを受け入れない |
| requests user_version=0で未知object/index/row形 | blind migrationせずfail closed |
| requests migration中にcrash | 新table/versionの全rollback、legacy row/indexを維持 |
| 共有migration中にknown writer/worker停止を確認できない | migrationを開始しない。version metadata/SQLite lockだけで旧binaryを排除したと扱わない |
| defs migration済み、requests migration未完了 | strict runを禁止し、対応版で調査・forward fix。自動rollbackしない |
| variables.sqlite missing/unknown schemaをread-only inspection | mode=roで作成せずUNKNOWN/NEEDS_RECONCILIATION |
| FK/integrity/schema validator失敗 | runを再初期化せずfail closed |
| mutation commit前crash | variables transaction rollback後に同keyを新規適用 |
| mutation commit後、DBOS checkpoint前crash | ledgerの記録済み結果を返し再適用しない |
| 後続mutation後に過去step replay | historical after valueを返しlatest stateを保持 |
| strict run後の旧version rollback要求 | automatic downgrade不可。forward repairか承認されたoffline restoreへ |

## Test cases

以下は実装時の受入設計であり、このADR作成時に実サービスやproject testsを実行する意味ではありません。

| 対象 | ケース |
| --- | --- |
| definitions backup/migration | online backupが検証できる。v1既存revision/hash/ETagが保持され、legacyはcontract rowなし、strict rowはcomposite FKを満たす |
| definitions validator | unknown version/object/column/index/FK拒否、v1→v2の各crash pointでrollback、integrity/foreign_key_check成功 |
| requests migration | user_version=0既知旧schemaとindexes/rowsを維持してv1へ移行し、unknown objectは拒否 |
| request transaction | request/source/bindingの三rowが一括commit/rollback、FK違反やidentity mismatchを検出 |
| future shared migration operator gate | 別途承認、known writers/workers停止確認、online backup/検証、schema互換、成功後legacy identityを確認。停止不能時はmigrationしない |
| variables DDL smoke | 隔離SQLite :memory:で4 tableと全indexを作成し、user_version、FK/partial unique/CHECKを検査 |
| value state | missing row、JSON null text、present=0 tombstoneを区別し、invalid combinationsを拒否 |
| mutation ledger | version chain unique、before version 0 digest規則、atomic CAS/update/ledger、same-key replay、後続更新後のhistorical replay |
| schema inspection | readonly missing DBを作らない。unknown schema/path escapeを拒否し、JSON1なしでcanonical/hash検査を行う |
| isolated migration | 隔離一時DBでonline backup、複数database snapshot pairing、partial migration、legacy identity保持を検証 |
| future shared rollout/restore | 別途承認、quiesce、online backup/検証、schema互換、複数database snapshot pairing、strict run後のauto downgrade拒否 |
| legacy regression | D1-01..65を変更せず実行し、legacy v1 manifest/request/ETag/digest回帰を維持。詳細な新規項目はD1-66以降 |

## Alternatives

| 案 | 判断 |
| --- | --- |
| definitionsの既存revision tableへcontract列を足す | 今回は採用しません。revision rowの既存形を保ち、contractを独立したrevision従属tableへ置きます |
| requests.sqliteをCREATE TABLE IF NOT EXISTSだけで拡張 | 採用しません。versionless legacy DBの未知objectやshapeを検出できないため、known-schema gateとtransactional migrationを使います |
| user_versionのみで旧requests writerを遮断 | 採用しません。現行binaryがversionを検査しないためです。I3のstartup exclusion実装は行わず、将来共有DBを移行する場合は別途承認とknown writer/worker停止確認を必要条件にします |
| variablesをDBOS-owned dbos.sqliteへ格納 | 採用しません。DBOS schema ownerとvariable ledgerの独立性を維持します |
| JSON fileだけでvariable state/ledgerを保持 | 採用しません。stateとmutation ledgerの同一transaction、unique execution key、version chainの一貫性をDDLで表せるSQLite案を維持します |

## 未決定事項

PRAGMA/CREATE SQLを比較するvalidator実装、busy_timeout値、具体的resource budget、許可rootの具体値、容量・応答上限、public API/error/status、backup retention、strict run後の許容data lossはD1 Open Questions 9〜12・14に残します。Global Mutex、Windows DACL/owner SID、全ancestor/sidecar handle/file-ID/TOCTOUはDEFERRED / NON-BLOCKINGです。本ADRのDDL・gateはProposedであり、承認済み・実装済みと扱いません。
