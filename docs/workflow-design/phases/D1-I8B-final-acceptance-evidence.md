# D1-I8-B final isolated acceptance cleanup and go/no-go evidence

- **実装状態:** ACCEPTED with documented caveats by ChatGPT review of `679df0a42929162f9bc7ee01b0e66e0b6db5ebe5`。
- **対象branch:** `codex/workflow-d1-i5-b4-verified-readers`。
- **base:** `afda1d9d05ee126ec1ceb5d81bab9b6a5879ac7f` (I8-A accepted with documented caveats)。
- **範囲:** UI server test-doubleの3 failure修正、D1-22 mock child output contract境界、PD-01..39 traceability、I8 final isolated gateと次段operator rollout計画。
- **全体境界:** I7-C2/I7、I8-A、I8-BはACCEPTED with documented caveats。D1 current development scopeはACCEPTED / isolated GOです。D1詳細設計の文書状態はformal production approval前のDRAFTを維持します。operational migrationとproduction enablementは別途明示承認待ち、I2はDEFERRED / NON-BLOCKING、public/production strict admissionはOFFです。

## Review記録

ChatGPT GitHub review of `afda1d9d05ee126ec1ceb5d81bab9b6a5879ac7f` はI7-C2/I7をACCEPTED with documented caveatsとし、profile-1 identity、pending Codex job reconciliation、ownership照合、およびisolated worker process-death recoveryを含むI8-A integrationも受け入れました。Full suiteは3,955 passed / 24 skipped / 3 failedであり、all-greenではありませんでした。3件は未変更production `server.py`の`drain_final_answer_writes(close=True)`呼出しと`tests/test_ui_server.py`のFakeBridgeの欠落によるtest-only mismatchでした。baselineを実行して同じfailureだったとは主張しません。

I8-Bではレビュー指定どおりtest doubleだけを修正し、production runtimeを変更していません。この証跡の初版作成時点ではChatGPT review待ちでした。後続のreview結果と現在の受入状態を次項に記録します。

ChatGPT GitHub review of `679df0a42929162f9bc7ee01b0e66e0b6db5ebe5` はI8-BおよびI8 isolated acceptanceをACCEPTED with documented caveatsとしました。D1 current development scopeもACCEPTED / isolated GOです。これはproduction acceptanceではなく、operational migrationとproduction enablementは別途明示的なユーザー承認待ちです。review留保はこの文書の全体suite、D1-22、PD crosswalk、rollout節に整理しています。

## UI server test-double修正

`tests/test_ui_server.py`のFakeBridgeに`drain_final_answer_writes(*, close: bool)`を追加し、close引数とeventを記録します。live SSE shutdown、通常runtime shutdown、UI startup failureの3ケースで、既存のapp/UI/Bridge orderとSSE/exception assertionsを維持し、`app.shutdown`の後にdrainが一度呼ばれ`close=True`であることを検証します。

## D1-22 mock child return-contract範囲

D1詳細設計の原文は「子Workflow return境界の契約模擬: child outputをSchema検証し、明示returnのみ親へ渡す」です。`tests/test_workflow_d1_i1_contract_core.py::test_d1_22_mock_child_output_is_validated_at_contract_boundary`は既存`compile_contract` / `validate_output`を使い、mock childのvalid outputを受け入れ、Schema-invalid outputを`ContractOutputInvalid`として拒否します。

現行runtimeにはsubflow childを呼び出す境界、explicit return mapping、親contextへのhandoff primitiveがありません。このtestはcontract validation半分のみです。親へexplicit returnだけを渡す意味はD6実subflow実装時の残件として明示し、D1-22全体のPASSとは扱いません。別のtest-only return semanticsやsubflow実装は追加していません。

## PD-01..39 crosswalk

この表は各Proposed Decisionを既存test pathsへ紐づけ、現行実装test、非実施のoperator gate、DEFERRED項目を分けたものです。suite実行は各PDの個別設計承認ではありません。

