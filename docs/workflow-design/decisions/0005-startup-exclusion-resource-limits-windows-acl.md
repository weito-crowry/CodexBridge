# ADR 0005: 起動排他・resource cap・Windows ACL

- **Status:** Proposed（レビュー待ち）
- **対象:** Workflow D1詳細設計
- **関連:** [D1詳細設計](../phases/D1-input-output-variables.md)、[アーキテクチャ](../architecture.md)、[ADR 0004: SQLite DDLとschema migration](0004-sqlite-ddl-and-schema-migration.md)

このADRはD1第4回のcontroller起動排他、Windows ACL/path保護、strict v1 resource capを記録します。D1はDRAFTのままで、本ADRもレビュー前のProposedです。mutex、ACL、resource limitはいずれもコードや運用に実装済みではありません。

## 動機と確定済み事実

SQLite schema versionだけでは旧binaryとの同時writerを止められません。現行requests.sqliteにuser_version管理はなく、旧binaryが新しいversion fieldやlockを認識する保証もありません。definitions.sqliteはuser_version=1で、schema migrationは既存revision/hash/legacy ETagを守る必要があります。

現行の数値上限は次のとおりです。これらは既存仕様・実装の上限であり、本ADRで増加させません。

| 境界 | 現行上限 |
| --- | ---: |
| HTTP API request body全体 | 128 KiB |
| Workflow YAML/source | 64 KiB |
| Codex job prompt | 16 KiB UTF-8 |
| Workflow result | 64 KiB |
| run input file | 2 MiB |
| active Workflow | 同時2件 |
| Workflow task数 | 10,000 |

manager.pyはrun directoryをmode=0700で作成しますが、os.chmodはWindowsで呼ばれません。service.pyのsnapshot作成も同様で、Windows ACLは明示されていません。node_events.pyとtask_projection.pyにはWindows FILE_ATTRIBUTE_REPARSE_POINT検査がありますが、variables.sqliteの全open経路を覆いません。コード内に既存のWindows named mutex、LockFileEx、portalockerによるstate_root writer排他はありません。

## 提案

### 1. 新対応controllerのstate_root排他

新対応Bridgeはstate_rootのDBを最初にopenしたりmigrationしたりする前に、state_rootごとのOS process連動型排他を取得します。primary mutexはWindows Global namespaceのnamed Mutexです。Local namespaceはログオンsessionごとに分離され、同じstate_root/SIDでもRDP、fast user switching、service sessionをまたぐcontrollerを排他できないため採用しません。mutex identityはcanonical absolute state_root pathと現在のuser SIDから安定したSHA-256 identifierを作ります。例となる名前は次の形です。

~~~text
Global\CodexBridge.Workflow.Writer.v1.<sha256-of-state-root-and-user-sid>
~~~

CreateMutexExWにSECURITY_ATTRIBUTESと明示security descriptor/DACLを渡し、dwFlags=0とします。CREATE_MUTEX_INITIAL_OWNERは指定しません。同名Mutexが既にある場合に初期ownerが曖昧になるため、作成時にownerを設定せず、CreateMutexExWの結果とGetLastErrorを確認してからWaitForSingleObject系functionで有限時間待ちます。Mutex objectのDACLは現在のUser SIDとLOCAL SYSTEMだけを許可します。ERROR_ALREADY_EXISTSの場合もobject type、security descriptorのowner SID、DACLを検証し、named object squattingを疑う不一致は拒否します。Global Mutexを作成できない、DACL/owner検証に失敗する、または有限waitで所有権を取得・確認できない場合はstrict enableを止めます。Local namespaceへのfallbackは禁止します。canonicalization、hash input framing、Windows API wrapperはOpen Question 9に残します。

Mutexはthread-ownedです。controllerの寿命中mutexを所有するthreadを存続させ、停止処理とlive worker確認後にその同じthreadからReleaseMutexを呼びます。他threadからreleaseしてはなりません。第二の新対応controllerは有限時間待つかfail closedし、既存processを強制終了しません。owner threadの終了（process終了を含む）でmutexはabandonedになり得るため、WAIT_ABANDONEDなどの結果は単なるlock取得成功と扱わず、request/manifest/DB/workerの状態照合を完了してから再開します。

