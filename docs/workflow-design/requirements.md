# Workflow基本仕様 v0.2

## 状態と適用範囲

本書は、チャットで合意したWorkflow基本仕様 v0.2を要件として記録します。ここに記す基本方針は合意済みです。Schema sidecarの保存キー、数値上限、細かな既定動作など、実現方法の詳細までは確定していません。

D1の具体設計は [D1詳細設計DRAFT](phases/D1-input-output-variables.md)、現行コードとの差分は [architecture.md](architecture.md)、構造化ループ方針は [ADR 0001](decisions/0001-structured-loops.md) を参照してください。

## 最上位設計原則（2026-11-26 合意）

CodexBridge Workflowは、特定業務に特化しない汎用の永続Workflow実行基盤とする。Issue修正Workflowは必要機能を検討するための参照ユースケースであり、最終的な統合受入シナリオの一つである。これを実現できることは必要条件だが、コア実装をIssue修正専用にしてはならない。

- PolicyはWorkflow、MechanismはCodexBridgeの責務とする。
- Agent/Provider/モデル/推論強度、スレッド新規作成・継続、実行回数、待機期限、予算、再試行、終了条件、エラー分岐はWorkflowが明示的に決める。新規経路へ用途由来の固定回数、既定待機期限、固定予算、暗黙の再試行・中断を持ち込まない。
- CodexBridgeは実行、状態・結果・利用量（取得可能な場合）の提供、永続化、障害回復、同一論理実行の意図しない重複送信防止を担う。Providerの能力・認証・アクセス権限およびデータ整合性に由来する技術制約は保持する。
- 汎用ノードの合成を優先する。Issue、Attempt等は参照ユースケースにおける論理スコープの意味付けであり、将来の構造化スコープは任意のループ・ブロックを対象とする。D1受入済みの契約・変数実装を再開発せず、D5/D6で拡張する。
- 以下のIssue修正手順・OK/NG/UNKNOWN・反復上限・停止規則は参照ユースケースの方針であり、全Workflowに強制する既定動作ではない。UNKNOWNは同一論理呼び出しの自動再送許可ではない。Workflowが明示する別の論理呼び出しとは区別する。
- 新規仕様の汎用化を理由に旧Workflowの保存済みrevisionや意味、既存互換経路の制約を黙って変更しない。

## 目的と入口入力（参照ユースケース）

Workflowは、Issue修正、Git操作、テスト、レビュー、判定、結果集計を、再開可能な構造化フローとして記述・実行できるようにします。

Issue修正Workflowの入口では、対象Issue配列と、配列内の各Issueに対応付く修正設計書および受入条件がそろっている必要があります。

1. 処理対象Issueの配列
2. 各Issueに対応付く修正設計書
3. 各Issueに対応付く受入条件

Issueと設計書・受入条件の対応関係を保ったまま共通入力contractに渡します。具体的なJSON形状・変数名はcontractで定義します。値の欠落と明示的なJSON nullは区別し、入口で不足を黙って作りません。

## 制御構造

- 通常のIssue修正は **Forを外側、Repeat-Untilを内側** にした構造化ループで表します。
- Repeat-Untilの本体を最低1回実行してから終了条件を評価します。
- 受入判定がOKになったら、Breakで内側の修正反復を終了し、そのIssueの結果をACCEPTEDにします。
- 判定がNGならレビュー情報を保存し、次のRepeat-Until反復で修正を続けます。
- 判定がUNKNOWNなら成功・失敗のどちらにも変換せず、安全に停止します。UNKNOWNになった処理を自動再送しません。
- 反復上限に達したIssueはREJECTEDとし、定めた停止規則に従って処理を止めます。上限値と停止範囲の詳細は後工程で決めます。
- For、While、Repeat-Untilを制御構造として扱います。BreakとContinueの対象は、字句上もっとも内側にあるループだけです。
- 新規仕様としてGoTo式の任意ジャンプや後方ジャンプを追加しません。既に存在するジャンプ表現は保存済みWorkflowとの互換性のために維持します。

## Issue修正サブフロー

各Issueの修正反復では、以下を明示的な順序で実行できるようにします。

1. Workflow、Issue、Attemptに属する変数を初期化します。
2. Agentノードが変数を埋め込んだプロンプトを使ってCodexへ修正を依頼します。
3. Git差分などの変更を対象にテストを実行します。
4. 機械判定、Agent判定、または両者を使ったCombined判定を行います。
5. OKならACCEPTEDを確定し、内側の反復をBreakします。
6. NGならレビュー情報と判定材料を保存し、次の反復に進みます。
7. UNKNOWNなら再送せず、状態を保持して安全に停止します。
8. 上限までにOKにならなければREJECTEDとし、停止規則を適用します。

