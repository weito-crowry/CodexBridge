# D2: 汎用Agent呼び出し・埋め込みプロンプト

Status: DRAFT — 2026-11-26。実装未着手。
Baseline: dev a51203ab4022d212ff62c06b529f17827a2e44fd。
最上位設計原則は [基本仕様](../requirements.md) の2026-11-26合意を適用する。以下の具体的なwire形式・永続化方式・工程はレビュー対象であり、合意済み原則と区別する。

## 1. 目的・非目的

Agentを任意のWorkflowから呼び出し、D1の型付き入力・変数・jq・出力契約・DBOS永続実行と合成する。Issue修正は例題であり、コアにIssue/修正/レビュー専用の条件、カウンタ、予算、実行順序を追加しない。

D2ではCodexを実Providerとし、テスト用Fake Providerで共通契約を検証する。任意の外部Providerを将来登録可能にするが、LLM API、GUI編集、専用判定、汎用ループ、子Workflow、Git操作、予算集計のポリシー機能は実装しない。ChatGPTの現行会話を直接操作できるとは仮定しない。

## 2. 現行実装と境界

- interpreter.py: _validate_subset/_validate_action/compute_codex_job_request/_strict_codex_job_task_identities。旧codex-jobは固定1/2/3パターン、prompt_fromは限定連結。
- strict_executor.py: _strict_task_map/task_provider。authorized_codexのみ既存外部jobを実行。D1 projection checkpointを利用可能。
- worker.py: _run_durable_codex_job、start/poll DBOS steps。旧待機は600 poll、0.2秒、sleep budget 120秒。旧step名・checkpoint形式は変更しない。
- codex_job_adapter.py: prepare_job/claim_post/start_job/observe_job/receipt/shutdown ownership。既存の永続台帳、MAYBE_SENT、exact turnの最終回答取得を再利用する。旧台帳のv1/v2検証・digest・履歴を変更しない。
- http_client.py: loopback UI API、固定create_thread body。ui_control.py: create_threadでmodel/reasoning_effortを能力検証、continue_threadで既存スレッド検証とinactive確認、interrupt APIを提供。継続時のmodel/effort指定は現行API非対応。
- service.py/manager.py/worker_entry.py/worker.py: Codex専用検出、静的期待task identity、全Job成功を要求するterminal projectionとshutdown ownershipがある。新規Agent経路で別の意味を定義し、旧経路の条件を使い回さない。
- definitions.py/strict_manifest.py等: max_live_codex_jobsは1..3の旧wire/DB契約。新Agent経路ではこのフィールドを実行数の予算として使用しない。値域を拡張するための既存DB migrationは行わない。

## 3. ノード定義案

OWS v1.0.3の公式Schemaを変更しない。D1同様にstrict revisionで明示的なcall adapterを使用する。新旧外部Agent呼び出し混在は初期版では非対応とし、旧codex-jobからの移行は明示的なrevision更新で行う。純粋なD1ノードと新Agentは合成可能とする。

    - ask:
        call: codexbridge-agent
        with:
          provider: codex
          target: default
          session:
            mode: new
          options:
            model: example-model
            reasoning_effort: high
          prompt: |
            以下のデータを要約してください。
            {{source}}
          bindings:
            source:
              from: '${ $cbvars.workflow.source }'
              format: json
          response:
            format: text

model値は例示であり実在モデルを保証しない。providerは登録済みID、targetは設定から解決する論理ID。D2のCodex実装はBridge内の既存local targetのみ、未知targetは明示エラー。任意URL、shell、Python class名によるProviderロードは仕様にしない。optionsを省略した場合はProviderの通常の既定動作を利用し、Bridgeはモデルを強制しない。新規cwdはD1の認可済みworkspaceを基準に解決し、権限は既存path/control policyに従う。

