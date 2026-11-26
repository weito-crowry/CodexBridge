# D2-I0: Agentノード実装契約 v1

Status: DRAFT（詳細設計・実装レビュー用） / 2026-11-26
Baseline: dev a51203ab4022d212ff62c06b529f17827a2e44fd
Parent: [D2概要](D2-agent-nodes.md)。本書はI0の具体仕様であり、親文書の提案・未確定事項と矛盾する場合は本書を優先する。実装済みを意味しない。

## 1. 実装範囲と互換性

PolicyはWorkflow、MechanismはCodexBridge。新規経路のAgent総回数、待機時間、予算、再試行、thread新規/継続、停止を固定しない。D2は任意数の静的な逐次Agentと既存の非循環制御・D1変数を合成する。Agentを反復配下に置く一般化はD5の実装範囲。D1の汎用task resource guard、同時Workflow・workspace排他、既存入出力データ上限は今回変更しない。これらは新しいAgent予算ではないが既存基盤の制約として残る。将来の汎用設定化は別工程。旧codex-job経路の実行条件、DB、checkpoint、API結果形は維持。

新Agent定義はstrict contract必須、旧codex-job/mock-jobとの混在はD2非対応。新経路はmax_live_codex_jobsを使用しない。既存DB/wireの必須値1を互換sentinelとして保持（新経路では2/3を旧専用引数として拒否）。1はAgent数上限ではない。公式OWS Schema無変更。saved revisionのsource/canonical hash、contract binding、manifest v2/request digestを再利用し、pinned task treeから新経路を識別する。旧readerが未知callを実行しないことを試験する。既存revisionを移行しない。

## 2. ノードwire仕様

call: codexbridge-agent
with は閉じたobject。operation省略時はinvoke。共通: provider（登録済みASCII ID）、target（登録済み論理target ID）、operation（invoke/observe/wait/interrupt）。provider/targetはliteralで指定（動的選択は後続拡張、現時点でもWorkflow switchで選択可能）。未指定のtargetはdefault。対象providerの設定された既定targetへ解決する。D2の実Providerはcodex、targetはdefault/local Bridgeのみ。Fakeはテスト注入限定。任意URL、shell、import名をProvider指定に使わない。

### invoke

必須: session、prompt。任意: bindings（既定{}）、options（既定{}）、response（既定{format:text}）、timeout_seconds（既定null）、wait（既定true）。

session: {mode:new} または {mode:continue, thread_id:STRING_OR_EXPR}。newでthread_idを許可しない。continueでは明示thread IDを必須とする。外部作成threadも既存control APIの認可・inactive条件に従って利用できる。所有権は実際に開始したturnに対して記録する。cwdはD1の検証済みworkspace（newのみ）。continueでは既存threadのworkspaceを既存APIで認可する。許可されないcwdやthreadを利用しない。

optionsはprovider固有の明示キーのみ。Codex new: model, reasoning_effort, title, sandbox_mode, confirm_full_access。modelとeffortは現行capabilitiesで検証、effort単独は拒否。sandbox_modeはinherit/danger-full-access、後者はconfirm_full_access=trueと現行operator認可が必要。省略時は現行APIの動作。title省略時は呼び出しを照合できる生成titleを利用してよい。continueはこれらをサポートしないため指定時は送信前にFAILED/unsupported_option。attachmentsは初期版の契約外。任意のoptionを透過送信しない。

response: {format:text} または {format:json, schema?: JSON_SCHEMA}。formatはliteral。schemaは定義時に検証するliteral object。textにschemaを指定しない。

wait=falseはdispatchの確定receiptでノードを返す。追加のremote待機をしない。timeout_secondsはwait=trueのinvokeまたはwait operationでのみ指定可。通信timeoutと区別する。

### observe / wait / interrupt

