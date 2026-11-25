# D1-I7-B strict variable adapter and direct Interpreter evidence

- **実装状態:** IMPLEMENTED / PENDING_CHATGPT_REVIEW。
- **対象branch:** `codex/workflow-d1-i5-b4-verified-readers`。
- **base:** I7-A commit `8dc2399dcd443de13f6d60119a08ef343ba01d09` (ChatGPT ACCEPTED with documented caveats)。
- **範囲:** 予約済みstrict v2の検証済みpinned contract/sourceからI4 variable ledgerへの内部adapter、root variable projection、strict direct Interpreter integration。
- **全体境界:** I7はIN_PROGRESS、I2はDEFERRED / NON-BLOCKING、D1はDRAFT、strict run admissionはOFFです。これはD1またはADR 0007全体の承認ではありません。

## 先行review記録

ChatGPT GitHub review of `8dc2399dcd443de13f6d60119a08ef343ba01d09` はI7-AをACCEPTED (with documented caveats)としました。review時は947 passed / 11 skipped、専用suite 29 passedです。static validator/definition-reader接続を受け入れ、runtime semantic mismatchesをI7-Bへ繰り越しました。full pytest、CPP/account-auth、live API、実Worker/DBOS/job、WAL metadataは未検証です。I6-C/I6もaccepted with caveatsで、419 passed / 6 skipped、専用11 passedです。corrupt request-source ledgerがあるreplayでID保持前にunavailableとなり得る点はI7-C admission integration時に再確認します。

## 実装

`StrictWorkflowVariableRuntimeAdapter.from_reserved_run`はI5のregular manifest readerと`verify_strict_reserved_readonly`を使い、予約済みv2、allowed roots、manifest identity、pinned strict revisionを再検証してからI4 `StrictWorkflowVariableMutationStore`へ接続します。未予約・legacy・不正snapshot・roots不一致は値非露出エラーに収束します。adapterはstrict contractとverified pinned definitionをInterpreterへ渡し、別definitionを実行しません。worker、manager、service start、公開run admissionには接続していません。

I4 storeにroot projection readerと実行contextから既存execution keyを得る小さなmethodを追加しました。root projectionはroot ACTIVE frameに対する一つのread transactionでdatabase/binding/frame/history全体を監査し、present値だけをdeclaration_id keyで返します。JSON nullはpresentのまま保持し、missing/tombstoneは除外します。loop実行のiteration vectorはroot frame identityに含め、non-root frameでは既存のpersisted vector一致条件を維持します。

I4 candidateの`initialize`は内部optional `value`を受け、明示値が宣言initialより優先します。引数なしの場合は旧挙動（initialを使う、またはno-op）を維持し、既存state/tombstoneからinitializeで復活できません。OWSのpublic strict callはI7-Aどおりvalue必須です。`object_set` / `object_delete`のpublic `path`はI4 `pointer`へ変換し、空pointerをvalidatorとadapterの両方で拒否します。literal initialize/assign値の静的照合はI4 candidate validationを使い、分離したsub-schema validatorによる`$ref`解決差を持ち込みません。I4 ledger schema/digest形式は変更していません。

strict InterpreterだけがI5から得たcompiled contractとadapterを使います。OWS `with`値は既存`_evaluate_set_value`/jq semanticsで評価し、identityはInterpreterのnode JSON Pointer、実際のnested iteration vector、ordinal 0、root frame固定、subflowなしです。prepare時点のCASは新規keyならcurrent stateから、既存keyならhistorical mutation recordから取得します。immutable prepared payloadはI4 applyへ渡し、same-key replayはhistorical resultを返しlatest stateを巻き戻しません。CallFunctionはpresent値なら`{present: true, value: ...}`、deletionなら`{present: false}`を返し、contextを暗黙同期しません。

