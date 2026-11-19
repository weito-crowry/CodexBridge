# D1-I5-A strict manifest v2 codec / snapshot evidence

- **実装状態:** ACCEPTED (with documented caveats) — ChatGPT GitHub review 2026-11-19
- **I5全体:** IN_PROGRESS。I5-Aは受入済み、I5-Bは未着手で次工程はI5-B1です。
- **対象branch:** `codex/workflow-d1-i5-manifest-readers`
- **実装commit:** `982b461ba47bdfa0a62bf6da4d648c47a6545e9c`
- **レビュー修正commit:** `2fda398abbce7299c5f1e3ae0a565aebd04ab6e9`
- **D1境界:** I4全体はACCEPTED (with documented caveats)。I5-Aはmanifest codecとisolated snapshot準備・検証に限定します。D1全体はDRAFT、strict run admissionはOFFです。

## 実装内容

新規 `src/codex_bridge/workflows/strict_manifest.py` に、既存legacy経路から独立したopt-in APIを追加しました。

- `strict_request_digest_v2(...)` は `codexbridge-workflow-request-v2` domainを使い、canonical JSONで次の明示fieldだけをhashします: parsed workflow object、canonical workspace root、常に指定される整数 `max_live_codex_jobs`、pinned definition ID/revision、source SHA、contract version/SHA、binding digest、normalized input SHA。raw input本文はdigestへ入れません。
- `StrictWorkflowManifestV2` と `serialize_strict_manifest_v2` / `parse_strict_manifest_v2` は全21 fieldのallowlist、strict type、lowercase SHA-256、canonical timestamp/JSON、run_format_version=2を検証します。unknown/missing/legacy versionやextra keyは拒否します。`control`、`codex_bridge_port`、prompt、input実値、variable実値はmanifestに含めません。
- `StrictWorkflowSnapshotWriter.prepare_snapshot(...)` はI3 `StrictWorkflowDefinitionStore.get` と `StrictWorkflowBindingVerifier.normalize_for_definition` の結果を使い、pinned YAML bytesを確認します。run-local `variables.sqlite` の初期schema/RunBindingをI4-A read APIで確認後、workflow.yaml / input.json / manifest.jsonを排他的に作成し、各fileをflush/fsyncします。DB reservation、run status更新、worker起動はしません。
- `StrictWorkflowSnapshotVerifier.verify_pre_reservation(...)` はsnapshot・pinned revision・正規化済みinput・初期variables.sqliteだけを検証します。request reservationがあるかは判定しません。
- `verify_reserved(...)` はI3 request bindingとpinned definitionを再照合し、manifest v2 digestを再計算します。variables.sqliteはI4-C `inspect_ledger()`で全ledgerを検証し、空の初期状態の場合はI4-A read APIでも確認します。SQLiteへ書込みません。
- workflow YAMLは重複key、非JSON値、NaN/Infを拒否してcanonical JSON値へ変換します。snapshot上限はworkflow 64 KiB、normalized input 96 KiB、manifest 32 KiBです。pathはrun-<32 lowercase hex>、state root直下、通常file、workspaceの既存`AllowedPathPolicy`境界を検査します。

作成途中の例外で作成済みfileを削除しません。再試行は既存fileとのconflictで停止します。filesystemの複数file作成とvariables.sqliteは単一atomic transactionではありません。

## 変更面と後続責務

変更したのは次の4 pathだけです。

- `src/codex_bridge/workflows/strict_manifest.py`
- `tests/test_workflow_d1_i5a_manifest.py`
- `docs/workflow-design/phases/D1-I5A-manifest-evidence.md`
- `docs/workflow-design/phases/D1-implementation-plan.md`（I5-A進捗・現行状態のみ）

service、manager、worker、task projection、API、MCP、既存legacy manifest/digest、DB schema、I3/I4 helperは変更していません。I5-Bが担当する全readerのversion-aware導入と適切なreservation/worker順序は未実装です。strict run admissionはOFFのままです。

