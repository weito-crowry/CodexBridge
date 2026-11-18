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
| [D1: 共通入出力契約・変数・スコープ](phases/D1-input-output-variables.md) | DRAFT（レビュー待ち） | contract、変数スコープ、永続化、DBOS回復、検証と受入条件の詳細設計案 |

## Phase一覧

各phaseの状態は設計文書の成熟度を表します。PLANNEDは実装済みを意味しません。phase名と境界は、後続の設計レビューで更新できます。

| Phase | 概要 | 状態 |
| --- | --- | --- |
| D1 | Workflow共通入出力契約、変数宣言、Workflow・Issue・Attempt相当のスコープ | **DRAFT** |
| D2 | Agentノード、埋め込みプロンプト、変数参照と差し込み | **PLANNED** |
| D3 | Git変更、ブランチ、差分などのGit操作ノード | **PLANNED** |
| D4 | テスト実行、機械判定、レビュー証跡の収集 | **PLANNED** |
| D5 | For / While / Repeat-Until、Break / Continue、条件付き反復と停止結果 | **PLANNED** |
| D6 | 親Workflowから子Workflowを呼ぶ引数・戻り値の実行連携 | **PLANNED** |
| D7 | Issue入力、Issue単位の処理、レビュー、結果集計 | **PLANNED** |
| D8 | GUIでのWorkflow編集、実行グラフ、MCP操作の統合 | **PLANNED** |

## 用語と状態

- **合意済み**: 基本仕様 v0.2 に記された製品方針。個々の実装詳細まで確定したことを示しません。
- **Accepted**: ADRが記録する基本方針。ADR 0001は構造化ループを採る方針を示し、既存機能に新しいLoop機能が実装済みであることを意味しません。
- **DRAFT**: 詳細設計案。レビュー・変更の対象であり、最終承認前です。
- **PLANNED**: 概要のみの予定。詳細設計・実装・受入は未完了です。
- **Legacy mode**: 共通contractが結び付いていない既存Workflow定義を、保存済み内容の従来検証・実行挙動のまま扱う互換モード案です。
- **Strict contract**: revisionに明示的に結び付けた入力Schema、出力Schema、変数宣言を使うopt-in方式の案です。
- **Unknown / UNKNOWN**: 結果を確定できない状態。成功・失敗に読み替えず、再送もしません。

## 依存関係

基本仕様と現在の責務分担をそれぞれ [要件](requirements.md) と [アーキテクチャ](architecture.md) に記します。D1はこれらを前提にする詳細設計案であり、ループ方針は [ADR 0001](decisions/0001-structured-loops.md) を参照します。D2〜D8はD1のcontract・scope・return境界を利用する前提ですが、後続設計で詳細な依存関係を定めます。
