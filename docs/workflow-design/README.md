# Workflow設計書

## この文書群の位置づけ

本書群は、Workflow機能の基本仕様と段階的な詳細設計を管理する場所です。実装済み機能の説明と今後の設計案を区別します。

- **基本仕様 v0.2** はユーザー合意済みの方針です。正式な要件一覧は [requirements.md](requirements.md) に記録します。
- **D1の詳細設計** は提案段階で、ユーザーの設計レビュー待ちです。文書状態は **DRAFT** であり、承認済み設計や現行実装として扱いません。
- **D2〜D8** は後工程の計画です。後から phases/D2-agent-nodes.md などを追加できる構成にし、現時点では未着手の空文書を作りません。
- 現行実装と将来案の差分は [architecture.md](architecture.md) に記載します。

## 文書目次

| 文書 | 状態 | 内容 |
| --- | --- | --- |
| [基本仕様 v0.2](requirements.md) | 合意済み | Workflowの構造、修正反復、判定、変数、契約、実行履歴、操作面 |
| [現行実装とターゲット構成](architecture.md) | 現状記録 / 将来構成案 | OWS、DBOS、保存定義、request ledger、MCP、Consoleの現状と拡張境界 |
| [ADR 0001: 構造化ループ](decisions/0001-structured-loops.md) | Accepted（基本方針） | 新規修正ループに任意ジャンプを使わない方針と互換性 |
| [ADR 0002: contractのrevision保存と正規化](decisions/0002-contract-storage-and-normalization.md) | Proposed（レビュー待ち） | contract revision保存、hash binding、strict input normalizationの設計候補 |
| [ADR 0003: run-local変数DBとmanifest v2](decisions/0003-run-local-variable-store-and-manifest-v2.md) | Proposed（レビュー待ち） | run別変数DB、DB移行、request binding、strict v2回復・replayの設計候補 |
| [ADR 0004: SQLite DDLとschema migration](decisions/0004-sqlite-ddl-and-schema-migration.md) | Proposed（レビュー待ち） | definitions/request/variable DBのDDL案、validator、upgrade/rollback gate |
| [ADR 0005: 起動排他・resource cap・Windows ACL](decisions/0005-startup-exclusion-resource-limits-windows-acl.md) | Proposed（レビュー待ち） | controller排他、旧版upgrade gate、NTFS ACL/path保護、strict v1上限案 |
| [D1: 共通入出力契約・変数・スコープ](phases/D1-input-output-variables.md) | DRAFT（レビュー待ち） | contract、変数スコープ、永続化、DBOS回復、検証と受入条件の詳細設計案 |

## Phase一覧

各phaseの状態は設計文書の成熟度を表します。PLANNEDは実装済みを意味しません。phase名と境界は、後続の設計レビューで更新できます。

| Phase | 概要 | 状態 |
| --- | --- | --- |
| D1 | Workflow共通入出力契約、変数宣言、Workflow・Issue・Attempt相当のスコープ | **DRAFT** |
| D2 | 汎用エージェント呼び出し・埋め込みプロンプト | **PLANNED** |
| D3 | 機械判定・エージェント判定・レビュー | **PLANNED** |
| D4 | 1 Issue・1回修正・受入の縦断実証（必要なテスト・検証を含む） | **PLANNED** |
| D5 | While / Repeat-Until / Break / Continue、有界反復 | **PLANNED** |
| D6 | サブWorkflow呼び出し・親子永続化・引数戻り値 | **PLANNED** |
| D7 | 複数Issue For・Git連携・結果集計 | **PLANNED** |
| D8 | GUI編集・実行表示・MCP管理拡充 | **PLANNED** |

## 用語と状態

- **合意済み**: 基本仕様 v0.2 に記された製品方針。個々の実装詳細まで確定したことを示しません。
- **Accepted**: ADRが記録する基本方針。ADR 0001は構造化ループを採る方針を示し、既存機能に新しいLoop機能が実装済みであることを意味しません。
- **DRAFT**: 詳細設計案。レビュー・変更の対象であり、最終承認前です。
- **Proposed**: ADRに記録した設計候補。レビュー待ちであり、Acceptedではありません。
- **PLANNED**: 概要のみの予定。詳細設計・実装・受入は未完了です。
- **Legacy mode**: 共通contractが結び付いていない既存Workflow定義を、保存済み内容の従来検証・実行挙動のまま扱う互換モード案です。
- **Strict contract**: revisionに明示的に結び付けた入力Schema、出力Schema、変数宣言を使うopt-in方式の案です。
- **Unknown / UNKNOWN**: 結果を確定できない状態。成功・失敗に読み替えず、再送もしません。

## 依存関係

基本仕様と現在の責務分担をそれぞれ [要件](requirements.md) と [アーキテクチャ](architecture.md) に記します。D1はこれらを前提にする詳細設計案であり、ループ方針は [ADR 0001](decisions/0001-structured-loops.md)、contract保存・正規化は [ADR 0002](decisions/0002-contract-storage-and-normalization.md)、run-local変数DBとmanifest v2は [ADR 0003](decisions/0003-run-local-variable-store-and-manifest-v2.md)、SQLite DDLとmigrationは [ADR 0004](decisions/0004-sqlite-ddl-and-schema-migration.md)、起動排他・ACL・strict上限は [ADR 0005](decisions/0005-startup-exclusion-resource-limits-windows-acl.md) を参照します。D2〜D8はD1のcontract・scope・return境界を利用する前提ですが、後続設計で詳細な依存関係を定めます。
