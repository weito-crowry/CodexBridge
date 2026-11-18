# D1実装スライス計画

- **文書状態:** PLANNING / DRAFT（レビュー待ち）
- **対象:** Workflow D1詳細設計
- **前提文書:** [基本仕様 v0.2](../requirements.md)、[D1詳細設計 DRAFT](D1-input-output-variables.md)、[ADR 0002](../decisions/0002-contract-storage-and-normalization.md)〜[ADR 0006](../decisions/0006-public-api-mcp-etag-error-contract.md)、[ADR 0007 Proposed](../decisions/0007-variable-call-adapter.md)
- **実装状態:** I0はACCEPTED (with documented caveats)、I1はACCEPTED (with documented caveats)、I2はDEFERRED / NON-BLOCKING（I2-Aは過去に実装済み・未接続、I2全体はNOT_ACCEPTED、I2-Bは停止）、I3〜I8はNOT_STARTEDです。D1全体はDRAFTで、最終承認・production enableを意味しません。

## 1. 目的と適用境界

この計画は、D1の設計・受入ケースを、将来のレビュー可能な実装指示へ分割する案です。I番号はWorkflow Phase D1内の実装slice番号で、製品Phase D1〜D8とは別の識別子です。2026-11-19の簡素化方針ではI3が次の実装sliceです。本書と関連ADRはD1全体の最終承認やproduction enableを意味せず、D1自体はDRAFTです。

各sliceは、提示された依存関係・成果物・変更面・テスト・受入条件を一つのbounded taskとして扱います。ここに記す将来のパスやモジュールは領域または候補であり、新規module名・配置を確定しません。実装時に既存の責務境界を確認し、候補を採用する場合もそのsliceのレビューで確定します。

通常のslice完了時は、そのsliceの変更・テスト結果・未達gate・rollback状態を記録し、指定branchへdocs/implementation commitをpushしてChatGPTのGitHub reviewを受けます。review前にdevへmergeしません。I3 migrationは新規隔離一時directory/test DBだけで検証し、共有DB移行には別途オペレーター承認と停止・backup・互換・legacy identity確認が必要です。pytestだけでbackup/restore behaviorを証明したり、将来のoperator stop gateを代替したりはできません。Windows ACL/RDP/Global Mutexは現行D1 gateではありません。

I0の実績は[D1-I0-baseline-evidence.md](D1-I0-baseline-evidence.md)、I1の実績は[D1-I1-contract-core-evidence.md](D1-I1-contract-core-evidence.md)、I2-Aの過去実績と当時の未達gateは[D1-I2-windows-guard-evidence.md](D1-I2-windows-guard-evidence.md)を参照してください。I3〜I8は未実装であり、実Codex job、共有/production DB migration等は未実施です。I3は隔離一時directory/test DBだけを対象にします。

## 2. 状態と依存関係

| Slice | 状態 | 依存 |
| --- | --- | --- |
| D1-I0 Baseline/traceability | ACCEPTED (with documented caveats) | なし |
| D1-I1 Pure contract core | ACCEPTED (with documented caveats) | I0 |
| D1-I2 Windows protection/controller primitives | DEFERRED / NON-BLOCKING（I2-A historical implemented、I2全体NOT_ACCEPTED、I2-B停止） | 独立。I3以後の依存ではない |
| D1-I3 Definition contract storage/migration | NOT_STARTED（次の実装slice） | I0, I1 |
| D1-I4 Variable store/ledger | NOT_STARTED | I1, I3 |
| D1-I5 Request binding/manifest/readers | NOT_STARTED | I3, I4 |
| D1-I6 HTTP/MCP contract surface | NOT_STARTED | I3, I5 |
| D1-I7 Strict root runtime/OWS call adapter | NOT_STARTED | I4, I5, I6 |
| D1-I8 Isolated acceptance/go-no-go | NOT_STARTED | I0, I1, I3〜I7 |

依存DAG:

~~~text
I0 Baseline ──> I1 Pure core ──┐
I0 Baseline ───────────────────┴──> I3 ──> I4 ──> I5 ──> I6 ──> I7 ──> I8

I2 Windows guard: DEFERRED / NON-BLOCKING (独立、I3〜I8に依存辺なし)
~~~

I3はI0/I1に依存し、I4はI1/I3、I5はI3/I4、I6はI3/I5、I7はI4/I5/I6、I8はI0/I1/I3〜I7に依存します。I2は独立保留で、このDAGのblocking edgeではありません。I8は現行の実装・互換・resource/privacy gateと隔離Windows上の実行/DB整合性を判定しますが、I2固有RDP、Global Mutex、explicit NTFS DACL/ACE、SID、file-ID/TOCTOU実証を要求しません。