| PD IDs | Existing evidence paths / cases | I8-B status |
| --- | --- | --- |
| 01..03 | `test_workflow_d1_i3a_contract_storage.py`, `test_workflow_d1_i3c_binding.py`, `test_workflow_d1_i6a_definition_repository_http.py`, `test_workflow_d1_i6b_mcp_repository_selection.py` — contract row, digest/binding, legacy-to-strict and mutation replay | 現行隔離CRUD/binding/replay testsを実行 |
| 04..05 | `test_workflow_d1_i1_contract_core.py` — schema dialect, allowed references, `additionalProperties`, defaults and normalization | 現行pure contract testsを実行 |
| 06..10 | `test_workflow_d1_i1_contract_core.py`, `test_workflow_d1_i4b_variable_logic.py`, `test_workflow_d1_i4c_mutations.py`, `test_workflow_d1_i7a_variable_call_validation.py`, `test_workflow_d1_i7b_variable_runtime.py` — declaration/scope/operation, execution identity, secret rejection | 現行validator/runtime/cap testsを実行 |
| 11..17 | `test_workflow_d1_i4a_variable_schema.py`, `test_workflow_d1_i4c_mutations.py`, `test_workflow_d1_i5a_manifest.py`, `test_workflow_d1_i5b4a_verified_readers.py`, `test_workflow_d1_i5b4c_reserved_readers.py`, `test_workflow_d1_i7c1_durable_execution.py`, `test_workflow_d1_i7c2_isolated_runtime.py` — store binding, reservation, replay and uncertainty | 現行isolated testsを実行。複数DB atomicityは主張しない |
| 18..22 | `test_workflow_d1_i3a_contract_storage.py`, `test_workflow_d1_i3b_request_storage.py`, `test_workflow_d1_i4a_variable_schema.py`, `test_workflow_d1_i1_contract_core.py::test_d1_22_mock_child_output_is_validated_at_contract_boundary` — isolated migration/schema checks and mock child output validation | migration/schema testsを実行。D1-22 parent return handoffはD6へdefer |
| 23 | `docs/workflow-design/decisions/0004-sqlite-ddl-and-schema-migration.md` §11 and §§15-16; isolated backup/migration tests above | future shared DB operator gate。実writer/worker stop、shared backup/restore、production migrationは未実施 |
| 24..27 | `test_workflow_d1_i2_windows_guard.py` covers only the current non-Windows gate condition | I2 Global Mutex, old-writer/controller exclusion, explicit DACL/ACE/SID and handle/TOCTOU ideas remain DEFERRED / NON-BLOCKING; no new I2 protection was added |
| 28..29 | `test_workflow_d1_i1_contract_core.py`, `test_workflow_d1_i4c_mutations.py`, `test_workflow_d1_i7b_variable_runtime.py` — existing configured caps and rollback before mutation | existing logical caps are tested. Hard physical WAL/SHM byte guarantee and WAL metadata invariance remain DEFERRED / NOT_TESTED |
| 30..34 | `test_workflow_d1_i6a_definition_repository_http.py`, `test_workflow_d1_i6b_mcp_repository_selection.py`, `test_workflow_definition_api.py`, `test_workflow_definition_mcp.py` — contract wire, validate/create/update, ETag and legacy field sets | 現行HTTP/MCP regression testsを実行 |
| 35..38 | `test_workflow_d1_i6c_saved_run_contract.py`, `test_workflow_d1_i7c2_isolated_runtime.py`, `test_workflow_d1_i8a_integration.py`, `test_workflows_api.py`, `test_workflow_mcp.py` — normalized saved-run replay, output/result/status, projections, permissions and caps | 現行HTTP/MCP/isolated worker testsを実行。task/event projectionはstatic partial/emptyのまま |
| 39 | `test_workflow_d1_i7a_variable_call_validation.py`, `test_workflow_d1_i7b_variable_runtime.py`, `test_workflow_d1_i7c1_durable_execution.py`, `test_workflow_d1_i7c2_isolated_runtime.py` — strict variable call allowlist/ledger/DBOS replay | 現行strict-only validator/runtime testsを実行 |

PD-23の隔離DB testは将来の共有DB運用承認を代替しません。PD-24..27とPD-29のphysical WAL/SHM要件は明示的にdeferredで、I8 blockerではありません。D1-22のexplicit parent handoffもD6へ残します。

## 全体suiteとcheck結果