判定器の具体的な入出力、Combinedの集約規則、レビュー情報の詳細Schemaは後工程の設計事項です。

## Agentと判定

- AgentノードはWorkflow内に埋め込んだプロンプトを持ち、Workflow変数から値を差し込みます。
- プロンプトテンプレートの正式な構文とエスケープ規則はD2で定めます。D1はノードから型付き値を明示参照する抽象契約までを扱います。
- 判定方式はMachine、Agent、Combinedを選べます。
- 判定結果は少なくともOK、NG、UNKNOWNを区別します。
- UNKNOWNは再送許可や自動リトライを意味しません。

## 変数

- 変数値は任意の未追跡ファイルや汎用contextへ混在させず、型付きJSON値として管理します。
- 基本的な論理スコープはWorkflow、Issue、Attemptです。Workflowはrun固有、IssueはIssue反復固有、AttemptはRepeat-Until反復固有です。
- 変数ノードを読み取りの必須条件にしません。他ノードから契約に沿って変数を参照できます。
- 初期化、代入、加算、減算、配列append、部分object更新、削除を提供します。
- 変数値、スコープ、型、更新履歴はrunの回復と実行履歴に結び付けます。
- 型に合わない計算、missing値からの暗黙生成、曖昧な同名参照を認めません。配列追加は実行リプレイで重複適用されないようにします。

## 共通契約、呼び出し、履歴

- Workflowをmain/subの別種に分けません。どのWorkflowにも共通の入力JSON Schema、出力JSON Schema、変数宣言を定義できます。
- GUI、MCP、API、別Workflowなど呼び出し元が異なっても、同じrevisionのcontractに対して同じ検証・値の扱いを適用します。
- 親Workflowは子Workflowへ入力引数を明示して渡し、子の戻り値は明示したreturn/export境界を通して受け取ります。親が子のローカル変数へ暗黙に書き込む方式は採用しません。
- メインフローの既存input_dataとJSON resultの構造を維持します。
- 保存された定義revisionは不変です。contractも対象revisionと一体で固定し、実行履歴から使ったrevisionを特定できます。
- DBOSとOWSを活用し、実行・再開・状態観測を行います。Workflowのrequest id冪等性とCodex jobの不確実な送信状態を維持します。

## OWSと既存機能の互換性

- OWS v1.0.3の公式YAML Schemaを黙って拡張したり、上流仕様のように偽装したりしません。
- Workflow固有contractはCodexBridge側のversioned sidecar/envelopeとして扱う設計案です。保存済み定義revisionと一体で固定します。格納キー、DB migration番号、公開URL等はD1のOpen questionsです。
- contractを指定していない既存revisionはlegacy modeで扱い、従来検証を維持します。既存YAMLを一括して新contractの意味に読み替えません。
- 新規strict contractは明示的にopt-inします。将来のmigrationは別途設計・承認します。
- 既存YAMLのset、input、output、export、ジャンプ表現の動作と保存済みrevisionを、設計書だけの変更で変えません。

## 操作面、ノード群、段階計画

- 将来の操作面として、GUIでのWorkflow編集、実行グラフ、MCPによる管理・実行操作を扱います。
- 計画するノード群には、汎用Agent呼び出し、機械判定、Agent判定、レビュー、テスト・検証、Git連携、入力、結果集計を含めます。
- 各Phaseは[README](README.md)の対応表に従います。D3は機械判定・エージェント判定・レビュー、D4は1 Issue・1回修正・受入の縦断実証（必要なテスト・検証を含む）、D7は複数Issue For・Git連携・結果集計を担います。
- Phase一覧と状態は[README](README.md)を参照してください。PLANNEDの項目は実装済みではありません。

## 合意事項と後工程の詳細決定

### 合意済み基本方針

入口にIssue配列と各Issueに対応付く修正設計書・受入条件をそろえること、外側Forと内側Repeat-Untilを使うこと、OK/NG/UNKNOWNの扱い、反復上限時のREJECTED、構造化ループだけで新しい修正反復を表すこと、型付き変数とWorkflow/Issue/Attemptの論理scope、全Workflow共通の入出力contract、immutable revision、明示的な親子引数戻り値、DBOS/OWS活用、GUI・グラフ・MCPとノード群を段階的に扱うことは合意済みです。

### 未確定の詳細

contract sidecarの直列化形式・格納先・migration、JSON Schemaで扱うキーワードの細部、defaultの適用範囲、additionalProperties未指定時の厳密contractの規則、変数宣言の具体構文、secretの保存方式、リソース上限値、判定統合規則、反復上限と停止範囲、各APIの具体フィールドなどは、適切なphaseで決定します。未確定値を確定仕様として扱いません。