## 3. Sliceごとの実装指示案

### D1-I0 — Baseline / 受入証跡

- **目的/成果物:** 現行のdefinition、HTTP/MCP、request id/ETag、run/result/graph、database/migration境界をgolden fixtureとread-only baseline記録に固定します。D1-01〜D1-146および後続D1追加ケースのtraceability台帳を開始し、各期待結果を既存契約とstrict提案に分けます。
- **変更面候補:** 既存workflow test suiteのfixture/test領域、必要最小限のdocs traceability。definitions.py/service.py/interpreter.py/API/MCPのruntime変更なし。fixture配置は既存suiteを確認後に決めます。
- **検証:** 現行pytest、Ruff、mypy、compileallをbaselineとして実行し、isolated Windows baselineではlegacy definition/run/API/MCP/result/graphを採取します。既存workspace未追跡証跡は列挙名以外参照しません。
- **受入gate:** legacy ETag/response/request replay/graph期待値が再現可能で、元となるtestとtraceability IDが各fixtureへ結び付き、変更前の環境・DB記録が読み取り専用で保存されること。
- **今回の状態:** I0は条件付き受入PASSとして受入済みです。[I0基準証跡](D1-I0-baseline-evidence.md)に対象8ファイルをカバーする新規8件と既存回帰のPASS/既知SKIPを記録しました。saved executionとgraphは29件の2ファイル合算です。歴史的一括suiteはNOT_COMPLETEのまま維持し、一括suiteをPASSとは表現しません。WinError 1314由来のsymlink 2 skipは既存記録のまま保持します。RDP/ACL gate未達をD1全体BLOCKEDの条件にはしません。D1全体はDRAFT、I1はACCEPTED、I2はDEFERRED / NON-BLOCKING、I3〜I8はNOT_STARTEDです。
- **rollback:** persistent DBや共有state rootを変更しません。fixtureが既存期待を誤って固定した場合はbaselineをpass扱いせず、そのsliceのfixture変更だけを修正・再レビューします。

### D1-I1 — Pure contract core

- **依存:** I0。
- **目的/成果物:** JSON Schema Draft 2020-12のallowlist/compile、strict contract key/type検証、canonical hash、input default normalization、missing/null区別、secret拒否、resource cap判定のpure coreを実装します。OWS schema、SQLite、HTTP/MCP、global runtime stateへ接続しません。
- **変更面:** `src/codex_bridge/workflows/contracts.py` と `tests/test_workflow_d1_i1_contract_core.py`。既存definitions validatorとの接続はI3/I6で扱います。
- **検証:** schema allowlist、external/cyclic ref拒否、depth/node/byte cap、nested defaultの一回適用、default再適用の冪等性、missing対null、secret入力拒否、legacy serialization/hash golden比較。
- **受入gate:** malformed/unsupported Schemaをsilent ignoreせず拒否し、正規化結果/hashが同一fixture上で決定的で、legacy経路のdigest calculationが変化していないこと。
- **rollback:** DB migrationを含めません。pure coreが旧validator/正規化と干渉した場合、strict呼出しを無効にしたままI1だけを修正し、legacy fixturesを維持します。
- **今回の状態:** 純粋contract coreと単体テストを追加し、記録したI1検証はPASSです。ChatGPTのGitHubレビューを受け、ACCEPTED (with documented caveats)です。[I1実装証跡](D1-I1-contract-core-evidence.md)。I0はACCEPTED、I2はDEFERRED / NON-BLOCKING、I3〜I8はNOT_STARTEDです。strict runtimeや既存呼出し元には接続していません。

### D1-I2 — Windows protection / controller primitives（DEFERRED / NON-BLOCKING）

- **状態・依存:** I2-AのGlobal Mutex primitiveと専用テストは既に実装済みで、controller/API/MCP/SQLite/DBOSへ未接続です。I2全体はNOT_ACCEPTEDです。I2-Bの追加実装は停止し、I2はI0/I1/I3〜I8から独立した保留項目です。
- **従来の目的/成果物（履歴）:** Windows Global named Mutex、明示DACL/SID、owner thread lifetime/release、reparse/handle-based path検証、旧process/worker quiescence gateの隔離primitive案でした。この一式は現行D1必須要件ではなく、I3/I8の受入gateにしません。
- **変更面候補:** Windows workflow startup/path protection領域とWindows-specific isolated test領域。既存 named DB schemaへ変更を加えません。新module/file名は実装時に既存配置を確認して決めます。
- **現行扱い:** I2-Aの過去証跡は保持します。I2-B、RDP、別session、quiescenceの未実装/未検証はD1/I3/I8をblockしません。現存moduleを削除・接続せず、D1の必須依存にもしません。

