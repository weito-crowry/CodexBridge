# D1-I0 Baseline / 受入証跡

- **文書状態:** REVIEW_PENDING（広範回帰未完了）
- **目的:** 現行legacy動作を観測可能なテストとgolden allowlistに固定する。strict contract、変数runtime、schema migrationなどの新機能を実装した証跡ではありません。
- **作業branch:** `codex/workflow-d1-i0-baseline`
- **起点branch / base SHA:** `docs/workflow-design-d1` / `3a883334daf6480cb3ae2f7fdb6e8269928504dc`
- **I0 commit SHA:** 証跡同期commitで記載予定
- **実行環境:** Windows native、Python 3.14 `.venv`、`uv`。

## 検証結果

| 検証 | 結果 | 証拠 |
|---|---|---|
| `uv run pytest -q tests/test_workflow_d1_i0_baseline.py` | PASS | 8 passed。HTTP形状、ETag/履歴、saved run、graph、legacy call拒否、MCP permission catalog。 |
| `uv run pytest -q tests/test_workflow_saved_execution.py tests/test_workflow_graph.py` | PASS | 29 passed。保存実行とgraphの既存回帰。 |
| 指定8ファイルpytest suite（本ファイル、definitions、definition API/MCP、saved execution/API/MCP、graph） | NOT_COMPLETE | 起動して実行を開始したものの、部分的なprogress dotの後40秒以上集計・終了が返らず、今回起動したpytestプロセスだけを停止しました。終了コード・最終pass/fail/skip数は取得できていません。PASS扱いしません。再検証条件は[実装計画I0](D1-implementation-plan.md)のレビューで確認します。 |
| `uv run ruff check tests/test_workflow_d1_i0_baseline.py` | PASS | All checks passed。 |
| `uv run ruff format --check tests/test_workflow_d1_i0_baseline.py` | PASS | 1 file already formatted。 |
| `uv run mypy src/codex_bridge` | PASS | Success: no issues found in 132 source files。 |
| `uv run python -m compileall -q src/codex_bridge tests/test_workflow_d1_i0_baseline.py` | PASS | exit code 0。 |
| `uv run pytest -q` 全体 | NOT_RUN | 広範suiteの未完了を受け、全体suiteは開始していません。 |
| Codex実ジョブ、外部POST、production DB migration、MCP dogfood | NOT_RUN | docs/test baseline taskの境界外。 |

## Golden正規化と追加テスト

静的goldenは[workflow_d1_i0_legacy_golden.json](../../../tests/fixtures/workflow_d1_i0_legacy_golden.json)のfield-name allowlistです。UUID、時刻、workspaceや一時path、canonical digestの実値はgoldenに固定しません。ETagはstrong v1の構文を検査し、header/body一致を確認します。Create/validate/list/detail/revisionのkey集合を固定し、legacy responseに`contract`、`contract_sha256`、`binding_digest`、`run_format_version`、`reason_code`が現れないことを確認します。list summaryにsource YAML/promptが出ず、detail/revisionではsource YAMLを返す既存動作を区別します。

新規テスト関数:

- `test_legacy_http_definition_shapes_etag_and_detail_projection`
- `test_legacy_http_etag_preconditions_update_replay_and_revision_history`
- `test_legacy_saved_run_manifest_binding_replay_and_restart`
- `test_legacy_graph_schema_count_and_json_pointer_node_ids_are_stable`
- `test_legacy_mode_official_ows_allows_but_interpreter_rejects_variable_call`
- `test_legacy_mcp_permission_catalog_keeps_read_write_execute_separate`（read/write/executeの3ケース）

## D1受入IDの対応

以下はI0で実測した既存互換の一部のみです。I0の8新規テストでD1全156件を実装・検証したという意味ではありません。

