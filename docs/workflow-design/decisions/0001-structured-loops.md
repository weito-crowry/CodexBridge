# ADR 0001: 修正反復に構造化ループを使う

- **状態:** Accepted（基本仕様 v0.2におけるユーザー合意済み方針）
- **範囲:** 新規Workflow仕様の制御フロー
- **実装状態:** 未実装。Acceptedは現在使える機能を意味しない。

関連文書: [基本仕様 v0.2](../requirements.md)、[アーキテクチャ](../architecture.md)、[D1詳細設計DRAFT](../phases/D1-input-output-variables.md)。

## Context

Issue修正には、修正依頼、テスト、受入判定、NG時のレビュー保存、再試行がある。新しい修正フローを任意ジャンプや後方ラベル移動で表すと、何回目の試行か、どのIssueが終わったか、回復時にどの反復だったかを明示しづらい。

現在のinterpreterには同じdo scope内の名前付きジャンプがあり、循環ジャンプを拒否します。これらの既存表現と保存済みWorkflowは互換性のため維持します。

## Decision

新しいIssue修正ループは次の構造にします。

- 外側をIssue配列に対するForとします。
- 内側をRepeat-Untilとし、修正attemptの本体を最低1回実行してから終了条件を評価します。
- テスト・受入判定がOKならACCEPTEDを記録し、Breakで内側のRepeat-Untilを終了します。
- NGならレビューを保存し、Repeat-Untilの次のiterationへ進みます。
- UNKNOWNなら結果を確定せず、再送せずに停止します。
- 上限までにOKにならなければREJECTEDを記録し、指定された停止規則を適用します。上限値と停止範囲は後工程で確定します。
- BreakとContinueは字句的に最も内側のloop frameだけを対象にします。
- 新規仕様にGoTo式任意ジャンプや後方ジャンプを加えません。既存ジャンプ表現はlegacy互換のため残します。

Loop本体は変数初期化、Codex修正、テスト、Machine/Agent/Combined判定、NGレビュー保存などを含められる設計を目指します。詳細なtask構文と終了条件はD5以降で定義します。

## Consequences

- 制御フローはIssueとAttemptの入れ子scopeとして表せます。Workflow、Issue、Attemptの論理scopeを、汎用runtimeのloop iterationやnested block frameに対応付けられます。
- DBOS replay識別に、stable node pathと各loopの全iteration vectorを含められます。
- OKのBreak、NGの次iteration、UNKNOWNの停止、上限時のREJECTEDを異なる遷移として記録できます。
- D1の変数・contract設計がこの制御構造のscopeと反復IDを受け入れる必要があります。
- このADRは既存interpreterにRepeat-UntilやBreak/Continueを追加したものではなく、既存YAMLやrun結果の読み替えも行いません。

## Alternatives

- **任意ジャンプで後方移動する:** 新規のIssue修正フローでは採用しません。反復をloop frameとして識別する基本方針に合いません。既存の名前付きジャンプ表現は互換性のため存置します。
- **Forだけを使い、試行数を前もって列挙する:** 失敗に応じてNGレビューを次のattemptへ渡すループを明確に表しにくいため採用しません。
- **汎用Whileだけにする:** 合意済み仕様がRepeat-Untilの最低1回実行と最後の条件評価を要求するため、このIssue修正の基本形にはしません。
