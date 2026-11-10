# CodexBridge OWS v1.0.3 + DBOS PoC

Windows上で、OWS DSL v1.0.3の限定subsetをPython Interpreterで実行し、DBOS 3.4.0のworkflow/step checkpointとSQLite復旧を確認するPoCです。完全なOWS runtime互換性は主張しません。`poc/dbos`のPhase 0/1/2、本体、ルート依存には変更を加えません。

## 公式仕様と検証境界

参照した仕様はv1.0.3 tagに固定しています。`main`のv1.0.4以降の構文は使いません。

- [v1.0.3 dsl.md](https://raw.githubusercontent.com/open-workflow-specification/specification/v1.0.3/dsl.md)
- [v1.0.3 dsl-reference.md](https://raw.githubusercontent.com/open-workflow-specification/specification/v1.0.3/dsl-reference.md)
- [v1.0.3 schema/workflow.yaml](https://raw.githubusercontent.com/open-workflow-specification/specification/v1.0.3/schema/workflow.yaml)

公式schemaは上記tagから取得して[`schema/workflow.yaml`](schema/workflow.yaml)に固定しました。取得元URLは`https://raw.githubusercontent.com/open-workflow-specification/specification/v1.0.3/schema/workflow.yaml`、SHA-256は`df3326da907e17e743e9d9e888839481d1557abdf91c592e74f27506ff523f0b`です。`yaml.safe_load`で読み、`jsonschema.Draft202012Validator`で公式schemaを検査します。workflow fixtureと復旧fixtureはこの公式schemaを通ります。

検査は二段階です。まずPoC subsetの静的検査で未対応task/属性/flowを拒否し、続いて公式schemaで構造を検査します。公式schema合格は、このPoCが全OWS意味論を実装したことを意味しません。例えば公式schemaに適合する`call`や`try`も、subset検査で実行前に拒否します。

## Interpreter subset

`document.dsl`は文字列`'1.0.3'`に限定します。`evaluate.language=jq`と`evaluate.mode=strict`を明示し、`${ ... }`だけをjq式として本物の`jq` packageに渡します。通常文字列は評価しません。`for.each`と`for.at`の値は`jq.compile(..., args=...)`で`$item`、`$index`等へJSON値として束縛します。

対応するtaskは`do`、`set`、`switch`、`for`です。

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

未対応構文・属性はfail-fastです。input/output/exportのschema設定、`input.from`/`output.as`/`export.as`のobject形式、`if`、`for.while`、`try`、`fork`、`call`、`listen`、`emit`、`run`、`wait`、`raise`、DSL内HTTP/MCP、任意script、top-level `use`/`schedule`等は実行しません。未対応の追加キーや型もsubset検査で拒否します。

## DBOS checkpointと復旧

CLI実行ごとに`.state/runs/run-<uuid>/`を作り、workflow YAMLと入力の固定コピー、SHA-256、manifest、DBOS SQLite、task JSONL、worker log、最終JSON結果を置きます。`.state/`はGit ignore対象です。`resume`は固定copyとmanifestのhashを照合し、既存workflow IDを必須として`DBOS.launch()`から復旧します。resume処理は`start_workflow()`を呼びません。

各named taskのDBOS stepはtask scope pathをidentityとして登録します。loop pathには0始まりのiteration indexを含めます。step checkpointにはraw task action結果に加えてtask名・identity・task kind・DSL hash、変換済みtask inputのSHA-256、runtime variables（contextとloop変数を含む）のSHA-256を保存し、replay時に現在値と照合します。不一致やdigestのない旧形式checkpointはfail-closedします。`do`親taskと`for`親taskにも既存のnamed checkpointを使い、新しいcheckpoint段階は追加しません。input/output/export変換はcheckpointの外で純粋に再計算し、context更新もworkflow replayで同じexport式を再適用します。これにより完了stepのactionは再実行せずに、変換済み出力とcontextを再構成します。復旧fixtureは既存の最終結果`{"final":2}`を保ったまま、export済みcontextから結果を作ります。復旧実験のtask JSONL、heartbeat、marker gateは実験を可視化するためのローカル制御記録で、OWS taskの外部副作用ではありません。

start workerは`DBOS.start_workflow()`からIDを得た直後、manifestをatomic replaceで更新します。実験はstart workerを自分の`subprocess.Popen`で起動し、2つ目のtaskがgateで待機中にそのPopenだけをkill・waitします。heartbeatはworker内`os.getpid()`を記録し、kill前の更新、kill後0.5秒超の静止、SQLiteの`PENDING`/完了step数を確認した後にmarkerを作ります。その後`cli.py resume`を実行し、同じworkflow IDが`SUCCESS`となることを確認します。

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

## 検証

```powershell
& .\poc\ows\.venv\Scripts\python.exe -m pytest -q -c .\poc\ows\pytest.ini .\poc\ows\tests
& .\poc\ows\.venv\Scripts\ruff.exe check .\poc\ows
& .\poc\ows\.venv\Scripts\ruff.exe format --check .\poc\ows
& .\poc\ows\.venv\Scripts\python.exe -m compileall -q .\poc\ows
```

RecoveryのJSON証跡は`.state/test-recovery/`およびdriver指定のstate directoryに保存され、完了stepの実処理開始回数、Popen PID/worker PID、heartbeat静止区間、workflow status、operation checkpoint、最終resultを含みます。
