# CodexBridge OWS v1.0.3 + DBOS PoC

Windows上で、OWS DSL v1.0.3の限定subsetをPython Interpreterで実行し、DBOS 3.4.0のworkflow/step checkpointとSQLite復旧を確認するPoCです。完全なOWS runtime互換性は主張しません。`poc/dbos`のPhase 0/1/2、本体、ルート依存には変更を加えません。

## 公式仕様と検証境界

参照した仕様はv1.0.3 tagに固定しています。`main`のv1.0.4以降の構文は使いません。

- [v1.0.3 dsl.md](https://raw.githubusercontent.com/open-workflow-specification/specification/v1.0.3/dsl.md)
- [v1.0.3 dsl-reference.md](https://raw.githubusercontent.com/open-workflow-specification/specification/v1.0.3/dsl-reference.md)
- [v1.0.3 schema/workflow.yaml](https://raw.githubusercontent.com/open-workflow-specification/specification/v1.0.3/schema/workflow.yaml)

公式schemaは上記tagから取得して[`schema/workflow.yaml`](schema/workflow.yaml)に固定しました。取得元URLは`https://raw.githubusercontent.com/open-workflow-specification/specification/v1.0.3/schema/workflow.yaml`、SHA-256は`df3326da907e17e743e9d9e888839481d1557abdf91c592e74f27506ff523f0b`です。`yaml.safe_load`で読み、`jsonschema.Draft202012Validator`で公式schemaを検査します。標準workflow、データフロー、復旧、`mock-job`の各fixtureはこの公式schemaを通ります。

検査は二段階です。まずPoC subsetの静的検査で未対応task/属性/flowを拒否し、続いて公式schemaで構造を検査します。公式schema合格は、このPoCが全OWS意味論を実装したことを意味しません。subsetが実行できる`call`は`mock-job`だけです。HTTP、MCP、他の関数名は実行前に拒否し、`try`など未実装taskも拒否します。

## Interpreter subset

`document.dsl`は文字列`'1.0.3'`に限定します。`evaluate.language=jq`と`evaluate.mode=strict`を明示し、`${ ... }`だけをjq式として本物の`jq` packageに渡します。通常文字列は評価しません。`for.each`と`for.at`の値は`jq.compile(..., args=...)`で`$item`、`$index`等へJSON値として束縛します。

対応するtaskは`do`、`set`、`switch`、`for`と限定された`call: mock-job`です。

- `do`は順次実行し、各taskの出力を次taskの入力にします。入れ子`do`は入れ子scopeで実行します。
- `set`はmapまたは式の値をそのまま次taskへ渡します。辞書を暗黙mergeしません。
- `switch`はcaseを宣言順に評価し、最初に真となったcaseを選びます。`when`を持たないcaseをdefaultとして扱い、case名が`default`かどうかには依存しません。defaultは最後に置きます。
- `for`は`for.in`の結果が配列であることを要求し、順序を保って列挙します。`for.each`/`for.at`で束縛した値を利用して入れ子`do`を実行し、各反復の最後のtask出力を順に配列へ集めます。これはこのPoCの限定出力規則です。
- `then`は同一`do`scope内のtask名へのジャンプ、`continue`、`exit`を扱います。`exit`は現在の`do`scopeを終えます。`end`は未対応として拒否します。存在しないscope外targetと静的に検出できるjump cycleを拒否し、実行task数にも上限を設けます。

### データフローとworkflow context

workflow `input.from`はraw workflow inputをjqで変換し、その結果を最初のtask inputとworkflow contextの初期値にします。workflow `output.as`は最後のtaskの変換済み出力に適用します。どちらも省略時はidentityです。

taskでは`input.from`をraw task inputに適用してからtask本体を実行します。task `output.as`はraw task outputを変換し、その結果が次taskのraw inputになります。続いて`export.as`がある場合は変換済みtask outputを評価し、結果でworkflow context全体を置き換えます。`export.as`の省略時はcontextを維持します。nested `do`と`for`の各反復は同じcontextを共有します。`for`が反復ごとの最後のtask outputを配列へ集めるPoC限定規則は維持します。

対応するデータフロー式は文字列のstrict jq式`${ ... }`だけです。workflow `input.from`はraw workflow input、task `input.from`はraw task input、task `output.as`はraw task output、`export.as`は変換済みtask output、workflow `output.as`は最後のtaskの変換済みoutputを`.`として評価します。式で使える変数は各段階に応じた`$context`、task本体とtask output/exportでの`$input`、`export.as`だけの`$output`です。`for.each`/`for.at`の変数も維持します。未提供の`$workflow`、`$task`、`$runtime`等はjq compile時に拒否します。

v1.0.3の`dsl.md`はstrict式の`${...}`、jq言語、式失敗の扱いを定めていますが、jq filterが返す0件/1件/複数件streamの値への正規化は明記していません。このPoCでは曖昧さをPython式等で隠さず、テストで固定したローカル規則として0件=`null`、1件=その値、複数件=JSON配列を使います。

未対応構文・属性はfail-fastです。input/output/exportのschema設定、`input.from`/`output.as`/`export.as`のobject形式、`if`、`for.while`、`try`、`fork`、`listen`、`emit`、`run`、`wait`、`raise`、DSL内HTTP/MCP、任意script、top-level `use`/`schedule`等は実行しません。未対応の追加キーや型もsubset検査で拒否します。

### `mock-job` call とdurable Job Adapter

[`fixtures/mock_job_workflow.yaml`](fixtures/mock_job_workflow.yaml)はOWS v1.0.3公式schema-valid fixtureです。`call: mock-job`と`with`引数mapを使い、`set`、loop、`output.as`、`export.as`をつないでjob IDとresultを次taskへ渡します。loop iteration indexを含むtask identityは異なるrequest keyになります。

`with`は`operation: process`、JSON値の`payload`、整数`duration_ms`（0～2000）のみ許可します。キー追加、不正型、他operation、範囲外durationは拒否します。payloadやデータフローは本物のjq expressionをstrict modeで評価し、素のJSON値はそのまま渡します。Python `eval`や任意commandの実行はありません。DBOS step runnerなしでInterpreterを直接使ってcallすると、副作用前にadapter未接続エラーになります。

各Phase 4B runは`.state/phase4b-runs/run-<uuid>/`を作り、DBOS system DB `dbos.sqlite`とは別の`jobs.sqlite`にjob台帳を保存します。ledgerは`request_key = workflow_id + deterministic task identity`をuniqueにし、`BEGIN IMMEDIATE` transactionで同key・同digestの再送を既存jobへ照合します。同key・異digestはfail-closedです。状態、canonical request digest、進捗、result/error、dispatch回数、service実行回数を記録します。

固定処理だけを行うmock serviceをDBOS workerと別の子Python processとして起動します。serviceは`QUEUED` jobを1回claimし、進捗と完了receiptをledgerへ記録します。`mock_failure: true` payloadは試験用に決定的`FAILED`を返します。FAILED receiptはDBOS stepをERRORにし、自動再送しません。bounded poll timeoutではjobをcancel扱いにせず、ledger状態を残してDBOS stepをtimeout ERRORにします。ledger状態を照合できない場合はadapter-owned `NEEDS_RECONCILIATION` markerで止め、自動再送しません。このmarkerはDBOSのworkflow statusではありません。

Phase 4Bのstep内bounded pollingはPoC限定です。実Codex接続時には長時間MCP呼び出しをDBOS step内で待たせず、非同期start/status/event連携に置き換える必要があります。このPoCは実Codex、MCP、HTTPを呼びません。

### Phase 4C: 短いstepとdurable wait

`cli.py run --mock-job-durable-wait`はmanifestの`control.mock_job_durable_wait=true`を固定し、mock runを`.state/phase4c-runs/run-<uuid>/`へ隔離します。この明示modeだけがPhase 4Cのcheckpoint構成を使います。従来のPhase 4B runは既存の`call` stepとstep内bounded pollingでresumeし、新modeのstep列へ切り替わりません。

新modeの`call: mock-job`は同じ`workflow_id + task identity`をkeyに、完了を待たない短い`start_job` step、短いstatus/event取得step、workflow側の`DBOS.sleep()`を繰り返し、終端receiptを次taskへ返します。各status stepは状態snapshot、`read_events(after_id, limit)`、状態の再確認を行い、snapshotの`latest_event_id`までイベントを追いつかせます。sleep間隔は0.05～0.25秒、照会上限は1～600回、合計待機は最大30秒です。時間分岐に`time.monotonic()`は使いません。

`jobs.sqlite`には永続`job_events(event_id, job_id, state, timestamp, payload_json)`、service heartbeat/claim情報、adapter-owned `job_reconciliation`を追加します。QUEUED生成、RUNNING claim、進捗、SUCCEEDED/FAILED receiptのイベントは対応するjob更新と同じSQLite transactionに書きます。`read_events`は単調増加cursorと1～1000件の上限を使い、同じcursorからの再読込を許容します。照会の間に状態が変わってもイベントはDBに残り、cursorによるcatch-upができます。

所有serviceのheartbeatとjobの`updated_at`が指定TTLを超えて古くなったRUNNING jobは、`jobs.state`を変えずにsnapshotの`status`/`adapter_marker`へ`NEEDS_RECONCILIATION`を返します。明示的にSTOPPEDと記録されたserviceに属するRUNNING jobも同じ扱いです。service稼働中は進捗またはheartbeatを更新するため、job状態が一定時間変わらないことだけでは停止扱いしません。代替serviceはRUNNING jobを再claimしません。終端receiptがあればreceiptを優先し、手動解決UI/APIはありません。

個別照会はPoCローカルCLIから行えます。`--run-dir`は`.state/phase4b-runs`または`.state/phase4c-runs`直下の`run-*`に限定されます。

```powershell
& .\poc\ows\.venv\Scripts\python.exe .\poc\ows\cli.py run `
  --workflow .\poc\ows\fixtures\mock_job_workflow.yaml `
  --input-json '{"values":[3,5]}' `
  --mock-job-durable-wait

& .\poc\ows\.venv\Scripts\python.exe .\poc\ows\cli.py job-status `
  --run-dir <phase4c-run-directory> --job-id <job-id>
& .\poc\ows\.venv\Scripts\python.exe .\poc\ows\cli.py job-events `
  --run-dir <phase4c-run-directory> --after-id 0 --limit 100
```

Phase 4Cのworker kill/restart実験は、Phase 4Bと同じschema-valid recovery fixtureを別manifestで実行します。`phase4c_recovery_test.py`はstart step checkpoint後・service処理中に所有workerだけを既存のWindows process identity検証で終了し、service完了後に同じworkflow IDでrecoveryします。正常完了、job/request key固定、dispatch/service実行各1回、DBOS workflow 1件、4B baselineとのstatus/result一致を証跡に残します。

これは非同期job APIとdurable workflow sleepのPoCです。`DBOS.sleep()`がDBOS workflow threadを必ず解放するとは主張せず、完全なevent-driven/zero-pollingでもありません。DBOS `recv`、HTTP/SSE、MCP、実Codex、GUIは未接続です。外部処理一般に対するexactly-once保証も主張しません。

## DBOS checkpointと復旧

通常CLI runは`.state/runs/run-<uuid>/`、Phase 4B mock runは`.state/phase4b-runs/run-<uuid>/`、Phase 4C durable-wait runは`.state/phase4c-runs/run-<uuid>/`を作ります。workflow YAMLと入力の固定コピー、SHA-256、manifest、DBOS SQLite、task JSONL、worker log、最終JSON結果を置きます。mock runには`jobs.sqlite`、service/worker identity、service/worker logs、heartbeat、recovery evidenceも保存します。`.state/`はGit ignore対象です。`resume`は固定copyとmanifestのhashを照合し、既存workflow IDを必須として`DBOS.launch()`から復旧します。resume処理は`start_workflow()`を呼びません。

各named taskのDBOS stepはtask scope pathをidentityとして登録します。loop pathには0始まりのiteration indexを含めます。step checkpointにはraw task action結果に加えてtask名・identity・task kind・DSL hash、変換済みtask inputのSHA-256、runtime variables（contextとloop変数を含む）のSHA-256を保存し、replay時に現在値と照合します。不一致やdigestのない旧形式checkpointはfail-closedします。`do`親taskと`for`親taskにも既存のnamed checkpointを使い、新しいcheckpoint段階は追加しません。input/output/export変換はcheckpointの外で純粋に再計算し、context更新もworkflow replayで同じexport式を再適用します。これにより完了stepのactionは再実行せずに、変換済み出力とcontextを再構成します。復旧fixtureは既存の最終結果`{"final":2}`を保ったまま、export済みcontextから結果を作ります。復旧実験のtask JSONL、heartbeat、marker gateは実験を可視化するためのローカル制御記録で、OWS taskの外部副作用ではありません。

この段落はPhase 4Bのcheckpoint layoutを説明します。Phase 4Cでは`call` actionの単一stepを作らず、task/context checksumを保持した専用`start`/status step名とdurable sleepを使います。新modeは別manifest/run-dirに固定されるため、4Bの既存checkpoint identityと混ざりません。

start workerは`DBOS.start_workflow()`からIDを得た直後、manifestをatomic replaceで更新します。Phase 4B recovery driverはworkerとserviceを別々の`subprocess.Popen`で起動し、run-dir、argv、実PID、Popenとの親子関係、creation time、実Python image pathを照合します。Windowsのprocess handle APIで実child PIDだけを終了し、redirector stubの孤児化は成功扱いしません。serviceはworker停止をまたいで動かし続け、worker heartbeat停止とjob完了後・DBOS step checkpoint確定前の障害窓を確認してから同じworkflow IDを復旧します。回復run内ではjob ID/request keyと1行のledgerを厳密比較し、baselineとの結果は実行固有識別子を除いたstatus/resultで比較します。完了済call stepのreplayでadapter dispatchも増えないことを確認します。

`start_workflow()`がIDを返した直後からmanifestをatomic更新するまでの短いクラッシュ窓は残ります。この窓でworkerが終了するとCLIからIDを知る証跡がまだありません。これはPhase3 PoCの残存制限です。

## セットアップと実行

PowerShellでrepo rootから実行します。

```powershell
uv venv .\poc\ows\.venv --python 3.14
uv pip sync --python .\poc\ows\.venv\Scripts\python.exe .\poc\ows\requirements.txt

& .\poc\ows\.venv\Scripts\python.exe .\poc\ows\cli.py run `
  --workflow .\poc\ows\fixtures\workflow.yaml `
  --input-json '{"sign":1,"offset":2,"values":[3,5]}'
```

CLIはworkflow ID、run directory、最終result、definition/input hashをJSONで出します。途中停止後は同じrun directoryを使って復旧できます。

```powershell
& .\poc\ows\.venv\Scripts\python.exe .\poc\ows\cli.py resume --run-dir <run-directory>
& .\poc\ows\.venv\Scripts\python.exe .\poc\ows\cli.py status --run-dir <run-directory>
```

Phase 4Bの通常mock runとcrash recoveryは次のコマンドで実行できます。Recovery driverはbaseline resultをJSON fileから読み、PowerShell経由で長いJSON引数を渡しません。

```powershell
$normal = & .\poc\ows\.venv\Scripts\python.exe .\poc\ows\cli.py run `
  --workflow .\poc\ows\fixtures\mock_job_recovery_workflow.yaml `
  --input-json '{"payload":{"value":42},"duration_ms":250}' | ConvertFrom-Json

& .\poc\ows\.venv\Scripts\python.exe .\poc\ows\phase4b_recovery_test.py `
  --normal-result-path "$($normal.run_dir)\result.json"
```

Recovery evidenceは新しい`.state/phase4b-runs/run-<uuid>/phase4b-recovery-evidence.json`にあります。Pytestではnormal/recovery比較に加えFAILED receiptとbounded timeoutのDBOS状態も検証します。

Phase 4Cの回復検証はPhase 4B baselineの`result.json`を使います。

```powershell
$normal = & .\poc\ows\.venv\Scripts\python.exe .\poc\ows\cli.py run `
  --workflow .\poc\ows\fixtures\mock_job_recovery_workflow.yaml `
  --input-json '{"payload":{"value":42},"duration_ms":1800}' | ConvertFrom-Json

& .\poc\ows\.venv\Scripts\python.exe .\poc\ows\phase4c_recovery_test.py `
  --normal-result-path "$($normal.run_dir)\result.json"
```

Phase 4Cの回復証跡は`.state/phase4c-runs/run-<uuid>/phase4c-recovery-evidence.json`に保存されます。service crash後のRUNNING job照会とfail-closed markerは`tests/test_job_adapter.py`で実service processをkillして確認します。

## 検証

```powershell
& .\poc\ows\.venv\Scripts\python.exe -m pytest -q -c .\poc\ows\pytest.ini .\poc\ows\tests
& .\poc\ows\.venv\Scripts\ruff.exe check .\poc\ows
& .\poc\ows\.venv\Scripts\ruff.exe format --check .\poc\ows
& .\poc\ows\.venv\Scripts\python.exe -m compileall -q .\poc\ows
```

RecoveryのJSON証跡は各`.state/phase4b-runs/run-<uuid>/`に保存され、実Popen/worker/service PID、identity照合、heartbeat停止/継続、checkpoint barrier、workflow status、job ID/request key、ledger行数と実行回数、semantic result比較を含みます。