strict `$cbvars.workflow` projectionはI4 storeのdeclaration_id keyを使います。D1で許可する参照はdirect static `$cbvars.workflow["declaration_id"]`と`.identifier` shorthandで、その後のnested JSON accessを許します。static/runtimeで同じrewriteを適用し、`has()`でmissing/tombstoneをconstant textのjq errorにし、present nullは通します。bare/dynamic/unknown referenceはstrict static validationで拒否します。scannerはquoted text、jq interpolation、`#` commentの境界を尊重し、文字列やcomment本文を書き換えません。legacy jq pathにはrewriteもcbvars injectionもありません。

task input、operation、output、export、workflow outputの各式評価phaseで必要な場合に限り、一つのstable phase identityに対するroot projectionを一度取得して、そのphase内の式評価へ渡します。root workflow inputには`$cbvars`を供給しません。通常taskは従来の`compute_task_action`/`StepRunner`経路を維持します。nested `for` iteration vector、`do`、`switch`の挙動は直接Interpreter testsで確認しました。

## 検証

| 検査 | 結果 |
| --- | --- |
| I7-B adapter/runtime専用suite | 15 passed |
| 予約済みmanifest reader変更後のadapter factory / 全operation / allowed-root確認 | 2 passed |
| I4-A/B/C、I5-A/B1/B2/B3/B4A/B4B/B4C、I6-A/B/C、I7-A/B、graph、saved execution、task projection、service/manager/codex-job focused aggregate | 580 passed / 8 skipped / 1 existing-scope failure |
| 上記aggregate内の1 failure | `test_legacy_request_database_reads_without_creating_source_table`。legacy request-source tableを削除したreplayでI6-C `_strict_request_candidate`がmalformed v1 schemaをstrict candidateとみなしconflictする。service.pyは本sliceで変更しておらず、これは既知のI6-C corrupt request-source ledger caveat。I7-C admission integration時に再確認する |
| Ruff check / format check（変更Python paths） | PASS |
| mypy `src/codex_bridge` | PASS — 155 source files |
| `python -m compileall -q src/codex_bridge` | PASS |
| `git diff --check` | PASS |
| Full pytest / CPP / account-auth / live API / real Worker / DBOS / external job / WAL metadata | NOT_RUN |

aggregateの8 skipはWindows symlink privilegeがないhostでの隔離caseです。全suiteはWindows native `tmp_path`とfake/test clientを使用しました。production/shared DB、public strict start、live Bridge/worker、実Codex jobは使用していません。aggregateの既知legacy failureはI6-C review caveatとして分離記録し、service側への変更はこのI7-B範囲に加えていません。

## 残件と境界

- I7-CではDBOS recovery向けに各evaluation phaseのprojectionとprepared mutation argumentsをdurable checkpointへ保存し、recovery時にfuture variable stateからjq payloadやcontrol flowを再計算しない必要があります。I7-Bの直接fresh Interpreter rerunは、既にmutated DBを使ったrecoveryの証明ではありません。
- I7-Cではworker/runtime adapter、output schema validation、safe reason mapping、isolated admissionを接続します。manager/worker/worker_entryの現在のstrict runtime拒否は維持されています。
- I6-C caveatのcorrupt/missing request-source ledger replayはI7-Cで境界を再確認します。
- strict run admissionはOFFです。DB migration、public API/MCP契約変更、external Codex実行、output success判定、I8 acceptanceは未実施です。
- I7-BはChatGPT review待ちです。D1はDRAFT、ADR 0007全体はProposedのままです。

## 変更path

- `src/codex_bridge/workflows/interpreter.py`
- `src/codex_bridge/workflows/strict_variable_logic.py`
- `src/codex_bridge/workflows/strict_variable_mutations.py`
- `src/codex_bridge/workflows/strict_variable_runtime.py`
- `tests/test_workflow_d1_i4b_variable_logic.py`
- `tests/test_workflow_d1_i4c_mutations.py`
- `tests/test_workflow_d1_i7a_variable_call_validation.py`
- `tests/test_workflow_d1_i7b_variable_runtime.py`
- `docs/workflow-design/phases/D1-I7A-variable-call-validation-evidence.md`
- `docs/workflow-design/phases/D1-implementation-plan.md`
- `docs/workflow-design/phases/D1-I7B-variable-runtime-evidence.md`