| 検査 | 結果 |
| --- | --- |
| `tests/test_ui_server.py` | 9 passed。3 drain callは`app.shutdown`の後、`close=True` |
| `test_d1_22_mock_child_output_is_validated_at_contract_boundary` | 1 passed。child output Schema boundaryのみ |
| Full `uv run --no-sync pytest` | 3,959 passed / 24 skipped / 0 failed、3,983 collected (Python 3.14.10 / Windows native、54:38) |
| Skips | 24。ほとんどはWindows symlink privilege unavailable。追加で既存I2 non-Windows fail-closed pathはWindows上でskip |
| CPP/account-auth | `test_cpp_mcp_auth.py`とaccount-auth suitesを含めfull pytest内でpass。live auth/serviceは未実施 |
| `uv run --no-sync ruff check tests/test_ui_server.py tests/test_workflow_d1_i1_contract_core.py` | PASS |
| `uv run --no-sync ruff format --check tests/test_ui_server.py tests/test_workflow_d1_i1_contract_core.py` | PASS — 2 files already formatted |
| mypy `src/codex_bridge` | PASS — 156 source files |
| `python -m compileall -q src/codex_bridge` | PASS |
| `git diff --check` | PASS |

## 次段のoperator rollout plan（未実施）

このcompatibility/configuration follow-upでは実state rootやproduction/shared DBへ再アクセスせず、変更もしていません。以下は2026-11-26に得たread-only preflight結果の記録です。将来のmigration/enablementは別途ユーザー承認後に行います。

2026-11-26の実state root preflightで、`definitions.sqlite`は未作成、`requests.sqlite`は`user_version=0`の早期legacy v0（`workflow_requests`のみ、`workflow_request_sources`なし）と確認されました。保存requestは1件でstateが`running`でしたが、同じrunの保存済みworker/DBOS evidenceはworker完了・DBOS `SUCCESS`・job `SUCCEEDED`を示していました。この不一致は運用時に既存evidenceを照合して解決し、SQLでstateを直接変更しません。実DBのschema変更・初期化・再起動は行っていません。read-only観測中に`requests.sqlite-shm`のmtime進行も記録されましたが、原因は特定しておらず、sidecar metadataの不変性は主張しません。

今回のoffline follow-upは、バックアップ/復元/明示migrationが受け入れるv0を、完全legacy（request source tableあり）と、上記のcanonical early-v0（同じcanonical request table/indexesでsource tableだけ欠落）の2種類に限定しました。early-v0のbackup/restoreは欠落状態を保持し、migrationだけがtransaction内で空のsource/binding tablesを追加します。新しい保存sourceやstrict bindingは捏造しません。その他の欠落・不正schema、v0 binding、version不一致は拒否します。この互換修正はproduction DB上では実行していません。

ChatGPT review of `4d751ee4530b2df1d19f9254116b3e6f0cc3b30f` はearly-v0 migration compatibilityをACCEPTEDとしました。focused request storage/migration regressionは52 passedで、full pytestは再実行していません。PR #195により`origin/dev`のmerge commitは`9f95a698ea2d4e171b13fb201a317464117a8c20`です。

1. すべてのactive taskが完了した後、別途承認を得てBridge、全Workflow worker、その他known writersを停止し、Codex taskも残っていないことを確認します。DB lockや`user_version`だけでwriter停止の証明とは扱いません。
2. 今回のconfig wiringがChatGPT review後に`origin/dev`へ入ったことを確認し、停止中に`D:\CodexBridge` checkoutをfast-forwardで更新します。実設定は変更せず、migrationと初回再起動は両strict flagをfalseにしたまま進めます。
3. 既知のstate root `C:\Users\weito\AppData\Local\CodexBridge\workflows`を使う前に、read-only inventoryでDB存在、`user_version`、object names、journal mode、definition/revision/request/source/binding countsとrequest-state countsを記録します。rootが変わっていたら停止して再確認します。request v0はcanonical complete/earlyのどちらかを完全照合し、既存`running` rowとterminal evidenceの不一致を解決するまで進めません。既知schema/version以外は移行しません。
4. definitions/requestsのbackupはSQLite online backup APIを使い、停止中の同一セットとして採取・検証します。backup pathはstate root外に置きます。`requests.sqlite-wal`や`requests.sqlite-shm`をfile copyしません。migration APIが全rowid/field identityをbackup・移行前後で照合します。
5. 承認された場合にのみ既存explicit facilitiesを使います。`definitions.sqlite`が存在しない場合は`StrictWorkflowDefinitionStore.initialize_new_database()`でv2を新規初期化し、migrationとは扱いません。既存definitions v1→v2は`StrictWorkflowDefinitionStore.migrate_v1_to_v2(backup_path=...)`、requests legacy v0→v1は`StrictWorkflowRequestStore.migrate_v0_to_v1(backup_path=...)`を使用します（request migrationは検証済みearly-v0を含みます）。各DB内transactionは独立です。片方だけ成功/失敗した場合はstrict runを許可せず状態を調査し、自動downgradeしません。既にtarget versionのDBは再migrationしません。
6. migration/init後に両DBのversion/object inventory、integrity/FK、legacy revision/hash/ETagとrequest/source identitiesを再検査します。run-local variables DB/manifest/binding/reader互換も確認します。保存rowのstateを直接SQL更新しません。
7. Bridge再起動時のTOMLは両strict flagをfalseに保ち、既存read-only authenticated service endpointsへのGETだけでlist/detail statusを照合します。POST/実Workflow再実行はせず、status evidenceは値やtokenを含めず記録します。strict runtimeの有効化、Codex job flag、actual dogfoodはさらに別の明示承認後です。

