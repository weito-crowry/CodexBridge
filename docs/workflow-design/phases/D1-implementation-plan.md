# D1実装スライス計画

- **文書状態:** PLANNING / DRAFT（レビュー待ち）
- **対象:** Workflow D1詳細設計
- **前提文書:** [基本仕様 v0.2](../requirements.md)、[D1詳細設計 DRAFT](D1-input-output-variables.md)、[ADR 0002](../decisions/0002-contract-storage-and-normalization.md)〜[ADR 0006](../decisions/0006-public-api-mcp-etag-error-contract.md)、[ADR 0007 Proposed](../decisions/0007-variable-call-adapter.md)
- **実装状態:** I0はREVIEW_PENDING（回帰検証PASS、ChatGPTレビュー待ち）、I1〜I8はNOT_STARTED。D1の機能コード実装は開始していません。

## 1. 目的と適用境界

この計画は、D1の設計・受入ケースを、将来のレビュー可能な実装指示へ分割する案です。I番号はWorkflow Phase D1内の実装slice番号で、製品Phase D1〜D8とは別の識別子です。本書と関連ADRは実装や運用の許可を意味せず、D1自体もユーザー最終承認前のDRAFTです。

各sliceは、提示された依存関係・成果物・変更面・テスト・受入条件を一つのbounded taskとして扱います。ここに記す将来のパスやモジュールは領域または候補であり、新規module名・配置を確定しません。実装時に既存の責務境界を確認し、候補を採用する場合もそのsliceのレビューで確定します。

通常のslice完了時は、そのsliceの変更・テスト結果・未達gate・rollback状態を記録し、指定branchへdocs/implementation commitをpushしてChatGPTのGitHub reviewを受けます。review前にdevへmergeしません。DB migrationはisolated preproductionに限定し、production/shared state rootでは明示的な後続承認なしに実行しません。pytest等の結果だけでWindows ACL、旧binary quiescence、backup/restore gateがpassしたことにはしません。

I0の実績は[D1-I0-baseline-evidence.md](D1-I0-baseline-evidence.md)を参照してください。I1〜I8は未実装であり、実Codex job、production DB migration等は未実施です。

## 2. 状態と依存関係

| Slice | 状態 | 依存 |
| --- | --- | --- |
| D1-I0 Baseline/traceability | REVIEW_PENDING（検証PASS、ChatGPTレビュー待ち） | なし |
| D1-I1 Pure contract core | NOT_STARTED | I0 |
| D1-I2 Windows protection/controller primitives | NOT_STARTED | I0。I1への技術依存はないが、単一Codexの実作業順ではI1の後 |
| D1-I3 Definition contract storage/migration | NOT_STARTED | I0, I1, I2 |
| D1-I4 Variable store/ledger | NOT_STARTED | I1, I2, I3 |
| D1-I5 Request binding/manifest/readers | NOT_STARTED | I3, I4 |
| D1-I6 HTTP/MCP contract surface | NOT_STARTED | I3, I5 |
| D1-I7 Strict root runtime/OWS call adapter | NOT_STARTED | I4, I5, I6 |
| D1-I8 Isolated acceptance/go-no-go | NOT_STARTED | I0〜I7 |

依存DAG:

~~~text
                         ┌──── I1 ────┐
I0 Baseline ────────────┤             ├── I3 ── I4 ── I5 ── I6 ── I7 ── I8
                         └──── I2 ────┘                  └───────────────┘
~~~

I6はI3とI5の両方を満たしてから始めます。I8は全ての実装・互換・運用gateをまとめて判定します。依存sliceが未完了、証跡不足、またはrequired environment test未実施の場合、後続sliceの開始/strict enableをpass扱いにしません。

## 3. Sliceごとの実装指示案

### D1-I0 — Baseline / 受入証跡