### D1-I3 — Definition contract storage / isolated schema migration / CRUD core

- **依存:** I0, I1。I2は依存ではありません。
- **目的/成果物:** revision-bound contract保存、definitions.sqlite v1→v2とrequests.sqlite user_version=0→schema v1 migration、legacy互換を新しい隔離一時directory/test DBで実装・検証します。immutable revision、legacy ETag/request identity、migration backup/verify behaviorを維持し、strict saved-run admissionはOFFです。
- **変更面候補:** definitions.py、service.py、definition repository/mutation transaction領域と専用DB migration tests。現行schema ownershipを保ち、将来の新module名は固定しません。
- **検証:** 隔離SQLite backup/restore、user_version/schema/object検査、`integrity_check`/`foreign_key_check`、CREATE/version/commit各境界のcrash injection、legacy revision/hash/v1 ETag/request-id保存、contract row・revision・mutation ledgerの同一definitions DB transactionを確認します。
- **受入gate:** contract bindingとCASが一致し、legacy ETag/digest/revision/request-idは回帰比較を通過します。I3で共有/production DBを開いたりmigrationしたりしません。
- **将来の共有DB移行:** 別途オペレーター承認、known writers/workers停止、停止状態の確認、SQLite online backupと検証、schema互換確認、成功後のlegacy identity検証が必要です。停止を確認できなければmigrationしません。SQLite writer lockやuser_version単独で旧binary/別controllerを排除できるとは扱わず、旧binaryとの安全共存・自動upgradeを約束しません。旧Bridge本番起動中の自動migration、旧schemaへの自動downgradeは禁止です。
- **rollback:** isolated test DBのみでtransaction rollbackまたは検証済み一時backup restoreを行います。共有DBのrestore/downgradeはこのsliceの範囲外です。

### D1-I4 — Variable store / mutation ledger

- **依存:** I1, I3。
- **目的/成果物:** run-local variables.sqlite schema v1、RunBindingとworkflow root ScopeFrame、declaration/type/sensitivity validation、initialize/assign/increment/decrement/append/object_set/object_delete/variable_delete、execution_key ledgerを実装します。interpreter/OWS callへのwireは含めません。
- **変更面候補:** variable store repository/transaction領域（配置名未確定）とunit/crash-injection test領域。run-local DBだけを扱いdefinitions/requests/dbos/node-event DBへ値を混在させません。
- **検証:** `BEGIN IMMEDIATE`、unique key、CAS/version/digest、schema/user_version/integrity/foreign-key check、stateとledgerの同一transaction、tombstone対JSON null、RFC 6901、required property delete rollback、boolean/number厳密性、NaN/Inf拒否、duplicate replay防止、older historical after replay後のlatest state維持、commit前後DBOS crash、現行resource/privacy checks。
- **受入gate:** 同一execution_key再生は保存済みhistorical resultを返し、latest stateを巻き戻さず、同key異payloadはfail closed。Codex MAYBE_SENT状態機械を流用しません。
- **rollback:** isolated run DBだけを使用し、shared run dataは削除・再作成しません。chain不整合はsuccessにせずreconciliation扱い。strict dataが存在する場合はschema downgradeしません。

### D1-I5 — Request binding / manifest v2 / reader gates

- **依存:** I3, I4。
- **目的/成果物:** request contract binding、manifest run_format_version=2、normalized input snapshot、legacy v1とstrict v2を区別する全readerのversion-aware対応を実装します。
- **変更面候補:** service.py、manager.py、worker.py、task projection、graph/event/status/result/recovery読取経路とreader tests。既存component責務を保ちます。
- **検証:** binding/manifest/input/variables DBの相互一致、missing/corrupt store fail closed、orphan handling、crash/recovery、legacy full regression、全readerのformat path audit、MAYBE_SENTの不確実送信を再POSTしないこと。
- **受入gate:** どのreaderにもstrict v2未対応経路が残らず、legacy runをstrictとして再解釈しません。strict admission feature flagはI8 gateまでOFFです。
- **rollback:** binding/manifestが一致しないrunはneeds_reconciliationで停止し、statusを成功へ投影しません。旧runを新方式で再計算・変換せず、strict runの自動削除や自動降格をしません。

