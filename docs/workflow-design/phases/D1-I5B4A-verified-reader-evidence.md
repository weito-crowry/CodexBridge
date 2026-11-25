# D1-I5-B4A verified reader evidence

- **実装状態:** IMPLEMENTED / PENDING_CHATGPT_REVIEW。
- **対象branch:** `codex/workflow-d1-i5-b4-verified-readers`。
- **base:** `origin/dev` at `483c341af5b2411ea17710d704d5eb2b9f908d32`。
- **I5-B4A:** 共通strict v2予約済みsnapshot reader検証とservice workspace reader接続。
- **I5-B4全体 / I5全体:** IN_PROGRESS。I5-B4のworker reservation orderingとChatGPT reviewは未完了。
- **strict run admission:** OFF。**I2:** DEFERRED / NON-BLOCKING。**D1全体:** DRAFT。

## 実装

`strict_reader_gate.verify_strict_reserved_readonly(...)` は、state root、run ID、callerが読んだmanifest bytes、期待するworkflow/request identity、allowed rootsを受け取ります。既存 `classify_readonly` が有効なlegacy v0/v1と判定した場合は `None` を返し、strict v2の場合は `StrictWorkflowSnapshotVerifier.verify_reserved(run_id)` を呼びます。caller原本を `parse_strict_manifest_v2` で解析し、verifierが再読込したmanifestとdataclass equalityで照合します。さらに予約確認が `True` で、run/workflow/request identityが期待値と一致する場合だけ既存の `StrictWorkflowSnapshotVerification` を返します。unknown version、未予約、manifest差し替え、snapshot/input/variables store不整合、allowed root違反などは値を含まない `StrictReaderGateError` へ統一し、例外連鎖から生値を出しません。

`service._verified_active_workspace` はこのhelperを一度呼びます。strict v2では検証済みmanifestのcanonical `workspace_root` を返します。allowed rootとstate/workspace overlapは既存reserved verifierの `AllowedPathPolicy` 検証を使います。strict snapshotにlegacy digestを再計算しません。helperがlegacyを返した場合は、従来のlegacy digest/control/workspace検証へ進みます。不整合strict runは既存のworkspace conflictへ収束します。

`_inspect_run`、status/result、manager start/recover、task/event projection、worker/worker_entryのstrict拒否は変更していません。valid snapshotの検証はruntime実行許可ではなく、B4Aでworker ordering、strict run admission、feature flag、API/MCP契約、DBOS/job接続、output成功判定は追加していません。

## 変更path

- `src/codex_bridge/workflows/strict_reader_gate.py`
- `src/codex_bridge/workflows/service.py`
- `tests/test_workflow_d1_i5b4a_verified_readers.py`
- `tests/test_workflow_d1_i5b1_service_gate.py`
- `docs/workflow-design/phases/D1-I5B4A-verified-reader-evidence.md`
- `docs/workflow-design/phases/D1-implementation-plan.md`

## 検証

テストDBとrun snapshotはpytest `tmp_path`内だけを使いました。要求されたfocused pytestは合計 **364 passed / 0 failed / 4 skipped** です。

| 対象 | 結果 |
| --- | --- |
| B1 service gate + 新B4A verified reader tests | 31 passed、0 failed |
| I5-A manifest + I5-B2 manager/projection + I5-B3 worker reader suites | 144 passed、0 failed、3 skipped |
| workflow service/manager/task projection + CPP MCP/account auth boundary suites | 189 passed、0 failed、1 skipped |
| Ruff check（`src/codex_bridge`と変更した2 test files） | PASS |
| Ruff format check（`src/codex_bridge`と変更した2 test files） | PASS — 153 files already formatted |
| mypy `src/codex_bridge` | PASS — 151 source files |
| `python -m compileall -q src/codex_bridge` | PASS |
| `git diff --check` | PASS — staged diff、6 paths |
| full pytest | NOT_RUN — 今回の指定範囲外 |

4件のskipはWindows symlink作成権限によるものです。最初のB1/B4A再実行では、追加拒否テストのSQLite更新がcommitされず1件FAILしました。テストfixtureをcommitするconnection contextに修正し、最終suiteでは31件すべてPASSしました。製品コードの失敗ではありません。

検証には実Bridge/Worker起動、実Codex job、Workflow再POST、共有/production DB、DBOS checkpointを使用していません。read-only assertionはsnapshotとSQLite main fileのbytes比較です。WAL metadataの不変性は主張しません。

## レビュー・残件

- ChatGPT GitHub review待ち。Codex作業終了時点でI5-B4A受入済みとは扱いません。
- I5-B4の残りworker reservation ordering、I5全体の受入、I6〜I8は未実施です。
- strict run admissionはOFFのままです。status/resultおよびworkerがstrict v2を成功・実行扱いしないことを維持します。
- 旧I5-B3受入commitから `origin/dev` までの後続22 commitsを確認し、対象workflow reader source/test pathsに変更がないことを確認しました。