- **目的/成果物:** 現行のdefinition、HTTP/MCP、request id/ETag、run/result/graph、database/migration境界をgolden fixtureとread-only baseline記録に固定します。D1-01〜D1-146および後続D1追加ケースのtraceability台帳を開始し、各期待結果を既存契約とstrict提案に分けます。
- **変更面候補:** 既存workflow test suiteのfixture/test領域、必要最小限のdocs traceability。definitions.py/service.py/interpreter.py/API/MCPのruntime変更なし。fixture配置は既存suiteを確認後に決めます。
- **検証:** 現行pytest、Ruff、mypy、compileallをbaselineとして実行し、isolated Windows baselineではlegacy definition/run/API/MCP/result/graphを採取します。既存workspace未追跡証跡は列挙名以外参照しません。
- **受入gate:** legacy ETag/response/request replay/graph期待値が再現可能で、元となるtestとtraceability IDが各fixtureへ結び付き、変更前の環境・DB記録が読み取り専用で保存されること。
- **今回の状態:** [I0基準証跡](D1-I0-baseline-evidence.md)に対象8ファイルをカバーする新規8件と既存回帰のPASS/既知SKIPを記録しました。saved executionとgraphは29件の2ファイル合算です。歴史的一括suiteはNOT_COMPLETEのまま維持し、一括suiteをPASSとは表現しません。状態はREVIEW_PENDING（ChatGPTレビュー待ち）です。
- **rollback:** persistent DBや共有state rootを変更しません。fixtureが既存期待を誤って固定した場合はbaselineをpass扱いせず、そのsliceのfixture変更だけを修正・再レビューします。

### D1-I1 — Pure contract core

- **依存:** I0。
- **目的/成果物:** JSON Schema Draft 2020-12のallowlist/compile、strict contract key/type検証、canonical hash、input default normalization、missing/null区別、secret拒否、resource cap判定のpure coreを実装します。OWS schema、SQLite、HTTP/MCP、global runtime stateへ接続しません。
- **変更面候補:** workflow package内の純粋なcontract/normalization領域（module/file名は未確定）と対応unit test領域。既存definitions validatorとの接続はI3/I6で扱います。
- **検証:** schema allowlist、external/cyclic ref拒否、depth/node/byte cap、nested defaultの一回適用、default再適用の冪等性、missing対null、secret入力拒否、legacy serialization/hash golden比較。
- **受入gate:** malformed/unsupported Schemaをsilent ignoreせず拒否し、正規化結果/hashが同一fixture上で決定的で、legacy経路のdigest calculationが変化していないこと。
- **rollback:** DB migrationを含めません。pure coreが旧validator/正規化と干渉した場合、strict呼出しを無効にしたままI1だけを修正し、legacy fixturesを維持します。

### D1-I2 — Windows protection / controller primitives

- **依存:** I0。I1との技術依存はなく、I0後に独立してよいsliceですが、作業者・branch制約に従い直列で実施します。
- **目的/成果物:** Windows Global named Mutex、明示DACL/SID、owner thread lifetime/release、reparse/handle-based path検証、旧process/worker quiescence operational gateのisolation-only primitiveを実装します。Local namespace fallbackは設けません。
- **変更面候補:** Windows workflow startup/path protection領域とWindows-specific isolated test領域。既存 named DB schemaへ変更を加えません。新module/file名は実装時に既存配置を確認して決めます。
- **検証:** 同一state_root/SIDの同一session・異なるRDP/fast-user-switching/service session、Global mutex作成/取得競合、WAIT_ABANDONED照合、DACL/SID mismatch、reparse/TOCTOU、旧binary/autostart/leftover worker gateをisolated Windows environmentで確認します。共有production rootを使いません。
- **受入gate:** 全関連sessionを検査できない、Global Mutex/ACL/ownerを確認できない、または旧writer/worker quiescenceを証明できない場合はstrict enable不可とし、未実施をPASSに読み替えません。
- **rollback:** isolated test resourceだけを対象にし、shared state rootや既存ACLを変更しません。Windows primitiveに不一致があればstrict startupを無効のまま修正し、旧controllerを強制終了しません。

### D1-I3 — Definition contract storage / schema migration / CRUD core

- **依存:** I0, I1, I2。
- **目的/成果物:** definitions.sqlite v1→v2とrequests.sqlite user_version=0→schema v1のisolated migration、contract revision binding、immutable revision、v2 ETag計算、strict mutation cross-transport digest、backup/verify手順を実装します。strict saved-run admissionはまだOFFです。
- **変更面候補:** definitions.py、service.py、definition repository/mutation transaction領域と専用DB migration tests。現行schema ownershipを保ち、将来の新module名は固定しません。
- **検証:** SQLite online backup/restore、unknown schema/object拒否、CREATE/version/commit各境界のcrash injection、legacy revision/hash/v1 ETag保存、contract row・revision・mutation ledger原子性、HTTP If-Match/MCP expected_revision同値正規化、同一request id cross-transport replayと異revision conflict。
- **受入gate:** strict ETagのvalidator/contract/binding照合とCASが一致し、legacy ETag/digest/revisionはbit-for-bit regression比較を通過。旧binary排他はisolated rootで確認し、migrationはproductionで実行しません。strict run-start flagはOFFのままです。
- **rollback:** migration失敗はtransaction rollbackまたは検証済みisolated backup restore。strict runが存在しないisolated fixtureでのみrestoreします。v2 strict dataができた後に旧schemaへ自動downgradeせず、forward repairまたは後続承認済みoffline restoreを使います。