### PowerShell / existing venv Python command examples（将来の手順、未実行）

以下のコマンドは手順例としてのみ記録し、この作業では実行していません。実際に使うときは、すべてのknown writer停止後、別途migration承認を得てから実行します。使用するPythonはcheckout内の既存`.venv`です。backupはstate rootの外側に一意なdirectoryを作ります。

```powershell
$RepoRoot = 'D:\CodexBridge'
$Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$StateRoot = 'C:\Users\weito\AppData\Local\CodexBridge\workflows'
$RequestsDb = Join-Path $StateRoot 'requests.sqlite'
$DefinitionsDb = Join-Path $StateRoot 'definitions.sqlite'
$BackupRoot = Join-Path (Split-Path -Parent $StateRoot) ("workflow-rollout-backups\" + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $BackupRoot -Force | Out-Null
Set-Location $RepoRoot
if (-not (Test-Path $Python)) { throw 'Existing repository venv Python is unavailable' }
```

Stopped state rootのread-only inventoryでは`mode=ro`、`query_only=ON`、短いbusy timeoutを使います。row内容、prompt、inputは表示しません。

```powershell
$Inventory = @'
import sqlite3, sys
from pathlib import Path
p = Path(sys.argv[1])
if not p.exists():
    print(p.name, "absent")
    raise SystemExit(0)
c = sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.75)
try:
    c.execute("PRAGMA query_only=ON")
    print(p.name, "user_version", c.execute("PRAGMA user_version").fetchone()[0])
    print(p.name, "journal_mode", c.execute("PRAGMA journal_mode").fetchone()[0])
    print(p.name, "objects", c.execute(
        "SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ).fetchall())
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in (
        "workflow_definitions", "workflow_definition_revisions", "workflow_definition_contracts",
        "workflow_requests", "workflow_request_sources", "workflow_request_contract_bindings",
    ):
        if table in tables:
            print(table, "count", c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    if "workflow_requests" in tables:
        print("request_states", c.execute(
            "SELECT state,COUNT(*) FROM workflow_requests GROUP BY state ORDER BY state"
        ).fetchall())
finally:
    c.close()
'@
foreach ($Db in @($DefinitionsDb, $RequestsDb)) {
    $Inventory | & $Python - $Db
    if ($LASTEXITCODE -ne 0) { throw "Read-only inventory failed for $Db" }
}
```

このinventoryはschema/object/countだけを出します。`mode=ro`でもSQLiteが既存SHM metadataを更新する可能性があるため、その不変性は主張しません。migration backupはonline backup APIで採取し、live WAL/SHM sidecarはcopyしません。early-v0 backup/restore rehearsalと明示migrationは次のAPI例です。migration backup pathは既存でない固有名にします。