このmutexは新対応controller同士の競合を防ぐものです。旧binaryはmutexを知らず、後から旧binaryが起動することも止められません。upgrade gateでは旧BridgeのGUI、autostart、service、scheduled task、manual起動経路を停止・無効化し、旧processと未終了workerがいないことを確認します。互換buildのsingle ownerを認証し、requests.sqliteなどに他writerがいないと証明できる場合にだけmigrationまたはstrict v1を有効化します。未知writerや起動元を照合できない状態はfail closedです。mutex単独で旧binaryを遮断できるとは主張しません。

通常GUI/API/MCP clientは共有controllerへ接続し、clientごとにmutexを取りません。controllerがspawnするrun固有workerもcontroller mutexは取得しません。workerはcontrollerから許可されたrun/worker起動identity、request binding、manifestとrun ownerを照合します。operatorや旧processがcontrollerの起動契約を迂回してrecovery workerを作れないguardを設けます。controllerを停止するupgrade時にlive workerが残っている場合はupgradeしません。既存processを強制killする運用を通常の設計に入れません。

Global namespaceの同一Mutexは、同じstate_root/SIDを使うRDP、fast user switching、serviceを含む別ログオンsessionから共有され、controllerを一つに制限する設計です。別session間での同時起動試験を実装時ゲートとします。異なるSIDが同じstate_rootを使う場合はowner/ACL不整合を検出してfail closedします。

### 2. Windows ACLとpath security

strict D1で新規作成するrun directory、variables.sqlite、SQLiteの-WAL/-SHM sidecarはWindows NTFS上のprotected explicit DACLで保護します。作成時にsecurity descriptorを指定し、owner SIDとcanonical ACLを検証します。許可ACEは実行User SIDとLOCAL SYSTEMに限定し、Everyone、Users、Authenticated Users、別SIDからのgrantを認めません。DACLは安全でない親からの継承に依存せず、SQLite sidecarが新規作成されるときは保護済みrun directoryの適切な継承ACEを受けるようにします。

state_root自体の所有SIDとACLも検査します。既存state_rootやlegacy runのACLを自動書換えしません。不適切なACL、owner不一致、ACLを確認できない既存rootではstrict runを開始せず、管理者による明示的な修復手順に委ねます。既存のmode=0700/os.chmodのみをWindows ACL保護と見なしません。

state_root、run directory、variables.sqlite、WAL/SHMおよび該当するancestor pathにsymlink/junction/reparse pointがあれば拒否します。lstatとFILE_ATTRIBUTE_REPARSE_POINT等の属性検査に加え、create-exclusiveとhandle-based owner/file-ID照合を使い、名前衝突やpath swapを検出します。SQLiteがsidecarを作成するため、sidecarのowner/DACL/path検査が完了するまでstrict workerを動かしません。NTFS以外、security descriptor/identity API不在、意図したSIDを得られない場合はstrictをfail closedにし、POSIX/WSL/Dockerへ暗黙に切り替えません。

Windows file ACL/identity raceを完全に排除できるとは主張しません。handleベース検査と隔離Windows dogfoodでの検証を実装時gateにします。ACLは他ユーザーからの誤アクセスを防ぐ境界であり、同一userのrogue process、Administrators、backup privilegeからの保護を保証しません。strict v1のsecret input/variable拒否（PD-10）は維持します。

### 3. strict v1 resource budget（Proposed初期値）

以下はユーザーレビュー前の追加strict上限案です。基本仕様の確定値ではなく、レビュー後に後続設計revisionで調整できます。現行上限は前表のまま維持し、重なる制約はmin(existing_limit, strict_limit)を適用します。

| strict v1対象 | Proposed cap | 検査段階 |
| --- | ---: | --- |
| contract_json_canonical | 64 KiB UTF-8 | definition compile/save |
| normalized input JSONとinput.json | 96 KiB | default正規化後のrun admission |
| output JSON | 64 KiB | success/result確定前 |
| 単一variable value / after_value JSON | 64 KiB UTF-8 | initializationとmutation前後 |
| 単一mutation payload | 64 KiB UTF-8 | mutation admission |
| runあたり現存variable値の合計 | 2 MiB UTF-8 | mutation transaction内の前後 |
| variable declarations | 256 / run | contract compile |
| scope frames | 1,024 / run | frame作成前 |
| mutation ledger rows | 10,000 / run | 新しいledger rowのinsert前 |
| variables.sqlite main DB file | 64 MiB | write admissionとpage/quota検査 |
| variables.sqlite WAL | 16 MiB high-water目標 | checkpoint監視とwriter抑制 |
| JSON Schema nesting depth | 24 | contract compile |
| JSON Schema nodes合計 | 512 | contract compile |
| local $ref展開深度 | 16 | contract compile |
| Schema内object property数合計 | 512 | contract compile |
| input/output/variable JSON depth | 32 | admission、mutation、output |
| JSON object property数 | 1,024 / object | admission、mutation、output |
| JSON array length | 10,000 / array | admission、mutation、output |

