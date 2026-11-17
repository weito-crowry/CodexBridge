# Phase C5 Controller visible review harness

Issue #182 adds a local synthetic review path through the real
`WorkflowManagementController`, `WorkflowManagementDialog`, and execution graph
widget. The controller receives only `FakeWindow` and `FakeWorkflowClient`.
The fake client serves 50 in-memory workflow rows, one detail/result, a five-node
graph, and cursor-paged events. Its POST method raises immediately if called.

The controller's existing 3,000 ms timer is left intact. Initial polls return a
stable graph snapshot and event history. A later poll appends one synthetic
event and changes the snapshot version; subsequent polls keep that state stable.
`New run` remains disabled after every controller button update, and a direct
`start_requested` signal is denied by the dedicated controller subclass.

The harness does not construct the product MainWindow or a real workflow client.
It does not connect to Bridge, HTTP, MCP, DBOS, Worker, Codex, persistent state,
or previous acceptance artifacts. The Japanese locale is configured in process
memory through a settings adapter; the harness does not write QSettings or OS
language settings.

## Commands

Run the static, nonvisual preparation check:

```powershell
\.venv\Scripts\python.exe scripts\phase_c5_controller_visible_review.py --prepare
```

After implementation review, a user can launch the regular or compact visible
dialog in a native Windows PowerShell session:

```powershell
\.venv\Scripts\python.exe scripts\phase_c5_controller_visible_review.py --show --size=regular
\.venv\Scripts\python.exe scripts\phase_c5_controller_visible_review.py --show --size=compact
```

`--show` refuses non-native Windows sessions and `QT_QPA_PLATFORM=offscreen`.
The dialog title is `Phase C5 合成データ / 実行不可 / 外部通信なし`. The user can
inspect the 50-row list and switch between Result and Execution graph while the
controller's normal timer polls the fake client.

## Acceptance status

- Synthetic/offscreen checks: **PASS**, 4 new tests and 101 total across the seven focused
  Controller, C5, Graph, and Dialog test files; 0 skipped and 0 failed.
- `ruff check src tests scripts/phase_c5_controller_visible_review.py` and matching
  `ruff format --check`: **PASS**.
- `mypy src scripts/phase_c5_controller_visible_review.py` and `compileall`: **PASS**.
- CLI `--prepare`: **PASS**, 50 synthetic workflows, five graph nodes, valid synthetic DTOs,
  no QApplication or window creation, and zero real-data or external-communication access.
- Full pytest suite: **NOT_RUN**.
- Windows visible human inspection of regular and compact layouts: **NOT_RUN**.
- Codex implementation turn: `--show` is not run; real workflow jobs, Workflow POSTs,
  Bridge/Worker/MCP/API processes, and harness external communications are not started.
