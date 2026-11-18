# D1-I3-C Cross-DB Binding Evidence

- **状態:** IMPLEMENTED / PENDING_CHATGPT_REVIEW
- **branch:** `codex/workflow-d1-i3-contract-storage`
- **実装範囲:** 隔離された `definitions.sqlite` と `requests.sqlite` の既存opt-in storeを使う読み取り専用の横断照合APIと、pinned strict contractに対する入力正規化・snapshot照合。
- **全体状態:** I3はIN_PROGRESSです。I3-A/I3-BはACCEPTED (with documented caveats)、I3-CはChatGPT review待ちです。I2はDEFERRED / NON-BLOCKING、I4〜I8は未着手、D1はDRAFT、strict runはOFFです。

## 実装

新規の[strict_binding.py](../../../src/codex_bridge/workflows/strict_binding.py)は、`StrictWorkflowBindingVerifier`、`StrictWorkflowBindingMetadata`、`StrictWorkflowInputNormalization`を提供します。

- `verify(run_id, ...)` はrequests storeの`read_binding`から予約済みのdefinition ID/revisionを読み、definitions storeの`get(id, exact_revision)`でそのimmutable revisionを読みます。definition ID/revision、source/contract/binding SHA、contract version 1、run format version 2、strict contractの存在を照合します。latest revisionへの置換はしません。
- 各DBを照合した後に両storeを再読し、観測中に変わった値を拒否します。複数DB間のatomicityや、悪意ある同時書き込みの完全な防止は提供しません。
- `normalize_for_definition(id, revision, raw_input)` はI1 `normalize_input`を一度呼び、入力内容を含まないreprの結果dataclassに独立した正規化済みdictとcanonical SHA-256を返します。
- `verify_normalized_input(run_id, snapshot)` は明示提供された正規化後snapshotだけを検証します。default適用でcanonical JSONが変わるsnapshotは拒否し、予約済みnormalized-input SHAと照合します。成功結果はsnapshot照合済みを記録します。
- storeやI1からの例外は安定したvalue-free errorへ変換します。metadata、入力結果、元のstore/dataclassはいずれもreprで入力値、prompt、contract本文を表示しません。

このAPIは`request_digest`を再計算しません。callerの`expected_workflow_id`と`expected_request_digest`は形式検証後の比較値であり、DB値の独立した真正性証明ではありません。snapshotを渡さない`verify`はrequest内のnormalized-input hashとDB bindingのみを確認し、input内容の真正性を検証済みとは記録しません。manifest v2、manifestの真正性、snapshotの出所、DBOS checkpoint、run directory、実行開始は対象外です。

モジュールはworkflow runner、環境、singletonに接続していません。I3-Cはstoreの公開read/get APIだけを使い、SQL、migration、reservation、manifest、variables DB、HTTP/MCP、worker、Codex POSTを実行しません。テストDBはすべてpytest `tmp_path`内に作成しました。

## テストと検証

横断テストは同じ隔離state rootにrequests v1を先に作り、definitions v2を後から初期化する実DB構成です。strict definition作成、strict reservation、verify、default normalization、snapshot照合、latest更新後のpinned revision、replay、missing/legacy、未知revision、cross-DB ID/revision/hash不一致、request/contract row改変、期待値不一致、cycle/NaN/non-JSON入力、redaction、DB本体とrowの読み取り不変を確認しました。

| 検証 | 結果 |
|---|---|
| I3-C専用テスト `tests/test_workflow_d1_i3c_binding.py` | PASS: 26 passed |
| I3-C + I3-A + I3-B + I1 + I0 + definition repository回帰 | PASS: 165 passed, 2 skipped（193.59秒） |
| symlink test 2件 | SKIP: Windows WinError 1314でsymlink作成不可 |
| `uv run --no-sync ruff check src/codex_bridge tests/test_workflow_d1_i3c_binding.py` | PASS |
| 新規Python 2ファイルの`ruff format --check` | PASS |
| `uv run --no-sync mypy src/codex_bridge` | PASS: 137 source files |
| `uv run --no-sync python -m compileall -q src/codex_bridge` | PASS |
| `git diff --check` | PASS |
| full `pytest` | NOT_RUN |

read-only不変性テストは両SQLite本体のbytes/mtimeとrequests/definitionsの関連rowを前後比較します。既存WAL read APIを繰り返すと`requests.sqlite-shm`の接続調整情報が変わることを確認しました。sidecarの不変性は保証対象に含めず、主DB本体とrowは不変です。既存storeの変更は禁止範囲のため、I3-CがこのSQLite側の調整を制御しません。初回のファイル不変性テストでsidecar変化を確認した後、主DB本体と全関連rowを比較するテストで再実行しPASSを確認しました。

## 残る制約と次のslice

- Cross-DB照合は複数DB transactionではありません。二重読取は観測中の変化を検出する範囲の対応です。
- snapshotを明示しないverifyでは、input内容とmanifestの真正性は未検証です。
- request digestのcanonical payloadは未確定のため再計算・完全性判定をしていません。
- I3-CがChatGPT reviewでacceptedとなるまでI3全体はIN_PROGRESSです。次候補はI4 variable storeです。I5 manifest/readerはI4後の将来sliceで、未実装です。I3-Cのみではstrict runを開始できません。
- production/shared DB、実Bridge/Worker、実Codex job、production反映は未実施です。
