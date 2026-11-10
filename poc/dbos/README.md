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

検証時は2回のworkflow実行後、`workflow_status` 2行と `operation_outputs` 6行（各workflowの3ステップ分）を確認しました。`.venv/` と `.state/` はこのディレクトリの `.gitignore` で除外しています。強制停止後の再起動・復旧試験はこのPoCの検証範囲に含みません。
