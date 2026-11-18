# D1-I1 Pure Contract Core 実装証跡

**状態:** `REVIEW_PENDING`（実装・記載済み検証は完了、ChatGPTのGitHubレビュー待ち）
**基点:** `codex/workflow-d1-i0-baseline` / `ff30f856e9ec29c512acd279756963c001e4bf75`
**作業ブランチ:** `codex/workflow-d1-i1-contract-core`

## 範囲

I1の純粋な契約コンパイル、JSON正規化、Schema検証コアを追加しました。追加実装は [contracts.py](../../../src/codex_bridge/workflows/contracts.py)、単体テストは [test_workflow_d1_i1_contract_core.py](../../../tests/test_workflow_d1_i1_contract_core.py) です。実装スライスの位置付けは [D1実装計画](D1-implementation-plan.md)、仕様案は [D1詳細設計](D1-input-output-variables.md) を参照してください。

このモジュールは既存のworkflow実行系から呼び出されない独立した純粋ライブラリです。定義保存、SQLite/DBOS、HTTP/MCP、OWS interpreter/schema、run manifest、既存legacy digest/ETag、GUIには接続していません。strict runを開始可能にする変更、feature flagの有効化、外部Codex job、production DB操作も行っていません。

## 追加した契約

- `canonical_json` と `sha256_json` は、キー順固定・UTF-8・コンパクト形式・非有限数拒否でJSON値を決定的に符号化します。`parse_strict_json` は重複キー、NaN/Infinity、不正UTF-8等を拒否します。
- `compile_contract` はstrict v1の厳密なトップレベル構造と変数宣言を検証し、Draft 2020-12 Schemaの許可キーワード、objectの `additionalProperties` 明記、ローカルかつ非循環な `$defs` 参照、初期値/defaultの適合性を確認します。秘密変数は受け付けません。
- `normalize_input` は入力コピー上で欠落値に限りdefaultを再帰適用し、明示nullを保った後に入力Schemaを検証します。配列要素は生成せず、既存のobject要素を正規化します。root defaultおよび出力defaultの補完はしません。
- `validate_output` はdefaultを挿入せず出力Schemaを検証します。契約・入力・出力・資源上限の例外は、入力値を含めない安定した機械可読codeを持ちます。
- Proposed上限としてcontract 64 KiB、normalized input 96 KiB、output/variable initial 64 KiB、declarations 256、Schema depth 24 / nodes 512 / properties 512 / ref depth 16、JSON value depth 32 / object properties 1024 / array length 10000を適用します。

返却されるcontract構造はコピーとして扱い、呼び出し元の変更でcompiled stateが変化しないようにしています。JSON Schemaの追加keyword、外部参照、循環参照は明示的に拒否します。
`CompiledContract`のreprではcanonical contract本文を非表示にし、digestは表示します。テストでinput defaultとvariable initialのsentinel値がreprへ漏れず、canonical本文プロパティには保持されることを確認しています。

## 検証結果

環境はWindowsネイティブ、プロジェクトのPython 3.14環境と `uv run` です。

| 検証 | 結果 |
| --- | --- |
| `uv run pytest -q tests/test_workflow_d1_i1_contract_core.py` | PASS — 39 passed、0 failed |
| `uv run pytest -q -ra tests/test_workflow_d1_i0_baseline.py` | PASS — 8 passed、0 failed |
| `uv run pytest -q -ra tests/test_workflow_definitions.py tests/test_workflow_definition_api.py` | PASS — 31 passed、2 skipped、0 failed（73.90秒） |
| `uv run ruff check src/codex_bridge/workflows/contracts.py tests/test_workflow_d1_i1_contract_core.py` | PASS |
| `uv run ruff format --check src/codex_bridge/workflows/contracts.py tests/test_workflow_d1_i1_contract_core.py` | PASS — 2 files already formatted |
| `uv run mypy src/codex_bridge` | PASS — 133 source files、issuesなし |
| `uv run python -m compileall -q src/codex_bridge tests/test_workflow_d1_i1_contract_core.py` | PASS |
| `uv run pytest -q`（製品全体） | NOT_RUN — I1では全体suiteを実行していません |

Definitions/APIの2件のskipは、Windows環境でsymlinkを作成する特権がなく `WinError 1314` となった既存テストです。skipを成功扱いにはしていません。I0の証跡は [I0基準証跡](D1-I0-baseline-evidence.md) に別途記録されています。

## 状態と後続作業

I0は `ACCEPTED (with documented caveats)` のままです。I1は記載済みの検証を通過し、レビュー待ちの `REVIEW_PENDING` です。D1全体は最終承認前のDRAFTであり、I2〜I8はすべて `NOT_STARTED` です。

定義DBへのcontract保存とmigrationはI3、変数永続化とmutation ledgerはI4、run binding/manifest/reader対応はI5、公開HTTP/MCP契約はI6、OWS runtime adapterやstrict実行接続はI7、隔離Windows受入はI8の対象です。これらは本作業で実装・検証していません。全体pytest、production DB migration、API/MCP実行、GUI、OWS変更、Codex外部jobも未実施です。