```powershell
$RequestVerifyBackup = Join-Path $BackupRoot 'requests-early-v0-verify.sqlite'
$RequestMigrationBackup = Join-Path $BackupRoot 'requests-before-v1.sqlite'
$RestoreRoot = Join-Path $BackupRoot 'requests-restore-check'
$BackupCheck = @'
import sqlite3, sys
from pathlib import Path
from codex_bridge.workflows.strict_requests import StrictWorkflowRequestStore
root, backup, restore = map(Path, sys.argv[1:])
uri = (root / 'requests.sqlite').resolve().as_uri() + '?mode=ro'
c = sqlite3.connect(uri, uri=True, timeout=0.75)
try:
    c.execute('PRAGMA query_only=ON')
    version = c.execute('PRAGMA user_version').fetchone()[0]
finally:
    c.close()
if version != 0:
    raise SystemExit('Expected a re-inventoried requests v0 DB before backup rehearsal')
store = StrictWorkflowRequestStore(root)
store.create_verified_backup(backup)
store.restore_verified_backup(backup, destination_state_root=restore)
'@
$BackupCheck | & $Python - $StateRoot $RequestVerifyBackup $RestoreRoot
if ($LASTEXITCODE -ne 0) { throw 'Verified early-v0 backup/restore failed' }

$MigrateRequests = @'
import sqlite3, sys
from pathlib import Path
from codex_bridge.workflows.strict_requests import StrictWorkflowRequestStore
root, backup = map(Path, sys.argv[1:])
db = root / 'requests.sqlite'
if not db.exists():
    raise SystemExit('requests.sqlite is absent; do not initialize it in this migration path')
c = sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=0.75)
try:
    c.execute('PRAGMA query_only=ON')
    version = c.execute('PRAGMA user_version').fetchone()[0]
finally:
    c.close()
store = StrictWorkflowRequestStore(root)
if version == 0:
    store.migrate_v0_to_v1(backup_path=backup)
elif version == 1:
    store.inspect()
else:
    raise SystemExit('Unknown requests schema version; stop without migration')
info = store.inspect()
print({"user_version": info.user_version, "requests": info.request_count,
       "sources": info.source_count, "strict_bindings": info.strict_binding_count})
'@
$MigrateRequests | & $Python - $StateRoot $RequestMigrationBackup
if ($LASTEXITCODE -ne 0) { throw 'Explicit request v0-to-v1 migration failed' }
```

Definitionsがpreflightどおり不在なら、新規v2初期化だけを明示実行します。既存ならversionを再確認し、v1だけ`migrate_v1_to_v2(backup_path=...)`、v2は再migrationせず、unknown/corruptなら停止します。

```powershell
$DefinitionsMigrationBackup = Join-Path $BackupRoot 'definitions-before-v2.sqlite'
$PrepareDefinitions = @'
import sqlite3, sys
from pathlib import Path
from codex_bridge.workflows.strict_definitions import StrictWorkflowDefinitionStore
root, backup = map(Path, sys.argv[1:])
db = root / 'definitions.sqlite'
store = StrictWorkflowDefinitionStore(root)
if not db.exists():
    store.initialize_new_database()
else:
    c = sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=0.75)
    try:
        c.execute('PRAGMA query_only=ON')
        version = c.execute('PRAGMA user_version').fetchone()[0]
    finally:
        c.close()
    if version == 1:
        store.migrate_v1_to_v2(backup_path=backup)
    elif version != 2:
        raise SystemExit('Unknown definitions schema version; stop without migration')
store.list(limit=1)
print('definitions schema read-verified')
'@
$PrepareDefinitions | & $Python - $StateRoot $DefinitionsMigrationBackup
if ($LASTEXITCODE -ne 0) { throw 'Explicit definitions prepare/migration failed' }
```

初期化/migration後は前掲inventoryとread-only repository/store readersでversion、object schema、aggregate countsを照合します。request store migrationはbackupと同一のlegacy identityを内部照合し、staleな`running` rowをSQLで修正しません。すべての照合後、TOMLの両strict flagをfalseにしたままBridgeを起動し、既存control tokenをPowerShell変数へ安全に渡した上でread-only GETだけを行います。

```powershell
$Headers = @{ Authorization = "Bearer $ControlToken" } # 既存の安全なsecret sourceから取得し、表示しない
$List = Invoke-RestMethod -Method Get -Uri 'http://127.0.0.1:8001/ui-api/control/workflows' -Headers $Headers
$List.workflows | Group-Object status | Select-Object Name, Count
# 必要なら、既存のrun IDをローカル変数へ入れてdetail GETし、statusだけ確認する。POST/result再実行はしない。
# Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:8001/ui-api/control/workflows/$WorkflowId" -Headers $Headers |
#     Select-Object status
```

