# Phase B5-R3 統合受入結果

## 判定

**BLOCKED**。module形式で起動したrunnerはGate 0の回帰・静的検査を通過しましたが、runtime preflightのHEAD guardで停止しました。Gate 1へ到達せず、Workflow/Codex実行はありません。runner起動はこの1回で終了し、再試行していません。

- Issue: #145
- branch: codex/workflow-phase-b5-r3-acceptance
- 開始時HEAD / 基準parent: 473803df48ce2c0849e253c0357a7b9ea5684641
- runnerのPARENT_HEAD: ce05ba54d9d47f9160a9b1a3f571f0239cad4983
- R3 attempt: 6f823afa781b40ef8641bc37d04f0311
- evidence: [phase_b5_workflow_acceptance-6f823afa781b40ef8641bc37d04f0311.json](phase_b5_workflow_acceptance-6f823afa781b40ef8641bc37d04f0311.json)
- runner起動前のSHA-256: 602d8830368493fe54c5f57033c9d69b8b959af23a0fcd0631b3a86f45c0e82b

## Gate判定

| Gate | 判定 | 結果 |
|---|---|---|
| Gate P: module import | **PASS** | runner moduleをimportし、_mainがcallableであることを確認。phase5e2c2_isolated_smokeとphase5e_smoke_supportもimport成功。_mainは呼び出していない。 |
| Gate P: offline検査 | **PASS** | B5 unit、対象回帰、静的検査を下記のとおり実行。 |
| Gate 0: runner内offline回帰・静的検査 | **PASS** | runner evidenceで4群961 passed / 8 skipped / 0 failed、静的検査は全てPASS。 |
| Gate 0: runtime preflight | **BLOCKED** | runnerのsource HEAD guardが指定branchのHEADを受け入れず停止。 |
| Gate 1: 隔離MCP・Control | **NOT_RUN** | runtime preflightで停止。Bridge、port、token、HTTP/MCP接続なし。 |
| Gate 2: saved execution | **NOT_RUN** | 初回POST 0、実Codex job 0、MCP replay 0。 |
| Gate 3: read-only restart | **NOT_RUN** | Bridge起動・再起動なし。 |

Gate P module import command:

uv run --no-sync --extra dev --extra workflows --extra console python -c "import importlib; m=importlib.import_module('scripts.phase_b5_workflow_isolated_smoke'); assert callable(m._main)"

追加でscripts.phase5e2c2_isolated_smokeとscripts.phase5e_smoke_supportをimportlib経由でimportした。失敗したR2のscript-path起動は再実行していない。

## 確認された停止原因

実機runnerの唯一の起動コマンド:

uv run --no-sync --extra dev --extra workflows --extra console python -m scripts.phase_b5_workflow_isolated_smoke

runner evidenceのreasonはruntime_preflight_unavailable、preflight errorはrunner_source_head_is_not_expected_parent。R3 branchのHEADは指定どおり473803df48ce2c0849e253c0357a7b9ea5684641で、PARENT_HEADのce05ba54d9d47f9160a9b1a3f571f0239cad4983はそのancestorである。

scripts/phase_b5_workflow_isolated_smoke.pyの_git_head()はmerge-base --is-ancestorでancestor関係を確認した後、さらに現在HEADとPARENT_HEADの完全一致を要求する（該当箇所は同ファイルの_git_head関数）。そのため、指定されたR3 branchのHEADを拒否した。runnerはsource_head記録前に止まった。これは受入runnerのguard条件の不一致であり、B1〜B4製品ソースの不具合を示すものではない。

提案: 承認済み手順がancestorを許容するなら、guardをその契約に沿わせ、固定ancestorの確認後にHEAD完全一致を必須としない修正をChatGPTレビューに提示する。今回は実行上限に達したためrunner/テストを変更せず、再起動もしない。修正と次の受入実行にはレビュー後の別途指示を要する。

## Offline検査結果

B5専用unit testは29 passed / 0 skipped / 0 failed。TEST_GROUPSに対応する4群は次のとおり。B5の29件はConsole/smoke群に含まれているため、合計に重ねて加算していない。

| 対象群 | Passed | Skipped | Failed / errors |
|---|---:|---:|---:|
| Workflow API/MCP/saved definitions | 107 | 2 | 0 |
| service/API/multijob/manager | 439 | 5 | 0 |
| Console/smoke/B5 runner | 260 | 1 | 0 |
| OWS regression | 155 | 0 | 0 |
| **合計** | **961** | **8** | **0** |

skip 8件はWindows symlink privilege条件。全pytestは実行していない。runner内Gate 0でも上記4群と静的検査が記録され、全テスト群はPASSだった。

次の静的検査はいずれも独立実行およびrunner evidenceでPASS:

- uv run --no-sync --extra dev ruff check src scripts tests
- uv run --no-sync --extra dev ruff format --check src scripts tests（296 files）
- uv run --no-sync --extra dev mypy src（128 source files）
- uv run --no-sync --extra dev python -m compileall -q src/codex_bridge scripts
- uv lock --check
- git diff --check

## 実行件数・隔離境界

- R3 module runner invocation: 1回（終了コード1、BLOCKED）
- 初回HTTP Workflow POST: 0
- unique initial client_request_id: 0
- 実Codex job: 0
- same-ID MCP replay: 0
- conflicting POST: 0
- MCP/Control API呼出し、DBOS run、SQLite run照合: 0
- Bridge start/restart、Worker/app-server start: 0
- status observation/result gate: NOT_OBSERVED
- ownership/orphan cleanup: NOT_RUN / NOT_OBSERVED

旧B5 attemptのremote outcomeは引き続きUNKNOWN。旧B5/R2のevidence、request ID、run、session、Codex jobにはアクセスせず、再利用・再送・再開・照会・復旧していない。本番Bridge、18000/18001、production state、認証原本にも接続・変更していない。

B5-R3は受入PASSではない。Gate 1〜3と実機saved executionは未検証。#141のBLOCKED判定と旧attemptのUNKNOWNは維持する。