### D1-I4 — Variable store / mutation ledger

- **依存:** I1, I2, I3。
- **目的/成果物:** run-local variables.sqlite schema v1、RunBindingとworkflow root ScopeFrame、declaration/type/sensitivity validation、initialize/assign/increment/decrement/append/object_set/object_delete/variable_delete、execution_key ledgerを実装します。interpreter/OWS callへのwireは含めません。
- **変更面候補:** variable store repository/transaction領域（配置名未確定）とunit/crash-injection test領域。run-local DBだけを扱いdefinitions/requests/dbos/node-event DBへ値を混在させません。
- **検証:** CAS/version/digest、tombstone対JSON null、RFC 6901、required property delete rollback、boolean/number厳密性、NaN/Inf拒否、duplicate replay防止、older historical after replayの後にlatest stateが進んでいるケース、commit前後DBOS crash、aggregate/WAL/ACL cap。
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

- **依存:** I0〜I7全て。
- **目的/成果物:** D1全traceability suite、Windows native ACL/controller/migration/rollforward、crash/recovery、HTTP/MCP dogfoodの証跡とgo/no-go報告を作成します。
- **変更面候補:** 既存pytest/Ruff/mypy/compileall対象、隔離Windows test environment、docs evidence。production/shared state rootやlive Codex実行環境は対象外です。
- **検証:** pytest、Ruff check/format、mypy、compileall、I2/I3/I4/I5のcrash・restore・Windows acceptance、MCP permission/response acceptance。実Codex external jobは個別に明示許可された場合だけ実行します。
- **受入gate:** D1-01〜D1-156とPD-01..PD-39のtraceabilityをpassまたは根拠付きで未達分類し、required Windows/session/ACL/migration gatesを実証。未実施・不明・失敗が一つでもあれば結果はBLOCKED/NOT PASSEDです。pytestのみで運用gateをpassにしません。
- **rollback/go-no-go:** gate未達ならstrict enable、production migration、releaseを行いません。全gate通過後もproduction strict enableには別途ユーザー承認を提案し、自動移行・merge・releaseはしません。

## 4. D1受入ケースtraceability（primary slice）

下表は現行受入ケースD1-01〜D1-146を番号を失わずsliceへ対応させた一次対応表です。範囲内の全IDを含み、sliceは一つ以上でよく、I8では本表全体を最終回帰として再実行します。新規adapter受入ケースD1-147〜D1-156は最後に別行で加えます。

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
| D1-42..50 | I0, I2, I3, I5 | definitions migration、request reservation、manifest/binding |
| D1-51..62 | I4, I5, I7 | missing/corrupt variables DB、reader/replay/MAYBE_SENT |
| D1-63..65 | I0, I3, I5, I8 | backup/restore、migration/downgrade operational tests |
| D1-66..78 | I0, I2, I3, I5 | backup, v1→v2, request migration/schema/old binary |
| D1-79..92 | I2, I3, I4, I5 | physical DB schema, state/ledger, transaction and recovery |
| D1-93..107 | I2, I4, I8 | Global mutex, old-writer gate, NTFS ACL/path/reparse |
| D1-108..120 | I1, I2, I4, I6, I8 | resource limits, cap enforcement, Windows and legacy boundaries |
| D1-121..132 | I0, I3, I6 | public contract wire, CRUD, ETag, legacy GET/list/graph/errors |
| D1-133..138 | I5, I6, I7 | strict start binding/input, output/result, uncertain state |
| D1-139..146 | I6, I7, I8 | HTTP/MCP errors/permissions/caps/client compatibility/CAS race |
| D1-147..156 | I0, I1, I2, I4, I6, I7 | ADR 0007 variable call validation/runtime/replay/regression |

この一次対応表により各既存IDはD1-01からD1-146まで連続しています。新しいD1ケースは末尾のD1-147〜D1-156のみです。D1承認後の実装slice開始時に、各ケースの具体的なtest名/fixture pathを既存test treeに合わせて追加し、IDの重複や再採番をしません。

## 5. Slice運用上の最終状態

I0〜I8はすべてNOT_STARTEDです。I番号は実装分割の提案であり、実装済みPhaseやstrict workflow機能の存在を示しません。レビュー後の各slice開始指示では、そのsliceに必要な変更面・isolated resource・実行可能なテスト・停止条件を明示します。ここに記載した受入やテストは将来の実装計画であり、このdocs-only変更では実行していません。