## 検証結果

すべての新規SQLite fixtureはpytest `tmp_path`に作成しました。production/shared DB、実Workflow/Codex job、DBOS runtime/checkpointには接続していません。

| 検証 | 結果 |
| --- | --- |
| 新I5-A専用suite `tests/test_workflow_d1_i5a_manifest.py` | PASS — 33 passed、FAIL 0。SKIP — 2（Windows symlink権限）。95.34秒 |
| I0/I1/I3-A/B/C、I4-A/B/C、legacy service/manager/task projection focused regression（12 files） | PASS — 372 passed、FAIL 0。SKIP — 3（Windows symlink権限）。368.12秒 |
| `uv run --no-sync ruff check src/codex_bridge tests/test_workflow_d1_i5a_manifest.py` | PASS |
| `uv run --no-sync ruff format --check src/codex_bridge/workflows/strict_manifest.py tests/test_workflow_d1_i5a_manifest.py` | PASS |
| `uv run --no-sync mypy src/codex_bridge` | PASS — 142 source files |
| `uv run --no-sync python -m compileall -q src/codex_bridge` | PASS |
| `git diff --check` | PASS |
| Full pytest suite | NOT_RUN |
| I5-B全reader audit、strict admission、DBOS実checkpoint、実機run、共有DB migration、外部Codex job | NOT_RUN — 後続sliceまたは別境界 |

## Caveats

- snapshot filesとvariables.sqliteの準備・検証は同一atomic transactionではありません。途中失敗ではpartial/orphan fileが残り、自動cleanupや再作成はしません。
- pre-reservation検証はrequest DBを見ず、snapshotと初期RunBindingの整合性だけを確認します。request予約済みrunの完全性は主張しません。
- reserved検証もDBOS checkpointを読みません。DBOS実checkpointとの対応、複数DB間atomicity、実runtime replayはI7以降です。
- I5-Bのservice/manager/worker/task projection等のreader接続と全reader auditが終わるまで、manifest v2を既存readerへ渡しません。legacy runをstrict v2として再解釈しません。
- Windows symlink作成権限がなかった2件はSKIPです。全pytest suiteと実機・本番runtime受入は未実施です。

## 次工程

I5-Bでmanifest v1/v2を扱う全readerを監査・接続し、strict reservationからworker開始までの順序を決めます。I7でDBOS mutation/checkpoint/recoveryを接続し、I8で隔離実機受入を行います。各後続作業までD1はDRAFT、strict run admissionはOFFです。

## ChatGPT GitHubレビュー修正 — 2026-11-19

実装commit `982b461ba47bdfa0a62bf6da4d648c47a6545e9c` のレビューで指摘された2件を、I5-A writer/verifierの境界内で修正しました。

1. `StrictWorkflowSnapshotWriter.prepare_snapshot()` は、caller作成の `StrictWorkflowInputNormalization` について、definition identityと自己整合digestだけを検証していました。pinned immutable revisionのCompiledContractを使ってI1 `normalize_input()` を適用し、再正規化後のcanonical JSONが渡されたcanonical JSONと完全一致し、SHA-256も渡されたnormalized digestと一致することを、run directoryの解決・作成前に検証するよう変更しました。default、required、nullable、`additionalProperties`、入力検証はI1/I3へ委譲します。不適合入力は値を含まない `StrictWorkflowSnapshotError` となり、run directoryやsnapshot filesを作りません。
2. `verify_reserved()` は、mutationがない全ledgerでもroot以外のframeがあると拒否していました。`inspect_ledger()` が全frame/state/mutationを検査した後、mutation=0かつstate=0なら正常なframe treeを受け入れます。root frameだけの場合は従来どおりI4-A initial-only inspectorで補強し、mutation=0でstate rowがある場合は拒否します。`verify_pre_reservation()` のroot-only条件とI4-A inspectorは変更していません。