API request body全体はcontractを含めて現行128 KiB以下を確認し、contract/YAML等の個別上限だけでは通しません。normalized inputはraw bodyとは別にdefault適用後96 KiB以下か確認します。既存result cap 64 KiB、YAML/source 64 KiB、input file 2 MiB、prompt 16 KiB、active Workflow 2、task数10,000を広げません。入力/output/variable JSON byte数はUTF-8 canonical encoding、すなわちensure_ascii=false、sort_keys=true、separators=(",", ":")、NaN禁止を基準にします。

Duplicate JSON keys、NaN/Infinity、許可されないschema keyword、cyclic $refは拒否します。definition compileでSchema/contract/宣言の上限、request admissionでraw bodyとnormalized input、各mutationで単一値/payload/aggregate/DB/ledger、成功確定前にoutput/resultを検査します。read-only inspectorはpaginationとresponse capを使い全履歴を一括返却しません。MCP/HTTP公開responseの数値capはD1 Open Question 12です。

mutationのaggregate/ledger/database capを超える場合はBEGIN IMMEDIATE内で変更前に検出し、変更なしrollbackとcapacity errorにします。既存値・ledgerを削除せず、Workflow成功・acceptance PASSとして扱いません。main DB 64 MiBはSQLite PRAGMA max_page_count等とlogical quotaを組み合わせる案です。WAL 16 MiBは高水位目標で、上限前checkpointとwriter抑制を行います。長時間readerがcheckpointを妨げる場合があるためcheckpoint/backpressureと単一writer前提を検証します。SHM/WALの別個のhard byte limit、競合時も64 MiB main DB capを保証する方法はOpen Question 11に残します。

## 状態と判断マトリクス

| 状態 | 提案動作 |
| --- | --- |
| Global Mutexの作成不可、DACL/owner検証失敗、有限waitまたは取得owner確認失敗 | strict enableを止める。Local namespaceへfallbackしない |
| 新対応controllerがmutexを取得 | DB open/migrationへ進める。controller停止とlive worker終了までlock保持 |
| 第二の新対応controllerが起動 | 有限待機またはfail closed。既存ownerをkillしない |
| mutexがabandoned/WAIT_ABANDONED | request/manifest/definition/store/worker状態を照合するまで再開しない |
| 旧binaryが起動中・自動再起動可能・live worker残存 | 起動経路を止めて不存在を確認するまでmigration/strict enableを禁止 |
| 旧/未知writerの不在を証明できない | fail closed。mutex/version metadataだけで安全と判定しない |
| 通常GUI/API/MCP client | shared controllerへ接続し、clientごとのmutex取得は行わない |
| controller管理下のrun worker | controller lockを取らず、起動identityとbindingを検証。liveならupgradeを止める |
| state_rootまたはNTFS ACL/owner不一致 | strict runを開始しない。既存ACLを自動修復しない |
| path ancestor/run/db/WAL/SHMにreparse point | SQLiteをopenせず拒否 |
| 非NTFS、ACL/identity API失敗、owner SID不明 | POSIX fallbackなしでstrictをfail closed |
| Proposed resource cap超過 | transaction変更なしrollback、capacity error、成功扱いしない |
| long readerでWAL checkpoint不能 | writer抑制/容量検査を行う。物理上限を確認できない間は確実なquota保証と主張しない |

## Threat modelと限界

| 対象 | 脅威 | 提案する緩和/限界 |
| --- | --- | --- |
| SQLite state | 2つの新対応controllerの同時migration/write | state_root/SID由来mutexをDB open前に保持 |
| SQLite state | 旧binary/autostartがmutexを無視して起動 | 旧起動経路を止め、process/worker不存在が証明できる時だけmigration/strict enable |
| Mutex object | 別security contextによる名前の先取り・不正ACL | create時DACL指定、既存objectのowner/type/DACL検査。不明なら拒否 |
| run data | 別userや親directoryのreparse pointによるpath redirect | NTFS owner/ACL検証、全ancestorとdb/sidecarのreparse check、handle/file-ID照合 |
| run data | 同一user rogue process、Administrators/backup権限 | 保護保証の対象外。ACLを権限境界として過大主張しない |
| Workflow resource | 大きなSchema/JSON、配列/変数大量化、長時間ledger/WAL増加 | stageごとのstrict cap、transaction前check、page/quota/checkpoint案。WAL/SHM物理保証は未解決 |
| secret data | error/log/projectionへの値漏れ | PD-10 secret拒否、errorは値を含めず、MCP/Graphにvariable実値を出さない |

