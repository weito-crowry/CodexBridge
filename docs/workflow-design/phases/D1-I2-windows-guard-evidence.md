# D1-I2 Windows Guard 実装証跡

**状態:** `IN_PROGRESS / BLOCKED_FULL_ACCEPTANCE`（I2-Aの実装と記載済み隔離検証は完了、I2全体の受入gate未達）
**基点:** `codex/workflow-d1-i1-contract-core` / `dc3b68687a83b189f3eb98fed5a1db0fee929cf7`
**作業ブランチ:** `codex/workflow-d1-i2-windows-guard`

## I2-Aで実装した範囲

[windows_guard.py](../../../src/codex_bridge/workflows/windows_guard.py) に独立したGlobal named Mutex primitive、[test_workflow_d1_i2_windows_guard.py](../../../tests/test_workflow_d1_i2_windows_guard.py) に専用テストを追加しました。現行controller、manager、service、worker、API/MCP、SQLite/DBOSには接続していません。

- 現行process tokenの`TokenUser`からSIDを取得し、絶対Windows state rootを正規化して、rootとSIDをlength-prefixed UTF-8 framingしたSHA-256から`Global\CodexBridge.Workflow.Writer.v1.<hash>`名を作ります。相対・曖昧・device pathは拒否します。
- `CreateMutexExW`にowner SIDと`LOCAL SYSTEM`だけを含むprotected DACLを明示し、`dwFlags=0`で作成します。`GetSecurityInfo(SE_KERNEL_OBJECT)`からowner・DACLを読み戻し、protected DACL、2つの許可ACE、SID、mask、ACE flagを照合します。Local namespaceへのfallbackはありません。
- 待機は有限timeoutです。`WAIT_ABANDONED`は`ABANDONED`として返し、`is_ready`はfalse、`needs_reconciliation`はtrueになります。I2にはreconciliation serviceがないため`mark_reconciled()`は拒否します。`with`の`__enter__()`ではabandoned状態をcleanup後に`mutex_reconciliation_required`で拒否し、本体へ入れません。一方、同じowner threadからの`release()/close()`は、実行許可を与えずに`ReleaseMutex`と`CloseHandle`を行えるため、異常終了経路でもhandle cleanupが可能です。
- Mutex handleのreleaseは取得したWin32 thread IDに限定されます。作成status・ACL readback・waitの異常は安全な固定reason codeでfail closedし、エラーにstate path/SIDを含めません。`LocalFree`はNULL/0成功、非NULL失敗として扱い、実Windows APIを通すテストで確認しました。

Mutex名の算出はpure helperとして実装しています。Mutex取得自体は既存state rootのACL/reparse検査やcontroller起動を行いません。これらはI2-Bで実装するまで本番strict startには使用できません。

## 環境と検証結果

Windowsネイティブ環境はOS version `10.0.22631.0`、Python `3.14.10`です。pytestの一時ディレクトリがあるC: volumeは`NTFS`と読み取れました。実Mutexテストでは現在のprocess SID、実Global Mutex、実owner/DACL readback、同一process内の別thread競合を検証しました。SIDや実state pathは証跡に出していません。ファイルACLはこのsliceで検査していません。

| 検証 | 結果 |
| --- | --- |
| `uv run pytest -q -ra tests/test_workflow_d1_i2_windows_guard.py` | PASS — 12 passed、1 skipped、0 failed |
| `uv run pytest -q -ra tests/test_workflow_d1_i1_contract_core.py` | PASS — 39 passed、0 failed |
| `uv run pytest -q -ra tests/test_workflow_d1_i0_baseline.py` | PASS — 8 passed、0 failed |
| `uv run ruff check src/codex_bridge/workflows/windows_guard.py tests/test_workflow_d1_i2_windows_guard.py` | PASS |
| `uv run ruff format --check src/codex_bridge/workflows/windows_guard.py tests/test_workflow_d1_i2_windows_guard.py` | PASS |
| `uv run mypy src/codex_bridge` | PASS — 134 source files、issuesなし |
| `uv run python -m compileall -q src/codex_bridge tests/test_workflow_d1_i2_windows_guard.py` | PASS |
| 製品全体 `uv run pytest -q` | NOT_RUN |

I2テストの1 skipは、Windowsで実行したため非Windows fail-closed専用テストをskipしたものです。fake Win32 APIで`WAIT_ABANDONED`、異常CreateMutex status、DACL readback拒否を検証していますが、実プロセス終了によるnative abandoned mutexの再現はしていません。I0証跡にあるsymlink権限由来の既存2 skip（WinError 1314）も維持し、本sliceでは再実行していません。

## 未達gateと次のslice

以下は未実装または未検証であり、I2全体を受入済みとはしていません。

- **I2-B未実装:** 新規state/run directoryとfileのNTFS DACL作成/readback、既存rootのACL検査、reparse point/ancestor拒否、handle-based file identityとTOCTOU確認、exclusive file作成、WAL/SHM inherited ACE検査。
- **別session検証未実施:** RDP、fast user switching、service session間で同一root/SIDのGlobal mutex競合を検証していません。
- **quiescence gate未実装:** 旧Bridge/autostart/unknown writerやleftover worker不存在の証明を行うread-only operator gateはありません。旧binaryをMutexが止めるとは扱いません。
- **abandoned native recovery未検証:** 実process crashによる`WAIT_ABANDONED`後のrequest/manifest/worker reconciliationはI3/I5まで存在せず、状態は実行readyになりません。
- **SQLite sidecar未検証:** SQLiteを起動せず、WAL/SHMのACL継承を実測していません。

本作業では既存service/controller、共有state root、既存ACL、production DBを変更していません。ユーザープロセスやBridge serviceを停止していません。I0/I1はACCEPTED (with documented caveats)、I2は`IN_PROGRESS / BLOCKED_FULL_ACCEPTANCE`、I3〜I8はNOT_STARTED、D1全体はDRAFTです。I2-Bの隔離実装・テストおよび別session/quiescenceの運用受入を完了するまでstrict runtime接続やenableは行いません。