| D1 ID | 対応テスト | I0で観測した範囲 |
|---|---|---|
| D1-09 | `test_legacy_http_etag_preconditions_update_replay_and_revision_history` | 旧revisionを取得でき、後続update後もrevision 1のsourceが維持される。 |
| D1-10 | `test_legacy_saved_run_manifest_binding_replay_and_restart` | legacy requestの同一payload replay、source mapping、restart後の二重startなし。 |
| D1-27 | `test_legacy_saved_run_manifest_binding_replay_and_restart`（部分） | legacy manifest/request側のみ。OWS set/export/jump全体はこの証跡では未検証。 |
| D1-33 | `test_legacy_saved_run_manifest_binding_replay_and_restart`（legacy側のみ） | manifestに`run_format_version`がなく、legacy request digest/source bindingを保持。strict v2 pathは未実装。 |
| D1-34 | `test_legacy_http_etag_preconditions_update_replay_and_revision_history`（legacy側のみ） | legacy v1 ETagの強形式とheader/body一致。strict v2は未実装。 |
| D1-60 | `test_legacy_saved_run_manifest_binding_replay_and_restart`（部分） | legacy manifest/requestの再照合と再起動後replayのみ。全reader監査は未実施。 |
| D1-122 | `test_legacy_http_definition_shapes_etag_and_detail_projection`、`test_legacy_http_etag_preconditions_update_replay_and_revision_history` | contract未指定HTTP create/validate/updateのlegacy key形状とETag/response互換。MCP mutation shape全体は未検証。 |
| D1-128 | `test_legacy_http_etag_preconditions_update_replay_and_revision_history`（legacy CAS側） | missing If-Match=428、stale=412、update/replayとimmutable history。MCP strict CASは未実装。 |
| D1-129 | `test_legacy_http_definition_shapes_etag_and_detail_projection`（GET/list/revision側） | legacy summary/detail/revisionのfield allowlist、summaryでsource YAMLを非表示。HTTP graph metadata全体は未検証。 |
| D1-131 | `test_legacy_http_definition_shapes_etag_and_detail_projection`、`test_legacy_http_etag_preconditions_update_replay_and_revision_history` | legacy ETag body/header一致。strict v2は未実装。 |
| D1-143 | `test_legacy_mcp_permission_catalog_keeps_read_write_execute_separate` | MCP read/write/execute tool discoveryの権限分離。実handler authorizationやstrict DTOは別途未検証。 |
| D1-147 | `test_legacy_mode_official_ows_allows_but_interpreter_rejects_variable_call` | 公式OWS v1.0.3 schemaはPASSし、現行legacy interpreter subsetはunsupported call targetとして拒否。 |
| D1-148 | `test_legacy_mode_official_ows_allows_but_interpreter_rejects_variable_call`（legacy拒否側のみ） | 既存legacy modeでの拒否のみ。strict allowlistと既存job target併存は未実装・未検証。 |

`D1-01..156`の残りのstrict schema、storage、security、resource quota、v2 API、replay/recovery、output contract、Windows運用gate等は、実装slice計画に従いI1..I8へDEFERRED / NOT_IMPLEMENTEDです。[D1詳細設計](D1-input-output-variables.md)の受入番号は再採番していません。

## 現行動作と制限

- strict contractや型付き永続変数storeは追加していません。`codexbridge-variable`は公式schema上の有効なCallFunction targetですが、現行legacy interpreterではunsupportedです。将来I7のstrict-only adapterを誤ってlegacy期待へ混ぜないよう、テスト名にlegacy modeを明記しました。
- Graphは既存projectorの`schema_version`, static task count, JSON Pointer escapeだけを固定しています。時刻、PID、runtime-dependent status/result/event値はgoldenに含めません。
- MCP permission testはtool catalog分離のみを固定します。実行や外部副作用は行いません。
- この作業でSQLite、共有state root、定義DB、requests DB、CodexBridge本体は変更していません。pytestが使うstateは各`tmp_path`内です。
- ユーザー所有の未追跡証跡5パスは名前以外を参照せず、変更・stage・削除していません。

## 残る確認と次のgate

1. 広範な指定8ファイルsuiteはNOT_COMPLETEです。I0をPASSにする前に、timeout付きで対象を小分けに再実行し、完了したexit codeとpass/fail/skip件数を記録してください。今回の部分dotは結果数に換算しません。
2. I0 traceabilityはlegacy互換の一部をテスト関数へ結び付けた段階です。残りのD1 IDは将来slice用に保持し、今回の8件を全件PASSとみなしません。
3. I1〜I8はNOT_STARTEDです。strict v1、schema migration、Global mutex/Windows ACL、strict request binding、variables.sqlite、API v2、adapter runtime、Windows isolated acceptanceは未実装です。
4. ChatGPT GitHub review待ちです。I0はREVIEW_PENDINGであり、D1実装開始・D1承認・本番strict enableを意味しません。
