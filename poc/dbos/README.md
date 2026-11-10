# DBOS 隔離PoC

このディレクトリは、CodexBridge本体へ自動起動を追加せず、Windows上の専用Python環境でDBOSのworkflow/step実行とSQLiteへの状態保存を確かめるPoCです。GitHub、MCP、Codex、外部APIは呼び出しません。

## 実測環境

- Python: `3.14.10`
- uv: `0.13.7`
- DBOS: `3.4.0`
- pytest: `9.2.0`
- Ruff: `0.17.4`
- 依存の固定一覧: [`requirements.txt`](requirements.txt)（入力: [`requirements.in`](requirements.in)）

## 再現用 PowerShell コマンド

リポジトリルート `D:\CodexBridge-PoC` で実行します。

```powershell
uv venv .\poc\dbos\.venv --python 3.14
uv pip sync --python .\poc\dbos\.venv\Scripts\python.exe .\poc\dbos\requirements.txt
& .\poc\dbos\.venv\Scripts\python.exe .\poc\dbos\smoke.py
& .\poc\dbos\.venv\Scripts\python.exe -m pytest -q -c .\poc\dbos\pytest.ini .\poc\dbos\test_smoke.py
& .\poc\dbos\.venv\Scripts\ruff.exe check .\poc\dbos\smoke.py .\poc\dbos\test_smoke.py
& .\poc\dbos\.venv\Scripts\ruff.exe format --check .\poc\dbos\smoke.py .\poc\dbos\test_smoke.py
& .\poc\dbos\.venv\Scripts\python.exe -m compileall -q .\poc\dbos\smoke.py .\poc\dbos\test_smoke.py
```

