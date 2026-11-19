# D1-I4-C mutation ledger evidence

- **実装状態:** IMPLEMENTED / PENDING_CHATGPT_REVIEW
- **実装commit:** この証跡を含む単一の実装commit（full SHAは完了報告のGitHub review対象HEADを参照）
- **対象branch:** `codex/workflow-d1-i4-variable-store`
- **設計境界:** D1全体はDRAFT、strict run admissionはOFF。I4-A/I4-BはACCEPTED (with documented caveats)、I4全体はIN_PROGRESS / PENDING_I4C_REVIEW。

## 実装範囲

新規 [strict_variable_mutations.py](../../../src/codex_bridge/workflows/strict_variable_mutations.py) にopt-in内部APIを追加しました。

- `StrictWorkflowVariableMutationStore(run_directory, expected_run_binding, compiled_contract)` は、初期化済みのrun-local `variables.sqlite` にのみ接続します。DBの作成やmigrationはしません。
- `MutationContext` はstable `node_path`、完全なloop `iteration_vector`、`frame_id`、`mutation_ordinal`、optional `subflow_call_context`を持ち、reprは値を隠します。
- `apply(...)` は決定的なdomain-separated `execution_key` と、I4-B payload validatorを通して固定したoperation argumentsから`payload_digest`を内部生成します。callerからdigest値は受け取りません。
- `read_current(...)`、`inspect_ledger()`、`get_mutation(...)` はread-onlyです。`enter_frame(...)` / `close_frame(...)` はI4-Bのscope/identity/frame検証helperを再利用し、全mutation/state chain監査後にframe lifecycleを継続します。
- I4-AのDDLとI4-Bのpublic FrameStore API/fail-closed動作は変更していません。DBOS、manifest/reader/API、interpreter/OWSには接続していません。

各mutationは`BEGIN IMMEDIATE`、WAL、`synchronous=FULL`、foreign keys、有界busy timeoutの下でRunBinding/schema/integrity/frame/current-chainを検証します。CAS後にI4-B候補を計算し、`variable_states`更新と`variable_mutations`挿入を同一transactionに含め、commit前にschema、SQLite integrity、foreign keys、対象chain/latest stateを再検証します。`after_state_write`、`after_ledger_write`、`before_commit`のfault hookでcommit前の例外が両方のrowをrollbackすることを確認しました。

同一execution keyのreplayは保存済みbefore stateから候補とpayload digestを再計算し、ledger上の歴史的after値を返します。最新stateが進んでいても更新せず、same-key異payload/operation/declarationは`CONFLICT/NEEDS_RECONCILIATION`で拒否します。このエラーはDBのrun statusを変更しません。version 0のmissing、JSON null、versionを持つtombstoneを区別し、tombstone後のassignも可能です。initialのない`initialize`はno-op candidateを保存せず拒否します。

## 台帳検証と限界

- 新規mutationと`read_current`は対象変数chainを局所検証し、全frame、RunBinding、schema/user_version、`integrity_check`、`foreign_key_check`、run内ledger件数を検証します。`inspect_ledger()`とframe enter/closeはrun全体の全chain/stateを検査します。大きいrunでの局所readを全ledger audit済みとは表示しません。
- chain監査は0→1からの連続version、before/after presenceとdigest連鎖、最終state一致、canonical JSON、I4-B Schema/digestを検証します。最大ledger 10,000 rows、現行state値合計2 MiBを内部budgetとして採用しました。1 operationあたり64 KiBはI4-B/I1 helperの制約です。frame数上限1024はI4-Bの既存値を再利用します。これらはこの内部sliceのresource budgetであり、追加のOS ACL/MUTEX/SID/file-ID gateではありません。
- DDLに元request本文とnode/iteration/subflow contextは保存されません。そのため保存payload digestやexecution keyの値だけから、元requestやcontextを独立に再計算できません。replay時は入力requestからkey/digestを再構築して保存値と照合し、DB上のvalue/version chainは独立検証します。形式上有効なpayload digestの単独改変はfull auditだけでは検出できず、元request replay時の照合で拒否されます。
- expected bindingとcompiled contract hashおよびDB内RunBindingを照合しますが、呼出し元が渡すbinding値の独立した真正性やI3 cross-DB/manifest bindingは証明しません。SQLite commitとDBOS checkpointは別transactionです。DBOS実行・実checkpoint・外部jobを発行せず、commit後/checkpoint前はrestart後のfake checkpoint replayで確認しました。