session.modeはnewまたはcontinueを明示する。continueにはthread_id（literalまたは明示jq式）を必須とし、Workflow外で作られたthreadも既存APIが許可する限り指定できる。別Workflow所有を理由とした一律禁止は追加しない。ただし現行APIのinactive検証、認可、同時操作制御は維持する。同一threadの古いturnの回答を新しいturnの回答に流用しない。continueで非対応のmodel/effortは無視せず、送信前にunsupported optionとして返す。

provider/target/session/options/response/timeout_secondsの各型を検証し、値評価の有無をフィールドごとに定義する。D2の初期wire案ではprovider/target/session.mode/response.formatはliteral、thread_id、optionsの許可済み値、timeout_secondsはliteralまたは明示jq式。optionsの対応キーはProvider capabilityで検証する。任意のテンプレート評価をYAML全体へ適用しない。

## 4. プロンプトと構造化入力

- promptはUTF-8文字列、{{name}}は明示bindingsの名前（ASCII identifier）を置換する。{{{{はリテラル{{。左から一度だけ解析し、置換値の中の構文を再評価しない。閉じない開始記号、未知binding、未使用binding、重複キーはエラー。通常の単一波括弧は保持する。
- binding.fromはD1のjq式、format=textは文字列のみ、format=jsonは任意の正規JSON値をD1 canonical JSONで直列化する。明示nullはjsonで有効。D1未初期化変数はエラー。jqの一般的なmissingとnullの挙動はD1と整合し、binding評価は厳密な単一結果（zero/multipleをエラー）を要求する。型変換や欠損値生成を暗黙に行わない。
- structured inputはまずtyped bindingによるJSON埋め込みで提供する。Provider共通requestはレンダリング済みpromptと評価済みoptions/metadataを持つ。Codexへネイティブ構造化入力を送れるとは仮定しない。
- 定義時に構文と参照を検証し、実行時に値・型・展開結果を検証する。既存Codex APIの実際の入力上限を調査してProvider制約として適用する。旧PoCの16 KiB制限を汎用Agent共通仕様としない。具体的な通信上限は実装前に既存APIから確定する。
- 値を実行時checkpointに固定し、replayで最新変数から再生成して送信しない。認証情報はbindingsや結果の公開projectionに混入させない。通常prompt/outputをログへ無制限複製しない。

## 5. Provider契約案

共通層は capabilities / prepare / dispatch / observe / interrupt（能力に応じて）を提供する。Providerは認証・transport・remote handleの解釈を担当する。共通層はrequestの固定、論理呼び出しidentity、状態遷移、永続receipt、DBOS orchestrationを担当する。Codex Providerは既存CodexJobAdapterの送信・照合・状態取得ロジックを抽出/再利用し、同一remote呼び出しに旧Job台帳と新台帳の二重書込をしない。

共通requestはcontract version、provider/target identity、session、評価済みoptions、prompt、明示timeout（省略時null）、response specificationを含む。ProviderにはWorkflowの予算判定やレビュー合否の判断をさせない。Provider能力に含める取得可能項目はmodel/effort、session modes、interrupt、usage等。取得不能な利用量を0と偽らずnull/unknownとして表す。対象や設定が回復時に変化した場合に別Provider/別targetへ暗黙に送信しない。

## 6. 結果とエラー

結果はversioned objectとし、invocation_id、provider、target、status、job/remote identity、final_text、data、usage、error、remote_stateを持つ。Codexはthread_id/turn_idを返す。statusはSUCCEEDED、FAILED、TIMED_OUT、UNKNOWNを最低限区別する。送信前の明確な拒否とremote failureはreason codeで区別する。UNKNOWNはMAYBE_SENTや照合不能を含む。タイムアウトはremote終了を意味しない。

response.format=textはfinal_textをそのまま保持。jsonは最終回答全体を厳密JSONとしてparseし、必要に応じてresponse.schemaでD1のSchema制限を再利用して検証する。markdown fence除去、曖昧なJSON抽出、自動修正・再質問は行わない。明示nullとmissingを区別し、値の型はSchemaに従う。不正応答はFAILED/output_invalidとし、取得済みraw responseは保持する。Agentの自己申告は検証済み証拠ではない。

正常に観測できたAgentの失敗・timeout・不確実な送信状態は構造化結果としてWorkflowに返し、Workflowが次の処理を決定する。D3/D5ではこの結果から明示的な分岐・反復を構成する。Workflowの入出力契約違反、台帳破損、binding mismatchなど実行基盤自体の整合性エラーは別に扱い、成功結果に偽装しない。同一論理実行の不明な送信を自動再送しない。Workflowが別の論理呼び出しを明示した場合は別identityとして扱い、実際のProvider認可・状態に従う。

## 7. 回数・待機・予算

新Agent経路に固定の総Job数/同時数/用途別予算を追加しない。D2で保証する実行構造は既存OWSの逐次非ループAgentノード（任意数）とし、Agentを含む既存forを含めた動的反復はD5で一般化する。これはD2実装範囲の境界であって、製品の実行回数ポリシーではない。既存共通のtask resource guardやデータ・通信上限をAgent予算として利用しない。共通制約のWorkflow設定化はD5の検討項目とする。

timeout_seconds省略/nullはWorkflow指定の待機期限なし。指定時は正の有限値を実行開始時に評価・固定し、開始時刻と期限を永続化する。再起動で期限をリセットしない。固定最大値を追加しない。poll間隔、HTTP個別要求timeout等は通信機構の設定であり、Agent実行の期限とは別に扱う。ポーリングは軽量statusを優先し、終端ではexact turnの回答を取得・照合する。DBOS durable sleepとbounded-size observationを用い、長時間動作で応答履歴を毎回複製しない。deadlineなしの回復時も同一invocationを観測する。

timeout時は既定でremoteを中断せずTIMED_OUTを返し、remote handleと非終端状態を保持する。実行基盤が独断で再試行・中断しない。明示interrupt/observe操作のWorkflow公開方法はD2詳細化で確定する。remoteがまだ稼働中のownershipはWorkflowの論理終了と独立して保持する。利用量は取得可能な値と出所を返すだけとし、予算判定・加算・分岐はWorkflow側で記述する。

## 8. 永続実行設計案

旧codex_jobs.sqlite v1/v2、D1 definition/request DB、manifest v2の既存形式は変更しないことを第一案とする。新Agent定義をpinned YAMLから識別し、新規run-local agent invocation ledgerを使う。これは新しい論理呼び出し契約の永続化先であり、既存Codex送信機構を二重実装しない。run format変更が実際に必要ならI0で明示設計してから実装する。旧runの自動移行は行わない。

ledgerはrun binding、invocation identity、node pointer、iteration vector、prepared request/digest、provider/target、client request ID、dispatch state、remote handle、observation、terminal receiptを保持する。identityはrun/workflow binding + node pointer + iteration vectorに基づく名前空間付き決定的キー。providerやpromptを変えた同一identityは新しい送信とせずconflict。明示的な別ノード・別反復は別identity。旧task path文字列の固定形状に依存しない。

初回外部送信前にledgerとbindingを確定し、DBOS checkpointと相互照合する。初期化の順序、予約前の作成、recovery時の欠損検出をI0の永続契約で固定し、送信済み可能性があるledgerの欠損を空DB再生成で隠さない。dispatch直前にPREPARED→MAYBE_SENTをatomicにclaimする。送信receiptを永続化してACCEPTEDへ遷移する。送信が不確実ならUNKNOWNとして保持し、自動再送しない。read-only observeは同一remote handleについて繰り返せる。terminal receiptはimmutable、checkpoint replayは固定requestとreceiptを再検証する。跨DB atomic commitを仮定しない。

論理ノードの結果（timeout等）とremoteの実行状態を分ける。途中でWorkflowが正常に終了しても未終了remoteの記録・ownershipを消さない。外部既存threadに対して所有するのはWorkflowが開始したexact turnであり、thread全体の所有を主張しない。shutdownの既存operator規則との整合、観測だけ行う経路の回復、明示interruptの対象照合をI0で詰める。

## 9. API/Manager/Workerと互換性

新Agent経路はstrictのみ。既存strict runtime/controlの認可を維持し、Agentの外部実行は明示的なenablementに従う（追加flagの要否はI0で確定）。旧flagsを勝手に再解釈しない。validatorからdefinition reader、worker entry、manager recovery、worker、service projection、shutdown ownershipまで一貫したAgent分類を利用する。文字列検索でprompt内のcallを誤認しない。

旧codex-jobの固定max_live_codex_jobs、旧checkpoint、旧result/projectionはそのまま維持する。新Agent経路は旧1/2/3パターン、全Job SUCCEEDED要求、固定3件の投影を適用しない。Workflowの成功はWorkflow自身の制御・D1 output contractで決まり、正常に扱われたAgent failureを理由に基盤が一律失敗へ上書きしない。Ledgerの所有権・binding/receipt整合性は別途必須。条件によって実行されないnodeを欠損Jobとみなさない。新規projectionはbounded pagingと非機密metadataを使い、既存API shapeは互換的に維持する。既存result上限を超える大きな成果物参照は将来の別契約とし、黙って切り詰めない。

## 10. 実装工程案

- I0: 具体wire/validation、ledger DDL・identity・replay・ownership、明示interrupt/observe、実行認可、技術上限、公開projection契約を確定。実コードの依存点とテスト対応を設計文書に記録。大規模な設計判断をCodexへ委任しない。
- I1: 純粋なprompt parser/binding評価、型・エラー・UTF-8テスト。副作用なし。
- I2: 共通request/result/provider capabilityとFake Provider、JSON応答Schema検証。
- I3: 新Agent ledgerとdurable executor、新規Codex dispatch/observe。既存transportと状態機構を再利用。送信境界のcrash/replayテスト。
- I4: strict validator/worker/manager/service/ownership統合、任意数の逐次Agent、期限なし/明示期限、結果分岐、投影。旧run互換。
- I5: 明示thread継続、モデル/effort、明示操作、usage capability。既存threadの認可・inactive・exact turn検証。
- I6: 統合テスト、隔離回復、実機dogfood、運用受入。

各工程で必要なテストのみ実行し、統合時に全pytestを選定する。実機導入は明示承認後。既存未commit/未追跡データ、実DBを勝手に変更しない。Codexは単一agent、現在の安全なcheckout、通常branch、commit/pushまで。ChatGPTが設計・レビュー・受入を担当する。

## 11. 受入シナリオ

最低限: ①単一Agentで非IssueのJSON要約、②D1変数を複数位置に展開、③4個以上の逐次Agent（旧max_live_codex_jobs=1でも新経路で成功）、④明示既存threadの継続とexact turn、⑤model/effort対応と非対応拒否、⑥明示期限/期限省略/期限のrecovery、⑦remote継続中のtimeout・ownership、⑧失敗/UNKNOWN/不正JSONをWorkflowで扱う、⑨同一invocation replayで重複なし・別invocationは別送信、⑩送信前/送信直後/receipt後のcrash、⑪旧legacy/strict pure-variable/strict codex-jobと共存。Fake/隔離で異常系を網羅し、実機では短い非破壊Agent、複数呼び出し、継続、明示timeout等の必要範囲を確認する。

D1受入は維持する。Issue修正の縦断受入はD4以降。D2だけでIssue専用Workflowの完成を要求しない。

## 12. 未確定事項（I0）

既存UI APIの実際のprompt/response capとstatus取得経路、model capability、usageの取得可能範囲、target identity固定方法、ledgerの物理DDL/初期化/recovery、DBOSの長時間pollの永続量、明示interrupt/observe公開契約、結果schemaの制約、設定flag/公開reason codeを確認・確定する。これらを未検証のまま実装済みと扱わない。