provider/targetとhandleを指定する。handleは {thread_id,turn_id}（Codex）を明示するか、直前のresult.handle等をjq単一評価で渡す。Provider-neutral層はJSON objectとして扱い、Providerが完全性・正規形を検証する。任意の既存handleの明示指定も、Providerの認可・正確なturn照合に従って許容する。過去のthread全体を自動対象にしない。

observeはread-onlyの一回観測、waitはread-onlyの繰り返し観測（timeout_seconds任意、既定null）、interruptは明示的な一回の中断要求である。observe/waitでresponseを省略するとtext、JSON検証が必要なら明示する。interruptにはresponse/timeout_seconds/session/prompt/bindings/optionsを許可しない。read-only operationで新規turnを起動しない。interruptがackされた場合も実際のremote終了を推測せず、ACCEPTEDとremote stateを返す。interruptの不確実送信はUNKNOWN、同一論理操作を自動再送しない。

初期I3ではinvoke/new/wait=trueのみ実Providerへ接続。I4でwait=false/observe/wait、I5でcontinue/interruptを接続。未実装operationは定義時またはrun admissionで明示拒否（Fakeテストは除く）。unsupported指定を黙って無視しない。

### 動的値

thread_id、handle、options各対応値、timeout_seconds、waitはliteralまたは既存OWS全体jq式 ${ ... } の一回評価。session.mode/provider/target/operation/response.format/schema/promptはliteral。bindings.fromのみ必須jq式。文字列の一部にjq式を暗黙展開しない。literal文字列が式と同形の場合はjqの文字列リテラル式で明示表現する。unknown key、boolを数値扱い、duplicate YAML/JSON keyを拒否する。

## 3. Prompt / binding