## 検証結果

すべてのI4-C test DBはpytest `tmp_path`内に作成し、既存service、Workflow POST、Codex job、production/shared DB/definitions.sqlite/requests.sqlite/DBOSを操作していません。

| 検証 | 結果 |
| --- | --- |
| 新I4-C mutation/fault/replay/frame/concurrency/resource-limit tests（CLOSED read guard修正後の今回run） | PASS — 23 passed、FAIL 0。11.14秒 |
| I4-A/I4-B/I1/I3-C focused regression（今回のread guard修正run） | PASS — 154 passed、FAIL 0。SKIP — 2（I4-AのWindows symlink作成権限）。47.04秒 |
| I4-C + I4-A/I4-B + I3-A/B/C + I1/I0 + focused legacy regression（対象16 test files） | PASS — 343 passed、FAIL 0。SKIP — 4（Windows symlink作成権限: I4-A 2、legacy definitions 2）。271.78秒。この結果は前commit `f8a904c4caad458bfb0d1058ab86c488178d904f` の過去runであり、今回の修正検証件数には合算していません |
| 2つのthreadによるsame-key replay / different-key stale CAS | PASS |
| `after_state_write` / `after_ledger_write` / `before_commit` rollback、再起動後historical replay | PASS |
| ledger chain/state corruption、binding/schema/user_version、read-only missing DB、SQLite integrity/FK、value-free repr/error | PASS |
| ledger 10,000 / state total 2 MiB / frames 1,024 limits | PASS — testではmonkeypatchした小さい境界値で上限直前・上限到達・超過を検証 |
| `uv run --no-sync mypy src/codex_bridge` | PASS — 141 source files |
| `uv run --no-sync python -m compileall -q src/codex_bridge` | PASS |
| `uv run --no-sync ruff check src/codex_bridge tests/test_workflow_d1_i4c_mutations.py` | PASS |
| `uv run --no-sync ruff format --check` on touched Python files | PASS |
| `git diff --check` | PASS |
| Full pytest suite | NOT_RUN |
| 今回のI3-A/I3-B、D1-I0、focused legacy suites | NOT_RUN — CLOSED read guardに直接関係するI4-A/B、I1、I3-Cだけを実行 |
| DBOS real checkpoint / strict runtime / manifest-reader-API integration / production migration / external Codex job | NOT_RUN — I5以降または別境界 |

## ChatGPTレビュー修正 — CLOSED frame read guard (2026-11-19)

ChatGPT review対象commit `f8a904c4caad458bfb0d1058ab86c488178d904f` で、`read_current(frame_id, declaration_id)` がframeの存在だけを確認し、CLOSED frame上の既存stateとmissing stateを返す欠陥が指摘されました。設計 [D1 §11.3](D1-input-output-variables.md) に従い、対象frameを特定した直後に`status != ACTIVE`を確認し、既存のvalue-free `StrictWorkflowVariableConflict` で拒否します。declaration解決/history read前の局所guardであり、frame状態やread-only監査経路は変更していません。

修正後のI4-C testはactive rootでのmissing/current read、CLOSED child上の既存stateと同scope未初期化宣言のread拒否を確認します。同じCLOSED childで`get_mutation`と`inspect_ledger()`が履歴を検証でき、既存execution keyの`apply()` replayはversion 1の保存結果を返し、version 2のlatest stateを保持することも確認します。CLOSED childへの新規mutationは拒否され、別のACTIVE childとrootのreadは成功します。I4-CはIMPLEMENTED / PENDING_CHATGPT_REVIEWのままで、ChatGPTの再レビュー前に受入済みとはしません。

既存I4-B FrameStoreはvariable state/ledger非空時のenter/close/inspect拒否を維持し、新I4-C ledger-aware frame APIだけが全chain監査を通じてlifecycleを継続します。I4-CはChatGPT review待ちです。I5 manifest/reader、I6 API、I7 interpreter/OWS/DBOS wire、I8 acceptanceは未着手です。I2 Windows guardはDEFERRED / NON-BLOCKINGのままです。D1全体はDRAFT、strict run admissionはOFFです。