## Test plan

以下はD1実装時の受入計画です。新しいD1受入IDは93から120まで追加し、D1-01..92は保持します。

| D1 IDs | 受入対象 |
| --- | --- |
| D1-93..98 | 同root Mutex競合、同一state_root/SIDで別ログオンsessionのcontroller同時起動、RDP/fast user switching/service session間共有、有限待機、abandoned recovery、旧binary/autostart/未知writer gate、named object DACL |
| D1-99..100 | client/controller/worker境界、controller認証済みrun起動、leftover workerがupgradeを止めること |
| D1-101..107 | NTFS owner/protected ACL、広いACE拒否、legacy ACL不変、全path/sidecar reparse、exclusive create/file-ID、WAL/SHM DACL、ACL API/Filesystem不在 |
| D1-108 | Administrators/backup privilegeと同一user rogue processの保護限界が記述どおりであること |
| D1-109..112 | 現行cap非回帰、body合算、raw/normalized input、output/value/mutation/aggregate/declaration/frame/ledger上限の境界 |
| D1-113..114 | Schema depth/node/ref/property、JSON depth/property/array、duplicate key、NaN/Inf、cycle拒否 |
| D1-115..117 | main DB/WAL/SHM、long reader checkpoint、各段階のlimit検査、mutation rollbackでstate/ledger保持 |
| D1-118..120 | projection pagination/privacy、secret-safe error、既存prompt/time budget維持、外部Codex POSTなし |

Windows ACL/pathの実装受入は隔離NTFS上で行い、handle-based identityとSQLite sidecarのDACLを検査します。docs-only taskではpytest、Ruff、mypy、compileall、実Codex job、MCP dogfoodを実行しません。今回のProposed capを永続設定や既存runへ適用しません。

## Alternatives

| 案 | 判断 |
| --- | --- |
| SQLite user_versionだけで全writerを排他 | 採用しません。旧binaryがversionを検査せず、新Mutexにも参加しないためです |
| mutexを全client/workerが個別取得 | 採用しません。共有controllerがstate_rootを所有し、run workerはcontrollerのrun起動契約に従う責務分離を保ちます |
| stale file lockだけでprocess排他 | 第一候補にはしません。OS process連動mutexでabandonedを検知し、より強いquiescence gateと組み合わせます |
| mode=0700/os.chmodをWindows ACLの代替にする | 採用しません。Windows DACL/ownerを作成元で指定し検証します |
| legacy ACLを自動的に厳格化 | 採用しません。既存ユーザーstateを無断変更せず、明示修復へ委ねます |
| 既存上限をstrict capに合わせて拡張 | 採用しません。strict capは追加条件であり、現行API/YAML/prompt/result/file/concurrency/task capを変えません |

## 未決定事項

Open Question 9はGlobal mutex名のcanonicalization/hash input framing、CreateMutexExWを包むWin32 API、owner threadの寿命と同一threadによるrelease、RDP/fast user switching/serviceをまたぐ排他の実証、controllerの起動/停止統合、旧版起動経路の停止・自動再起動防止、worker起動identityと全writer quiescence証明です。Global作成・DACL/owner検証・取得に失敗した場合はLocalへfallbackせずstrict enableを止めます。Open Question 11はSECURITY_DESCRIPTOR/ACE継承/owner/file-IDの具体実装とdogfood、WAL/SHM個別上限・長時間reader時のcheckpoint、PRAGMA max_page_count/logical quotaでmain DB上限を保証する測定です。

API/status/error/public projectionとresponse cap、backup retention/data loss、secret暗号化、外部副作用の扱いはD1 Open Questions 10、12、14に残します。すべての数値capはProposed初期値で、実装済みや承認済みと扱いません。

## References

- [Kernel object namespaces - Microsoft Learn](https://learn.microsoft.com/en-us/windows/win32/termserv/kernel-object-namespaces)
- [CreateMutexExW function - Microsoft Learn](https://learn.microsoft.com/en-us/windows/win32/api/synchapi/nf-synchapi-createmutexexw)
