# ADR 0002: Workflow contractのrevision保存と入力正規化

- **Status:** Proposed（レビュー待ち）
- **範囲:** D1のcontract保存、revision identity、strict input normalization
- **実装状態:** 未実装。ProposedはAcceptedを意味しません。

関連文書: [基本仕様 v0.2](../requirements.md)、[アーキテクチャ](../architecture.md)、[D1詳細設計案](../phases/D1-input-output-variables.md)。

## Context

現行OWS v1.0.3 YAMLと配布SchemaはWorkflow定義を表し、definition repositoryはYAML revisionとsource/canonical hashを保存します。OWS YAMLへCodexBridge固有contractを加えると、公式Schemaとの境界を変え、既存revisionを再解釈する危険があります。

実行serviceは現在のrequest digestとmanifestを使ってrun内容を検証します。既存のrevision、request id同一性、legacy manifest検証はそのまま保つ必要があります。strict contractでは入力defaultを一度だけ正規化してから実行しなければ、同じrequest idの意味的同一性が安定しません。

## Decision

### Contractの保存とrevision binding

以下をD1の設計候補とします。

- OWS v1.0.3 YAMLのtop-level keyと公式配布Schemaを変更しません。別ファイルsidecarも使いません。
- CodexBridge拡張は contract_version: 1、input_schema、output_schema、variables を持つ論理contract JSONとします。
- contractは既存WorkflowDefinitionRepositoryと同じSQLite DB内の新規revision従属テーブル workflow_definition_contracts に保存します。
- テーブルの複合primary keyは (definition_id, revision) とし、既存workflow_definition_revisionsの親revisionを参照します。保存値は contract_json_canonical、contract_sha256、binding_digest、created_at とします。
- 親revisionの作成/更新、contract rowの追加、mutation request ledgerの記録は同一SQLite write transactionでcommitします。
- YAML source/canonical hashとcontract hashを両方保持します。contract_sha256は現行 _canonical_json と同じsort_keys、UTF-8、NaN拒否、compact separatorのcanonical JSONから計算します。
- binding_digestは同じcanonical JSON方式によるSHA-256とし、definition_id、revision、YAML source SHA、YAML canonical SHA、max_live_codex_jobs、contract_version、contract_sha256 を結びます。
- contract変更は新revisionとし、strict ETagはcontract identityを含むversioned identityに拡張します。strict ETag wire表現と既存clientへの移行影響はOpen questionsです。

### Legacyとstrict mutation

- legacy create/updateでcontractを省略した場合は既存legacy挙動を維持します。legacy request digestとrequest id再送規則は変えません。
- legacy revisionをstrictへ移すにはcontractを明示して新revisionを作ります。
- strict revisionのupdateではcontractを明示します。省略やnullをcontract解除として扱わず、strictからlegacyへの暗黙downgradeを許しません。
- contract-aware mutation digestにはcontractの有無、canonicalな値、対象revisionを含めます。
- APIを別routeにするか同一endpointのoptional fieldにするかは後続設計に残します。

### Schemaとnormalized input

- strict v1のinput_schema/output_schema rootはtype: objectとし、legacy root scalar outputは従来のまま維持します。
- strict SchemaはDraft 2020-12を明示し、候補allowlistとlocal non-recursive $refだけを受け付けます。未知のvalidation keywordを黙って無視せず拒否します。
- object SchemaにはadditionalPropertiesを明記します。明示されたtrueをfalseへ書き換えません。
- defaultはinput propertiesのmissing値だけに一度適用し、deep copyのnested objectと既存array内objectを正規化します。root defaultとoutput defaultは補完しません。
- 正規化後inputでvalidation・実行・strict idempotency digestを行い、binding digestとnormalized input hashをstrict v2 manifest/request-digest経路に加えます。
- legacy manifest v1と従来request digestは別経路で維持し、過去runを再計算・再解釈しません。未知versionやhash/revision不一致はfail closedです。

## Consequences

- OWS YAMLと公式Schemaは変更されず、strict contractの保存と検証はCodexBridgeのrevisionに閉じます。
- YAML、contract、mutation ledgerの部分保存を防ぎ、contract変更は追跡可能な新revisionになります。
- legacy request identityとmanifest検証を維持しながらstrict requestの正規化後同一性を定義できます。
- migration番号、manifestの具体的version discriminator、API wire field、ETag形式、Schema/response上限はこのADRでは確定しません。
- D1文書の状態はDRAFT / Proposedのままとし、ユーザーの設計レビューまで承認済み扱いにしません。

## Alternatives

- **OWS YAML top-levelへcontract keyを追加する:** 上流OWS SchemaとCodexBridge拡張の境界を変えるため採りません。
- **YAMLと別ファイルsidecarを置く:** 保存・backup・revisionの原子性とbindingを別管理にするため採りません。
- **SQLite DB内のrevision従属rowを使う:** このProposedの候補です。SQLite migrationの実装番号とAPI公開形は後続設計です。
- **既存manifest/request digestをstrictの意味へ読み替える:** 過去runの再解釈になるため採りません。strict v2を分け、legacy v1を維持します。