### Default-OFF runtime config wiring（本branchの実装、ChatGPT review待ち）

`BridgeConfig`と`config.example.toml`に次のTOML設定を追加し、`build_runtime`から既存manager/service opt-inへ渡します。省略時とexample値はfalseです。設定値はTOML booleanのみ受け付け、`strict_codex_jobs_enabled=true`には`strict_runtime_enabled=true`も必要です。Codex jobはさらに既存`control_token`が必要で、job用flagなしでは既存の拒否動作を保ちます。MCP permissionは別設定のままです。新env alias、per-request flag、GUI toggle、managed-config writerは追加していません。

```toml
[workflow]
mcp_permissions = [] # existing value may be retained
strict_runtime_enabled = false
strict_codex_jobs_enabled = false
```

有効化後もprepared definition/request DBが不在ならstartupでschemaを作らず、strict requestだけをunavailableにします。legacy runtimeとBridge startupには影響しません。今回のbranchは設定配線を実装するのみで、実config、migration、restart、runtime enablementは行いません。default OFFからの変更は別途承認・isolated dogfoodの後に扱います。

後日runtimeだけを別途承認された場合は`strict_runtime_enabled = true`、`strict_codex_jobs_enabled = false`とし、Codex jobを含むauthorized isolated dogfoodを別途承認する場合に限り2つ目もtrueにします。runtime用TOMLは`CODEX_BRIDGE_CONFIG`指定path、またはfallback `%APPDATA%\CodexBridge\config.toml`から読みます。今回どちらの設定ファイルも変更していません。

このbranchのconfiguration wiringは既定OFFで、実configと稼働中Bridgeへは未適用です。review後にsourceを更新してDBを準備しても、それだけでstrict executionは有効になりません。両flagの変更とisolated dogfoodは別途明示承認が必要です。

今回のoffline follow-up focused suiteはrequest storage/migrationとlegacy saved-execution regressionの52件がPASSです。実DB、live Bridge/worker、外部Codex job、full pytestは対象外です。既存のI8 isolated GOとproduction rollout保留は変わりません。

Default-OFF config wiring follow-upの最新検証は、config/server/workflow/saved-execution focused batch 148 passed（legacy representative 2件を含む）です。別のisolated strict runtime batch 26 passedにはprepared v2/v1 pure variable DBOS run/recoveryが含まれます。これらは重複するtest pathsを含むため合算しません。Ruff check/format、mypy 156 source files、compileall、diff checkもPASSです。full pytestおよびfake Codex transport suiteは今回再実行していません。このconfig follow-upはChatGPT review待ちです。

## Isolated go/no-go

ChatGPT reviewはI8-BおよびI8 isolated acceptanceをACCEPTED with documented caveatsとし、D1 current development scopeをACCEPTED / isolated GOとしました。full pytest 3,959/24/0、UI server 9/9、D1-22 schema mock 1/1、Ruff、mypy、compileall、diff checkが根拠です。この判定はproduction/public strict admissionの承認ではありません。D1詳細設計はDRAFT、public admission OFF、I2 DEFERREDのままです。PD-23 operator gate、live Bridge/API、real external Codex job、production/shared migration、hard WAL/SHM guarantee、D1-22 parent return routingは未実施または後続sliceです。

## 変更path

- `tests/test_ui_server.py`
- `tests/test_workflow_d1_i1_contract_core.py`
- `docs/workflow-design/phases/D1-I8A-integration-acceptance-evidence.md`
- `docs/workflow-design/phases/D1-I7C2-isolated-runtime-evidence.md`
- `docs/workflow-design/phases/D1-implementation-plan.md`
- `docs/workflow-design/phases/D1-I8B-final-acceptance-evidence.md`
- `config.example.toml`
- `src/codex_bridge/config.py`
- `src/codex_bridge/config_file.py`
- `src/codex_bridge/server.py`
- `tests/test_config.py`
- `tests/test_server.py`
- `tests/test_workflow_d1_i7c2_isolated_runtime.py`
- `src/codex_bridge/workflows/strict_requests.py`
- `tests/test_workflow_d1_i3b_request_storage.py`