### D1-I6 — HTTP/MCP contract surface

- **依存:** I3, I5。
- **目的/成果物:** 既存HTTP/MCP Create/Validate/Update/Get/List/graphのoptional contract、strict ETag v2/CAS、run-start DTO、output/error/status DTOとread/write/execute境界を実装します。strict run-startはまだOFFです。
- **変更面候補:** definition_api.py、definition_mcp.py、run_mcp.py、api.pyおよびそれらのDTO/permission/response tests。新endpoint/toolを増やさず、legacy route/toolの形を維持します。
- **検証:** legacy exact-response golden、v1 ETag/CAS、strict v2 ETag/CAS、validation/error mapping、HTTP/MCP cross-transport mutation replay、status/result、MCP permission、response/body cap、reason_code redaction。
- **受入gate:** strict fieldsは許可リストだけで、unknown fieldsを拒否し、未対応reader/permissionを通過させません。HTTP stale ETagは412、MCP stale expected_revisionは既存MCP precondition error。strict run-start flagはOFFです。
- **rollback:** strict public surfaceを非公開/OFFに戻してもlegacy routes/response/errorは維持。strict run-startが一度も有効化されていないisolated状態でのみ巻き戻し、versioned stored dataを旧形式へ再解釈しません。

### D1-I7 — Strict single-Workflow root runtime

- **依存:** I4, I5, I6。
- **目的/成果物:** strict contract revisionに限定したOWS call: codexbridge-variable adapter、workflow root frame連携、read-only jq projection $cbvars、DBOS mutation step、output Schema validation、safe reason_codeを接続します。Issue/Attempt scope、loop/subflow、dynamic promptは含めません。
- **変更面候補:** interpreter.pyのcall dispatch/validation、worker.pyのexecution_key context、I4 variable store adapter、OWS/interpreter compatibility tests。新module pathは候補とし実装時に確定します。
- **検証:** 既存ows schemaにvalidだが現行interpreter subsetではunsupportedなstrict fixture、strict/legacy call target分離、operation/argument validation、$cbvars snapshot/missing-value、same-key replay、OWS set/context/output/jump、mock-job/codex-job regression、no external Codex side effect。
- **受入gate:** strict runはisolated Windows smokeに限定し、root scope/frame identityがrun bindingから取得されcaller引数で偽装できません。legacy revisionにcall adapterや$cbvarsを導入しません。
- **rollback:** strict adapter entryを無効にし、既存call dispatch/modeを保持します。未確定なmutationはneeds_reconciliationとして残し、外部Codex POSTを再試行しません。real Codex jobは別途明示許可なしに実行しません。

### D1-I8 — Isolated Windows acceptance / go-no-go

- **依存:** I0, I1, I3〜I7。I2は依存ではありません。
- **目的/成果物:** 現行D1 traceability、隔離Windows execution、SQLite DB integrity/foreign-key/schema validation、migration/rollforward、crash/recovery、HTTP/MCP dogfoodの証跡とgo/no-go報告を作成します。
- **変更面候補:** 既存pytest/Ruff/mypy/compileall対象、隔離Windows test environment、docs evidence。production/shared state rootやlive Codex実行環境は対象外です。
- **検証:** pytest、Ruff check/format、mypy、compileall、I3/I4/I5のisolated migration・crash/recovery・DB整合性、Windows実行、MCP permission/response acceptance。実Codex external jobは個別に明示許可された場合だけ実行します。
- **受入gate:** D1-01〜D1-156とPD-01..PD-39のtraceabilityを現行/DEFERREDに分類し、現行gateを実証します。隔離Windows executionとDB整合性確認は行います。D1-93..107、Global Mutex/RDP、explicit DACL/ACE/SID、file-ID/TOCTOU gateは不要です。現行gateの未実施・不明・失敗はNOT PASSEDですが、DEFERRED項目はblockerにしません。pytestだけでmigration運用承認をpassにしません。
- **rollback/go-no-go:** gate未達ならstrict enable、production migration、releaseを行いません。全gate通過後もproduction strict enableには別途ユーザー承認を提案し、自動移行・merge・releaseはしません。

## 4. D1受入ケースtraceability（primary slice）

下表は現行受入ケースD1-01〜D1-146を番号を失わずsliceへ対応させた一次対応表です。範囲内の全IDを含み、sliceは一つ以上でよく、I8では現行gateのケースを最終回帰として再実行します。DEFERRED IDはtraceabilityに残して現行gateから除外します。新規adapter受入ケースD1-147〜D1-156は最後に別行で加えます。