### 今回の回帰結果

SQLite test dataはすべてpytest `tmp_path`内です。本番Bridge/Worker、実Workflow/Codex job、共有またはproduction DB、DBOS checkpoint、WSL、VM、Dockerは使用していません。

| 検証 | 結果 |
| --- | --- |
| I5-A専用suite `tests/test_workflow_d1_i5a_manifest.py` | PASS — 45 passed（従来33件に対し12件増）、FAIL 0。SKIP — 2（Windows symlink権限） |
| I1/I3/I4 focused regression（8 files: I1 contract core、I3-A contract storage、I3-B request storage、I3-C binding、I4-A variable schema、I4-B frames/logic、I4-C mutations） | PASS — 246 passed、FAIL 0。SKIP — 2（Windows symlink権限） |
| `uv run --no-sync ruff check src/codex_bridge tests/test_workflow_d1_i5a_manifest.py` | PASS |
| `uv run --no-sync ruff format --check src/codex_bridge/workflows/strict_manifest.py tests/test_workflow_d1_i5a_manifest.py` | PASS |
| `uv run --no-sync mypy src/codex_bridge` | PASS — 142 source files |
| `uv run --no-sync python -m compileall -q src/codex_bridge` | PASS |
| `git diff --check` | PASS |
| Full pytest suite | NOT_RUN |

追加テストは、I3正規化結果を使う通常パスとnullable `null`、自己hashが整合するcaller作成dataclassのschema違反/default不足/required不足/不許可`null`/additional property違反、異常時の値非漏洩と新run directory未作成を確認します。また、予約済みrunにおけるmutationなしのACTIVE/CLOSED child frame受入、child追加後のpre-reservation拒否、frame identity/path/parent reference改ざんの拒否、mutationなしのstate row拒否、既存nonempty ledger経路を確認します。

上記の従来33 PASS/2 SKIPおよび372 PASS/3 SKIPは前commitの過去結果としてそのまま保全し、レビュー修正後の45 PASS/2 SKIPおよび246 PASS/2 SKIPとは区別しています。結果は合算していません。

## ChatGPT GitHub受入判定 — 2026-11-19

レビュー対象は実装commit `982b461ba47bdfa0a62bf6da4d648c47a6545e9c` とレビュー修正commit `2fda398abbce7299c5f1e3ae0a565aebd04ab6e9` です。判定は **ACCEPTED (with documented caveats)** です。pinned contractに基づく入力再正規化とcaller入力との完全一致検証、およびmutation 0件で合法なchild frameを持つreserved runの受入を確認しました。修正後のI5-A専用suite 45 PASS/2 SKIP、I1/I3/I4 focused regression 246 PASS/2 SKIP、Ruff/mypy/compileallは記録済みの結果どおりPASSです。

受入時点の留保は次のとおりです。

- 複数snapshot filesとSQLite DB間のatomicityはありません。read-only SQLite参照でもWAL sidecar metadataが変わる可能性があります。
- I5-B reader接続、DBOS接続と実checkpoint対応は未実施です。reserved verifierはDBOS checkpointを検証しません。
- Windows symlink権限により2件はSKIPです。full pytest suiteはNOT_RUNです。
- I5-Aはisolated opt-in APIです。strict run admissionはOFFのままで、既存service readerへ接続していません。

I5-Aは `ACCEPTED (with documented caveats)`、I5全体は `IN_PROGRESS`、I5-Bは未着手です。次工程はI5-B1のcommon run format discriminatorとservice reader gateであり、B1完了時点でも全reader互換完了とは扱いません。D1全体は `DRAFT`、strict run admissionは `OFF` です。

この受入記録を追加したdocs-only commitでは、`git diff --check`を実行し、pytest/Ruff/mypy/compileallはNOT_RUNです。これは上記のI5-A実装・修正commitで記録された検証実測とは別です。