name = ASCII [A-Za-z_][A-Za-z0-9_]*。{{name}}で対応するbindingを一回展開。{{{{をliteral {{ とする。左から走査、{{{{を優先認識、{{name}}のみplaceholder。未閉鎖・不正name・未知binding・未使用bindingを定義時に拒否。通常の単一波括弧と単独の }} はliteral。置換結果は再走査しない。bindingは{from:jq式,format:text|json}（両必須）。同一bindingは一回評価して複数箇所で共有する。textは文字列のみ。jsonはD1 canonical JSONを利用（文字列の引用、null、true/false、配列/objectを保持）。テンプレート末尾の改行、空白を勝手に削除しない。最終promptの空/空白のみ、不正Unicodeを拒否。

jqは既存のstrict cbvars rewriteとphase projectionを使用し、Agent用に単一結果を要求する評価ヘルパーを用意する（現行evaluate_jqのzero→null / multiple→arrayへの正規化は既存経路に残す）。jqが明示的に返したnullは有効なJSON値、zero/multipleと区別。未初期化D1変数は既存ガードに従う。一般的なjqのmissing→nullは既存意味を維持する。Agent専用operation expression collectorでbinding.fromと動的fieldだけを抽出し、prompt本文やschemaを誤ってjqとして検査しない。

preparationはDBOS named stepで、pinned definition、D1 operation phase projection、task input digest、variables digest、rendered requestを固定する。replayで同一入力のcheckpoint/ledger digestを照合し、最新変数によって再レンダリングして送信しない。外部送信前の構文・値・型・capability検証失敗は安全なreasonで返し、プロンプト原文を診断ログに出さない。定義検証時に判明するエラーはdefinition invalid。

## 4. 技術上限（現行コード調査）

2026-11-26 dev:
- ui_control.py _MAX_BODY_BYTES=64 KiB、_MAX_PROMPT_CHARS=32000。new/continue共通。新Codex Providerは実際のUTF-8 JSON bodyを送信前にserializeし64 KiB以下、promptは32000 Unicode文字以下。JSON envelopeも算入。旧validate_codex_job_promptの16 KiBは新経路へ流用しない。Provider共通のprompt文字数上限は設けない。
- http_client.py default max_response_bytes=65536、設定可能な技術上限1 MiB。新経路はtransport上限1 MiBで厳密検査、要求上限はUI APIの64 KiBを別途適用。read timeoutは現行の通信設定（初期5秒）を再利用、Agent期限とは別。
- status endpointはturn_id指定可能。turns/itemsはcursorページング対応、itemsはturn_id filterあり。稼働中はstatusのみ、終端はexact turnを確認しitemsを必要な範囲でページング。上限超過・履歴欠落・不一致は明示UNKNOWN/response_unavailable等、古いturnを採用しない。cursor進行を検証し、同じcursorを無限取得しない。
- 既存D1 Workflow最終resultの64 KiB制約、定義64 KiB、入力96 KiB、変数/契約の既存制約は維持。新Agent応答全体を黙って切り詰めない。大きな応答はrun-local receiptに保持し、Workflow output/exportで必要部分を選ぶ。Provider transport上限を超えたデータの取得成功を主張しない。巨大成果物の専用参照方式は将来課題。
- Worker既定timeout 120秒（worker.py _parse_args と _wait_for_strict_result）。新Agent runでは外側worker待機も期限なしにし、旧runの120秒は変更しない。DBOS sleepとshutdown signalを使う。API client/startup/worker shutdownなどの通信・プロセス管理timeoutは実行期限ではない。

## 5. Provider interface

型をimmutable dataclass/Protocolで定義し、versioned JSON serializerを用意する。名称は以下を標準とする。

capabilities(target) -> ProviderCapabilities
prepare(request, target_context) -> PreparedProviderRequest (外部変更なし)
dispatch(prepared, client_request_id) -> DispatchReceipt
observe(handle, target_context) -> Observation
interrupt(handle, target_context, client_request_id) -> DispatchReceipt

capabilitiesはsession mode、対応option、operation、利用量対応、transport制約。prepareは外部副作用のない正規化/検証。認可preflightはdispatch claim前に実行可。dispatch/interruptは同一claimで最大1回呼ぶ。明確な送信前拒否だけFAILED、POST後の曖昧なエラーはUNKNOWN。既存control request registryのメモリ保持（最大128）を耐久的な冪等保証とみなさない。

target identityはprovider ID + target ID + protocol contract version + canonical endpoint + admitted workspaceに固定。D2 Codexはloopback/port、認可済みworkspace。tokenは環境から使用し永続化しない。プロセスPIDは再起動で変化するためidentityにしない。同じlogical endpointの再起動を許容し、remote handleを照合する。構成変更で別endpointへ暗黙に送信しない。初期run-local bindingにtarget contextを固定する。将来他targetの識別はProvider固有。現在のUI APIに永続server instance IDが確認できていないため、同じportに別のBridgeを起動した場合の完全な識別保証はない。未知remoteは再送せずUNKNOWN。

Codexは既存http_clientの認証/JSON transport、codex_job_adapterのstate mapping/final-answer extraction/receipt原則を抽出・再利用。旧adapterのv1/v2台帳を変更して新台帳と二重書込みしない。UI API既存 create_thread / threads/{id}/turns / threads/{id}/turns/{turn}/interrupt を使用。

## 6. 共通結果 v1

常に固定keyを返す。取得不能はnull、空objectや0に偽装しない。

    {
      "version": 1,
      "invocation_id": "deterministic-key",
      "operation": "invoke",
      "provider": "codex",
      "target": "default",
      "status": "SUCCEEDED",
      "handle": {"thread_id":"...","turn_id":"..."},
      "remote_state": "completed",
      "final_text": "...",
      "data": null,
      "usage": null,
      "error": null
    }

status: ACCEPTED / RUNNING / SUCCEEDED / FAILED / TIMED_OUT / UNKNOWN / BLOCKED / INTERRUPTED。
- SUCCEEDED: 対象turnがcompleted、final answer取得済み、指定output検証済み。
- FAILED: 明確な拒否、remote failed、応答検証失敗。remote成功と論理失敗をremote_stateで区別。
- TIMED_OUT: 明示された待機期限を超過。remote未終了の可能性を保持。
- UNKNOWN: 送信不明、handle/終端証跡不足、取得不能。再送許可ではない。
- BLOCKED: needs_approval/needs_input。自動承認・入力を行わない。
- ACCEPTED/RUNNING: nonterminal remoteが確認できた。dispatch-only/observeで返せる。
- INTERRUPTED: 対象turnの実際の中断を確認済み。
interrupt ackはACCEPTED。ackと実際のremote終了を混同しない。

errorはnullまたは{code,message}。codeの初期集合: invalid_request, unsupported_provider, unsupported_target, unsupported_operation, unsupported_option, unauthorized, request_too_large, preflight_failed, dispatch_rejected, dispatch_uncertain, observation_unavailable, response_unavailable, remote_failed, remote_interrupted, needs_approval, needs_input, timeout, output_invalid, output_too_large。messageは安全な固定文、秘密/生prompt/生HTTP bodyを含めない。receiptの検証失敗、ledger欠損、binding mismatch等はAgent resultではなくstrict_execution_uncertain/run_integrity_invalidに分類し、Workflow整合性エラーを成功に変換しない。

text: data=null。json: 最終回答全体をparse_strict_jsonで解析し重複key・nonfinite等拒否、D1の制限付きSchema validatorをroot任意型で共用する。schema指定時はD1の許可語彙・ローカルref・resource制限を適用しdefault補完はしない。raw final_textは検証失敗でも保持する。JSONのnullはdata=nullでもstatus/response仕様で成功と区別。response format/schemaは論理操作に固定。Providerが構造化応答をnative強制することは要求しない。

usageはnullまたは{input_tokens,output_tokens,total_tokens,source}（取得不能な個別値はnull）。現行UI APIにはrate-limits取得があるが、exact turnのtoken usageを保証できる経路は本調査で確定していない。I5で実装経路を検証し、信頼可能なexact turn usageのみ返す。rate limitの消費を個別turn tokenと混同しない。予算設定・計算・停止判定はWorkflow側で行う。

## 7. run-local ledger

filename: agent_invocations.sqlite、user_version=1。D1 requests/definitions/variables/DBOS既存schemaは変更しない。new runのsnapshot生成後、request reservation前に初期化（service._admit_strict_saved_run）。snapshot由来の完全binding identityとtarget contextを格納し、同一DB内でschemaと初期bindingをtransaction commit。予約gateで確認。reserved runでは必須ファイルとして読み取り検証し、消失・破損時に再作成しない。予約前の孤立runは従来通り自動復活させない。旧runでは新ledgerを要求しない。通常readerがDBを生成しない。path/regular file/Windows reparse policyはD1 storeと整合。

物理スキーマ初版（型・制約は固定、timestampはUTC epoch ms INTEGER）:

    CREATE TABLE run_binding (
      singleton INTEGER PRIMARY KEY CHECK(singleton=1),
      binding_json TEXT NOT NULL,
      binding_sha256 TEXT NOT NULL,
      target_context_json TEXT NOT NULL,
      target_context_sha256 TEXT NOT NULL
    );
    CREATE TABLE invocations (
      invocation_id TEXT PRIMARY KEY,
      node_pointer TEXT NOT NULL,
      iteration_json TEXT NOT NULL,
      operation TEXT NOT NULL CHECK(operation IN ('invoke','observe','wait','interrupt')),
      request_json TEXT NOT NULL,
      request_sha256 TEXT NOT NULL,
      client_request_id TEXT UNIQUE,
      dispatch_state TEXT NOT NULL CHECK(dispatch_state IN
        ('NONE','PREPARED','MAYBE_SENT','ACCEPTED','REJECTED')),
      dispatch_receipt_json TEXT,
      dispatch_receipt_sha256 TEXT,
      created_ms INTEGER NOT NULL,
      deadline_ms INTEGER,
      result_json TEXT,
      result_sha256 TEXT,
      UNIQUE(node_pointer,iteration_json),
      CHECK((result_json IS NULL)=(result_sha256 IS NULL)),
      CHECK((dispatch_receipt_json IS NULL)=(dispatch_receipt_sha256 IS NULL)),
      CHECK((operation IN ('observe','wait') AND dispatch_state='NONE'
        AND client_request_id IS NULL) OR
        (operation IN ('invoke','interrupt') AND dispatch_state<>'NONE'
        AND client_request_id IS NOT NULL)),
      CHECK(deadline_ms IS NULL OR deadline_ms>=created_ms)
    );
    CREATE TABLE owned_handles (
      invocation_id TEXT PRIMARY KEY REFERENCES invocations(invocation_id),
      provider TEXT NOT NULL,
      target_identity_sha256 TEXT NOT NULL,
      handle_json TEXT NOT NULL,
      handle_sha256 TEXT NOT NULL,
      remote_state TEXT,
      observed_ms INTEGER,
      remote_terminal INTEGER NOT NULL CHECK(remote_terminal IN (0,1)),
      UNIQUE(provider,target_identity_sha256,handle_sha256)
    );

Application validator enforces canonical JSON/digest、exact key/schema、UUID/ID形式、pinned node pointer、operation/provider/target整合、run binding、state/receipt整合、owned_handleのinvoke origin。root singleton以外なし。FK ON、短いBEGIN IMMEDIATE transaction、既存D1 SQLite durability/backup policyに合わせる。immutable fields/result/dispatch receiptは一度だけ設定、更新CAS。既存結果があればbyte-equivalent canonical dataを要求。dbos-owned schemaは変更しない。state更新とowned_handlesの初回挿入は同一transaction。read-only inspectionはrow/paging単位で検証、全DBを無制限にAPIへ返さない。技術的SQLiteエラーは実行基盤異常。

invocation_id=SHA256(canonical JSON {namespace:"codexbridge-agent-v1",binding:checkpoint_identity,node_pointer,iteration_vector}) のhex。client_request_idは"cba-"+invocation_id（68文字。ui_control.pyのASCII ID制約8..100文字に適合することを確認済み、回帰テスト対象）。同じnode/iterationの異なるrequestはconflictで別送信しない。distinct node/iterationは別論理呼び出し。operationやproviderをidentityへ追加して意図しない別送信を作らない。D2ではAgent loop不可だがidentityはiteration対応。

prepared request JSON: version, operation, provider, target, target_identity, session/handle, evaluated options, rendered prompt, response, wait, timeout_seconds（不使用fieldはnull）。deadlineはledger列に保存。D1 phase/task input/variables digestと照合するDBOS prepare checkpointにcanonical request digestを含める。secretの永続化禁止（control token、API認証情報を含めない）。

## 8. State machine / replay

1. Verified reserved run + D1 phase projection → prepare named step。ledgerにPREPAREDまたはNONEをinsert。外部送信なし。既存rowならidentity/digestを照合。
2. dispatch前に期限・認可・prepare結果を確認。期限超過で未送信ならTIMED_OUT、remote handle null。claimをPREPARED→MAYBE_SENTにatomic CAS。claim commit後にPOST。
3. 確定receiptならACCEPTED/REJECTEDへCAS。invoke acceptedはexact handleをowned_handlesへ同一transactionで保存。interrupt receiptは新規所有権を作らない。応答不明はMAYBE_SENTを保持してUNKNOWN result。claim取得後のcrash（送信前/後が判別不能）も自動再送しない。
4. observeはread-only。同一handleを再照会可能。dispatch済みinvokeはPOSTせずobserveへ進む。acceptance checkpoint欠落でもledgerの確定receiptを照合して利用する。既にresultがある場合はその結果を返す。replayは最新remote状態で過去の論理結果を書換えない。
5. waitはDBOS durable sleep + status observation steps。各pollのcheckpointは観測時刻・identity・handle digest・remote state等の小さいmetadataを保持する。最終回答は必要時のみ取得、immutable result/receiptに保存。poll indexは決定的に進める。既定5秒間隔は通信機構であってtimeoutではない。無期限waitでpoll数の固定上限を設けない。DBOSのcheckpointは時間に比例して増えることを認識し、巨大履歴を反復保存しない（圧縮/保守は別課題）。
6. timeout_secondsは正の整数秒またはnull（bool不可、epoch msのSQLite INTEGERで表せない値のみ拒否）。初回prepare時刻とdeadlineを永続化する。再起動で期限を延長しない。durable observationの時刻で判定し、deadline後に初めて観測した完了は待機期限内の完了と推定しない。期限到達時はTIMED_OUT（送信の不確実性があればUNKNOWN優先）、自動interruptしない。送信済みhandleを保持し、後続observe/wait/interruptが可能。deadlineなしは完了/明示の非終端結果/障害/shutdownまで待つ。
7. resultを一度だけ書込、DBOS checkpointでdigest/identity/ledger rowを照合。checkpointのみ存在しledger欠損/不一致なら整合性エラー。logical resultとremote状態の変化を分離する。後続の明示observe/waitで所有済みhandleのremote stateを更新可能だが過去resultは不変。
8. 新規のWorkflow invocationにおける同一threadのcontinueは別turn。明示handleが外部作成turnならowned_handlesに登録しない。既存handleを更新する際はprovider/target/handleの完全一致が必要。

## 9. Runtime・API・設定

共通classifierは実際のtask treeのcall targetのみを分類する。legacy/strict pure/strict codex-job/strict agentを分ける。definition validate/read、strict executor、service admission/replay/recovery、manager/worker config、worker entry、worker CLI、shutdown ownership、projectionで同じ分類を利用。既存のrecursive generic dict searchでprompt/schemaのcall文字列を誤検出しない。旧挙動変更は必要箇所に限定する。

新設定 [workflow] strict_agent_enabled = false。strict_runtime_enabled=trueを前提とする。旧strict_codex_jobs_enabledは旧経路のまま。新flagを明示enableした場合のみ新Agentの外部操作を許可（既存control token/allowed roots/Provider auth必須）。config.py/config_file.py/sample config/manager/service/worker env・recoveryに一貫配線。flag無効でも保存済みrunのread-only inspectionは可能。無効化後の新しいdispatch/recoveryによる外部操作は拒否し、既存結果を消さない。実DB移行不要。operator enableは運用受入時に別途実施。

worker config version2にauthorized_agent execution_modeを追加し、旧shapeは保持。new pathではworker全体timeoutなし、旧CLIの120秒は旧runにのみ適用。worker entryで検証済みmode/portを渡す。運用shutdownはoperatorによる停止でありWorkflow timeoutではない。remote turnの所有情報を失わず、既存のBridge全体shutdown規則と接続する。既存のmanager._finish_shutdownは現プロセスのrecordに加えpersisted runを列挙するが、_persisted_live_run_directoriesはstarting/running/needs_reconciliationのみに絞り最大101行を読むため、再起動後の論理終了済みrunに残るremoteを取りこぼし得る。旧経路は維持し、新Agent所有権については全reserved strict runを安定順のread-onlyページで列挙し、pinned revisionからAgent分類、binding/ledger検証を行う。論理terminal stateで除外しない。固定100件で切り捨てない。初期版は既存requests DBのschema変更を避けるためこの走査を用い、履歴増加による性能改善は別途検討。読み取り不能・不完全走査はownership_complete=false。論理終了済みrunでもremote nonterminal所有を収集。MAYBE_SENTでremote不明ならownership_complete=false。既知exact owned turnを別のthread全体に拡張しない。

ServiceはAgent ledgerを旧codex_jobs.sqliteと別に読む。各Agent resultのstatusはWorkflow terminal statusではない。正常なWorkflow returnでD1 output contractに合致すれば、扱われたFAILED/UNKNOWN/TIMED_OUTが存在してもWorkflow succeeded可。欠損/破損したledger・不正binding・terminal evidence不一致はneeds_reconciliation。実行されなかった条件分岐nodeは欠損とみなさない。Agent ledgerが必要なrunでは初期空ledgerも正当。

既存run/status/detail/result shapeは変更しない。Agent metadataを必要に応じてoptional summaryとして拡張するが既存jobs欄へ異種結果を偽装しない。新規read-only invocation list/detailはI4で実装する: GET /ui-api/control/workflows/{workflow_id}/agent-invocations と /{invocation_id}（既存workflows/api.pyのworkflow_id経路・control認可に準拠）。listはlimit既定50・最大100、opaque cursor（created_ms,invocation_id）順、1ページずつ。metadataはoperation/provider/target/status/handle/remote_state/usage/error code/created_ms/deadline_ms。prompt/raw request/raw answerは通常公開しない。詳細も同じmetadata + result digest。Workflowが明示exportした結果は既存result APIから取得する。新MCP管理・GUI編集はD8で追加。既存strict terminal envelope version1は維持。

## 10. 実装分割・テスト

I1 (pure): agent_prompt.py。parser、binding検証、typed rendering、jq strict-single-result helper、expression collector。test_workflow_d2_i1_prompt.py。UTF-8、literal、複数差込、duplicate/unknown/unused、null/zero/multiple、非再帰、D1未初期化、旧jq動作維持。副作用/DB/API変更なし。

I2 (pure): agent_contracts.py / agent_provider.py。wire validator、prepared request、result、Provider Protocol、Fake、Schema共用。test_workflow_d2_i2_contracts.py。未知field、capabilities、response parse/error、raw保持、usage null、unsupported option。既存D1 validator挙動維持。

I3: agent_ledger.py / agent_executor.py / codex_agent_provider.py、既存http_client/adapterから最小限共用。DDL、CAS、初期binding、replay、new invoke。Fakeと隔離DBOS crash matrix、send失敗、exact turn、body limit、no double POST、deadline。production未接続。

I4: classifierとstrict admission/worker/recovery/service/manager/flags/ledger projection。wait=false/observe/wait。4個以上のAgent、skip分岐、failure/UNKNOWN handling、timeoutなしで120秒を超えるFake、旧API/旧DB/旧run/旧test、shutdown ownership、DBOS recovery。外部Agent利用の既定OFFを確認。

I5: continue/interrupt/model/effort/options、exact handle・外部既存thread、inactive/権限・unsupported、usage capability確認。必要ならbounded final-answer routeを最小追加（実際の履歴取得制約の証拠がある場合のみ）。

I6: 統合pytest、Ruff/mypy/compileall、Windows native隔離と明示opt-inの実機dogfood。非Issue例題、複数Agent、外部thread継続、timeout+observe、interrupt、JSON検証、回復、既存D1 smoke。D1全受入のやり直しはしない。

I1開始時はユーザー合意済みの汎用原則と本I0設計を仕様として提示し、単一Codex、no escalation/delegation/worktree、既存変更保護、該当pytest/Ruff、commit/push、ChatGPT review。各sliceのコード変更は別実装branchで管理し、設計文書のマージと本番有効化を混同しない。

## 11. 残る検証事項

実行前に確認を要する事項: 実機UI APIでのexact turn history paging/最大応答、同一endpointのidentity限界、exact turn usageの取得可否、DBOS長時間checkpointの増加量、I4のroute実装・認可回帰、operator shutdown時の未終了remoteの挙動。これらは設計で不明時の挙動を決めているためI1/I2を止めない。実機確認前に対応済みと主張しない。I0で新規の数値予算やIssue特化ノードは追加しない。
