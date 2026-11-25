# D1-I5-B4B verified service and projection evidence

- **実装状態:** IMPLEMENTED / PENDING_CHATGPT_REVIEW。
- **対象branch:** `codex/workflow-d1-i5-b4-verified-readers`。
- **base:** `origin/dev` at `483c341af5b2411ea17710d704d5eb2b9f908d32`。
- **I5-B4A review:** ChatGPT GitHub review of `27a2fbfc2700fb5aa9875eb2f57f2b7f301fe41a` はACCEPTED (with documented caveats)。full pytest、DBOS、実Worker/job、WAL metadataはそのreviewの留保として記録済み。
- **今回の範囲:** service status/resultとtask/event projectionをB4A共通verified readerへ接続。
- **I5-B4全体 / I5全体:** IN_PROGRESS。**strict run admission:** OFF。**I2:** DEFERRED / NON-BLOCKING。**D1全体:** DRAFT。

## 実装

`service._inspect_run` はstrict v2をB4A `verify_strict_reserved_readonly` で検証します。通常service callerは `worker_manager.allowed_roots` を `_snapshot`、result、task/event、worker readiness、row snapshot、request reservation readerへ明示伝播します。strictでrootsが空ならreservationの安全なmetadataを採用しません。有効strict runのstatus/resultは `needs_reconciliation` / `unknown` / non-terminal / `result=None` を維持し、検証済みmanifestの `max_live_codex_jobs` だけを既存snapshotへ投影します。worker status、job ledger、DBOS status、result JSONは読みません。

`task_projection._verified_run_source` も同helperを使います。strict runでは予約検証時に照合済みのpinned definitionをverification resultの非公開 `repr=False` fieldから受け取り、検証後にsnapshot `workflow.yaml` を再読込しません。既存graph validatorが対応する定義だけ、allowlistしたnode identity/operation/scope/orderとedge source/target/kindを返します。strict task stateはunknown、execution/completionは空/false、coverageはpartial / `workflow_not_confirmed`、latest event IDは0です。event projectionは空のままcursorを保持します。journal、jobs、DBOS、resultを参照しません。validatorで表現できない定義は `graph_invalid` / partialになります。

legacy v0/v1 status、digest、workspace、graph、event動作は既存経路のままです。公開DTO/schema version、manager/worker/worker_entry、strict admission/runtime、実行開始、DBOS/job接続、output成功判定は変更していません。

## 変更path

- `src/codex_bridge/workflows/service.py`
- `src/codex_bridge/workflows/strict_manifest.py`
- `src/codex_bridge/workflows/task_projection.py`
- `tests/test_workflow_d1_i5b2_reader_gate.py`
- `tests/test_workflow_d1_i5b4b_verified_projection.py`
- `docs/workflow-design/phases/D1-I5B4A-verified-reader-evidence.md`
- `docs/workflow-design/phases/D1-I5B4B-verified-projection-evidence.md`
- `docs/workflow-design/phases/D1-implementation-plan.md`

## 検証

pytestはWindows native環境の隔離 `tmp_path` DB/snapshotのみを使いました。focused regressionは **283 passed / 0 failed / 4 skipped** です。skipはI5-Aのsymlink 2件、I5-B3のsymlink 1件、legacy task projectionのsymlink 1件で、いずれもWindowsのsymlink作成権限が利用できないためです。B4B新規テストとB2 valid-strict projection更新の最終focused rerunは **9 passed / 0 failed** です。

| 検査 | 結果 |
| --- | --- |
| I5-A / B1 / B2 / B3 / B4A / B4B、service、task projection focused pytest | 283 passed、0 failed、4 skipped |
| B4B tests + 更新したB2 valid-strict projection test | 9 passed、0 failed |
| Ruff check（`src/codex_bridge`と変更したtest files） | PASS |
| Ruff format check（同上） | PASS — 153 source files already formatted |
| mypy `src/codex_bridge` | PASS — 151 source files |
| `python -m compileall -q src/codex_bridge` | PASS |
| `git diff --check` | PASS。文書のCRLF変換に関するGit warningのみ |
| full pytest | NOT_RUN — 今回の指定範囲外 |
| CPP MCP/account-auth境界回帰 | NOT_RUN — 今回のfocused対象外 |

runtime-artifact拒否を調べるためのfake worker/job/journal/DBOS/result filesは隔離pytest `tmp_path`内だけに作成し、strict readersがそれらを開かないことをassertしました。実Bridge/worker、実Codex job、DBOS実行、共有/production DB、Workflow再POSTは使用していません。WAL metadata不変性は主張しません。

## 残件

- ChatGPT GitHub review待ち。Codex作業終了時点でB4B受入済みとは扱いません。
- I5-B4のworker reservation ordering、manager/worker/worker_entryへのreserved verification接続は未実施です。
- strict runtimeおよび実job/output成功判定はI7以降の対象です。I5-B4Bは実行許可を追加しません。
- strict run admissionはOFF、I5-B4全体とI5全体はIN_PROGRESSです。D1全体はDRAFTです。