スモークは公式の [`DBOS(config=...)`](https://docs.dbos.dev/python/integrating-dbos) と `DBOS.launch()`、`@DBOS.workflow()`、`@DBOS.step()` を使用します。既定のSQLiteファイルは `poc/dbos/.state/dbos-poc.sqlite` です。`--db-path` で保存先を指定できます。テストは別プロセスで一時SQLiteへ実行し、DBOSテーブルと記録行を読み取り専用で確認します。

検証時は2回のworkflow実行後、`workflow_status` 2行と `operation_outputs` 6行（各workflowの3ステップ分）を確認しました。`.venv/` と `.state/` はこのディレクトリの `.gitignore` で除外しています。Phase 1の強制停止・復旧実験は次節を参照してください。

## Phase 1 強制終了・自動復旧

### 実行

リポジトリルート `D:\CodexBridge-PoC` から実行します。毎回新しい `run-<UUID>` ディレクトリを作り、Phase0の `dbos-poc.sqlite` は使いません。

```powershell
& .\poc\dbos\.venv\Scripts\python.exe .\poc\dbos\recovery_test.py --state-dir .\poc\dbos\.state\recovery-runs
& .\poc\dbos\.venv\Scripts\python.exe -m pytest -q -c .\poc\dbos\pytest.ini .\poc\dbos\test_smoke.py .\poc\dbos\test_recovery.py
& .\poc\dbos\.venv\Scripts\ruff.exe check .\poc\dbos\recovery_config.py .\poc\dbos\recovery_worker.py .\poc\dbos\recovery_test.py .\poc\dbos\test_recovery.py
& .\poc\dbos\.venv\Scripts\ruff.exe format --check .\poc\dbos\recovery_config.py .\poc\dbos\recovery_worker.py .\poc\dbos\recovery_test.py .\poc\dbos\test_recovery.py
& .\poc\dbos\.venv\Scripts\python.exe -m compileall -q .\poc\dbos\recovery_config.py .\poc\dbos\recovery_worker.py .\poc\dbos\recovery_test.py .\poc\dbos\test_recovery.py
```

### 時系列と判定

1. 初回workerは固定の `application_name=codexbridge-dbos-recovery-poc`、`application_version=1.0.0`、`executor_id=codexbridge-dbos-recovery-poc-executor` と専用SQLiteを使い、workflowを1件開始します。worker関数は同じ `recovery_worker.py` から両プロセスで登録します。
2. step1とstep2の開始を独立した `step-events.jsonl` に追記し、各行をflush・fsyncします。step2は継続マーカーを待ちます。親はstep1のSQLite完了記録とstep2開始イベントを確認してから、自身が `subprocess.Popen` で起動した初回workerだけに `Popen.kill()` を呼び、5秒以内に回収します。
3. kill後は初回workerのPython PIDを記録するheartbeatが0.5秒間更新されないことを確認します。続いて、復旧workerを起動する前、継続マーカーを作る前にSQLiteを読み取り、workflowが1件で `PENDING`、完了済みoperationがstep1のみ、step2未完了であることを再確認します。開始イベントもstep1・step2の2件だけであることを確認します。
4. その後に継続マーカーを作り、新しい「復旧専用」workerを起動します。このモードは新規workflow開始APIを呼ばず、同じDBOS設定と登録済みworkflowで `DBOS.launch()` の自動復旧だけを行います。workflowが `SUCCESS` になるまで最大30秒待ち、SQLiteとイベントログの両方で結果を照合します。

既定のphase timeoutは初回・復旧それぞれ30秒、step2のマーカー待ちは最大120秒、初回worker回収待ちは5秒です。`--phase-timeout-seconds` でphase timeoutを指定できます。pytest統合テストの外側timeoutは90秒です。失敗時のSQLite、JSONL、子プロセスstdout/stderr、前後のスナップショットは各runディレクトリに残ります。

runディレクトリには専用DB `dbos-recovery.sqlite`、step開始ログ `step-events.jsonl`、初回worker heartbeat `initial-worker-heartbeat.jsonl`、継続マーカー `continue-step2.marker`、初回・復旧前後のSQLiteスナップショット、workerログを保存します。すべて `.state/` の下にあり、既存 `dbos-poc.sqlite` は変更しません。

### 実測結果

2026-11-10の実行 `poc/dbos/.state/recovery-runs/run-ecfc7190ee3046c3bb4f96e525fbadca` では、workflow `01a1c460-517e-728c-8ef3-77c88a918daa` を初回 `PENDING` から復旧後 `SUCCESS` にできました。復旧前後とも `workflow_status` は同じworkflowの1行だけで、復旧前の `operation_outputs` は完了済みstep1のみ、step2は未完了でした。復旧後の `operation_outputs` はstep1・step2・step3の完了3行でした。

JSONL開始イベントの実測回数はstep1が1回、step2が2回、step3が1回です。初回workerは親のPopen PID `18880` / worker内Python PID `28736`、復旧workerはPopen PID `7116` / worker内Python PID `11668` でした。この環境では親側 `Popen.pid` と子側 `os.getpid()` の値が一致しませんでした。値の不一致を無視して成功扱いにはせず、親が保持する初回Popenに対する `kill()` と終了・回収（戻り値1）を確認し、初回worker PIDのheartbeatが0.5秒間停止したこと、marker作成前のSQLite `PENDING`・step2未完了、復旧開始までイベントが2件だけであることをそれぞれ検証しています。復旧は別のPopen・別のworker PIDで実行されました。

この実測で確認したのは、SQLite checkpointを使ったDBOS workflowの再起動後の再開と、途中step2の再実行です。Codex/Gitなどの外部副作用に対する厳密な二重実行防止は未検証です。

## Phase 2 外部副作用・再試行・UNKNOWN

Phase 2は外部サービスを別SQLiteで模擬し、DBOSのworkflow checkpointと外部副作用が別トランザクションである障害窓を検証します。外部サービス、Codex App Server、GitHub、MCP、HTTPへ接続しません。各実行は独立した `run-<UUID>` ディレクトリに保存します。

### 実行

リポジトリルート `D:\CodexBridge-PoC` から各ケースを単独実行します。

```powershell
& .\poc\dbos\.venv\Scripts\python.exe .\poc\dbos\side_effect_test.py --case idempotent_recovery
& .\poc\dbos\.venv\Scripts\python.exe .\poc\dbos\side_effect_test.py --case transient_retry
& .\poc\dbos\.venv\Scripts\python.exe .\poc\dbos\side_effect_test.py --case unqueryable_unknown
& .\poc\dbos\.venv\Scripts\python.exe -m pytest -q -c .\poc\dbos\pytest.ini .\poc\dbos\test_side_effects.py
```

driverはSQLiteを直接読み戻してから`PASS`を出します。pytestも各DBの行、workflow ID/status、request key、effect名/値、合計カウンタ、step entry、再試行またはUNKNOWN証跡を照合します。失敗したrunは削除せず証跡を残します。

### ケースと期待値

| ケース | 実験 | 期待される実測 |
| --- | --- | --- |
| `idempotent_recovery` | `workflow_id:charge`をkeyに外部ledgerへ1回commitし、step return前に親が初回workerをkill。再開workerは新規workflowを作らず`DBOS.launch()`だけで復旧し、同じkeyを外部ledgerから照合 | 1 workflow、初回と復旧のstep entry各1回、最終`SUCCESS`、完了step checkpoint 1件、外部effect 1件/合計41、lookup `MISS`→send `APPLIED`→lookup `HIT` |
| `transient_retry` | 副作用前に外部SQLiteで一時障害を2回注入 | DBOS標準step retryを`max_attempts=3`で使用し、3回目に成功。step entry 3回、`TransientServiceError` 2回、外部effect 1件/合計41 |
| `unqueryable_unknown` | 外部SQLiteへ非冪等sendを1回commitした直後、adapterの受領結果とDBOS step checkpointが未完了の状態で親が初回workerをkill | 復旧workerは外部照会・再送をせずadapterを`UNKNOWN`にし、workflowは実測どおり`ERROR`。外部send/effectは1回、step entryは2回、DBOSは失敗を`operation_outputs.error`に記録 |

DBOS 3.4.0の`DBOS.step(retries_allowed=True, max_attempts=3, should_retry=...)`を使用します。再試行predicateは`TransientServiceError`だけを許可し、実測では合計3 attempt（初回を含む）でした。UNKNOWNはこのpredicateで再試行されません。ケースCの初回kill前は`PENDING`かつstep出力/error checkpointなしです。復旧workerがUNKNOWNを検出して例外にした後、DBOSはその失敗をerror checkpointとして保存し、workflow statusは`ERROR`になりました。

### 証跡と制限

既定の保存先は `poc/dbos/.state/phase2-runs/run-<UUID>/` です。pytest実行も `.state/phase2-pytest-<case>/run-<UUID>/` の下に保存し、既存`.gitignore`で除外されます。runごとにworkflow DB `workflow.sqlite`と架空外部サービスDB `fake-external.sqlite`は別ファイルです。ケースCだけadapterの`adapter-state.sqlite`も追加します。JSONL `worker-events.jsonl`はworker内のstep entry、intent、error、UNKNOWN拒否を記録し、初回・復旧workerのheartbeat、worker stdout/stderr、`before-kill.json`、`before-recovery.json`、`final-evidence.json`、`result.json`を同じrunに保存します。

親は自分の`subprocess.Popen`で起動した子にだけ`Popen.kill()`し、waitで回収します。kill点は外部DBのeffect/合計とDBOSの`PENDING`・未checkpoint、ケースCではadapterのintent・receiptなしを確認して決めます。終了後は子のheartbeatが停止したことを確認します。親Popen PIDとworkerのPython PIDの一致は前提にせず、両方の値を記録します。タイムアウト時も親が所有する子だけをfinallyで回収します。

ケースCではworker/adaptorは外部DBを監査照会に使いません。pytest/driverが子worker終了後に外部SQLiteを監査し、effect 1件とsend 1回を確認します。ケースAは外部ledger lookupとrequest keyの一意制約で重複副作用を防ぎます。このPoCが示すのは当該fake service契約での回復・限定再試行・UNKNOWN停止であり、任意の外部サービスに対するExactly-once保証ではありません。実Codex接続は行いません。

### 2026-11-10 実測

3ケースをdriverから個別に実行し、各SQLiteとJSONLを読み戻しました。

| ケース | run / workflow ID | DBOS結果・step entry | retry / UNKNOWN | 外部SQLite |
| --- | --- | --- | --- | --- |
| `idempotent_recovery` | `run-323ff1ea88fd4b2f8587ca8d435026c1` / `01a1c47d-5c23-754f-8d84-4ff979b26fc6` | 1 workflow、初回`PENDING`・step未checkpointから復旧、最終`SUCCESS`、entry 2回 | retry 0。親が初回子をkill/waitしheartbeat停止を確認。復旧側の新規workflow開始なし | request key `01a1c47d-5c23-754f-8d84-4ff979b26fc6:charge`、effect 1件、合計41。lookup `MISS`、send `APPLIED`、復旧lookup `HIT` |
| `transient_retry` | `run-021c8a7b71d34bc39f3c2d4e6436b324` / `01a1c47d-7c85-77f2-8041-5371baf65bb6` | 1 workflow、最終`SUCCESS`、entry 3回 | `TransientServiceError` 2回、3回目成功。retry 2 | effect 1件、合計41。外部call結果は`TRANSIENT_ERROR`, `TRANSIENT_ERROR`, `APPLIED` |
| `unqueryable_unknown` | `run-b1fede46a7cf4f5e9f13f6512a571089` / `01a1c47d-8fbd-7039-99be-83419a47d236` | 1 workflow、初回`PENDING`・step未checkpointから復旧、最終`ERROR`、entry 2回 | retry 0。adapterは`UNKNOWN`、証跡`dispatch intent exists without a local receipt; no retry`。workerの再送/照会なし | 監査専用readbackでsend/effect各1回、合計41。外部callは初回`send_unqueryable`のみ |

3 runともPopen PIDとworker内Python PIDは不一致でした。driverは一致を仮定せず、自分が所有するPopenへのkill/waitとheartbeat停止で初回子の終了を確認しました。各runの詳細は `poc/dbos/.state/phase2-runs/<run>/` にあります。