| D1受入ID | Primary slice | 対応領域 |
| --- | --- | --- |
| D1-01..06 | I1 | Schema dialect/type/required/null/default/additionalProperties/enum |
| D1-07..08 | I1, I5 | normalized request identityとidempotency |
| D1-09..10 | I0, I3, I5, I6 | immutable revision、legacy definition/request/response |
| D1-11..16 | I4, I7 | scope/declaration/operations/execution key |
| D1-17..21 | I4 | mutation CAS、DBOS crash、historical replay、conflict |
| D1-22 | I5 | subflow return契約のmock（D6の実呼出しなし） |
| D1-23 | I6, I7 | output validationとresult publication |
| D1-24..26 | I1, I6, I7 | secret拒否、ref、安全なrequest/schema resource cap |
| D1-27 | I0, I7 | OWS set/export/jump/legacy regression |
| D1-28..30 | I3, I6 | contract row transaction、binding hash、CRUD mode |
| D1-31..37 | I1, I3, I6, I7 | strict/legacy root、keyword、ETag、schema normalization |
| D1-38..41 | I1, I4, I7 | existing OWS scope adapter、object mutation、secret |
| D1-42..50 | I0, I3, I5 | definitions migration、request reservation、manifest/binding |
| D1-51..62 | I4, I5, I7 | missing/corrupt variables DB、reader/replay/MAYBE_SENT |
| D1-63..65 | I0, I3, I5, I8 | 旧backup/restore・migration/downgrade期待結果を保持。隔離test DBでの検証を現行対象とし、共有DB運用手順は将来のoperator gateとしてDEFERRED |
| D1-66..78 | I0, I3, I5 | backup, v1→v2, request migration/schema/old binary (共有DBのoperator gateは将来手順) |
| D1-79..92 | I3, I4, I5 | physical DB schema, state/ledger, transaction and recovery |
| D1-93..107 | I2 (historical / DEFERRED; non-blocking) | Global mutex, old-writer gate, NTFS ACL/path/reparseの旧期待結果を保持。現行I8 gateから除外 |
| D1-108..120 | I1, I4, I6, I8 | 現行resource/privacy/rollback behavior。strict数値budgetはProposed、D1-115 hard WAL/SHM byte guaranteeはDEFERRED |
| D1-121..132 | I0, I3, I6 | public contract wire, CRUD, ETag, legacy GET/list/graph/errors |
| D1-133..138 | I5, I6, I7 | strict start binding/input, output/result, uncertain state |
| D1-139..146 | I6, I7, I8 | HTTP/MCP errors/permissions/caps/client compatibility/CAS race |
| D1-147..156 | I0, I1, I4, I6, I7 | ADR 0007 variable call validation/runtime/replay/regression |

この一次対応表により各既存IDはD1-01からD1-146まで連続しています。新しいD1ケースは末尾のD1-147〜D1-156のみです。D1-93..107のID/期待結果は履歴として残し、現行gateから外します。D1-45/D1-77の旧binary fail-closed/startup exclusion期待も履歴として保持し、実装gateにはしません。D1-53/D1-86の旧reparse-specific期待も履歴として保持し、既存root containment/path traversal/run外アクセス拒否とRunBinding behaviorのテストを維持します。全ancestor/sidecar Win32 handle・file-ID・DACL要件は追加しません。将来共有DB migration時のoperator stop/backup/compatibility/legacy identity条件は別途適用します。D1-108以降は既存上限、resource/privacy behaviorを維持し、strict追加数値はProposed data budgetとして扱います。D1承認後の実装slice開始時に、各ケースの具体的なtest名/fixture pathを既存test treeに合わせて追加し、IDの重複や再採番をしません。

## 5. Slice運用上の最終状態

I0/I1はACCEPTED (with documented caveats)、I2はDEFERRED / NON-BLOCKING（I2-A historical implemented but not connected、I2全体NOT_ACCEPTED、I2-B停止）、I3〜I8はNOT_STARTEDです。I3が次の実装sliceです。I番号は実装分割であり、D1全体の最終承認やstrict workflow機能のproduction enableを示しません。各sliceの作業指示では必要な変更面・isolated resource・テスト・停止条件を明示します。今回のdocs-only変更ではpytest、Ruff、mypy、compileallをNOT_RUNとし、実Codex job、共有/production DB migration、full strict enableも行っていません。
