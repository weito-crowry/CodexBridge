# Phase C5-P1 Qt compact layout and offline acceptance

## Result

- Gate 0: **PASS** — 199 passed, 2 skipped, 0 failed across the requested offline test files.
- Gate 1: **PASS** — Qt offscreen kept the dialog at both 900×760 and 720×600; the fixed action row stayed reachable while compact content could be scrolled to.
- C5-P1: **PASS for offline acceptance only**. This does not establish visible-GUI inspection or C5 Live success.

## Start point and environment

- Branch: `codex/workflow-phase-c5-p1-compact-layout`
- Start HEAD: `d26571ea3254f60cc5f5ed02801932c5b39bbc1c` (`codex/workflow-phase-c5-p0-gui-offline-acceptance`)
- Windows Native: Windows 11 `10.0.22631`
- Python: `3.14.10`; `pyproject.toml` still requires `>=3.11`.
- PySide6 / Qt: `6.11.2` / `6.11.2`; DBOS: `3.4.0`.
- `poc/ows/` source and fixtures are present; there is no independent `ows` Python module in this environment.
- Used the existing `.venv` directly. No `uv sync`, dependency update, or lockfile change was made.

## Qt size cause and correction

Before the change, Qt reported the dialog and root layout minimum as 748×665. The width was driven by the graph tab: its graph view had a 360px minimum width; the splitter and details pane brought the splitter minimum to 704px, the graph widget to 722px, and the tab widget to 726px. The dialog's 11px root margins on both sides raised that to 748px. Vertically, the upper workflow list/details minimum was 213px, the tab minimum was 360px, and the fixed buttons, status labels, layout spacing, and margins brought the root minimum to 665px. The action row itself needed 592×20px and was not the limiting child.

The dialog now places the existing workflow list/details, Codex jobs group, and Result/Execution graph tabs inside a `QScrollArea` with `widgetResizable=True`. Status labels and the existing five action buttons remain outside the scroll area. The tab area is capped at 400px so its large Graph size hint does not introduce a vertical scrollbar in the regular 900×760 view. Existing list, result, graph, and detail widgets are retained.

Measured after the change with fake in-memory DTOs and the Graph tab selected:

| Qt offscreen case | Dialog | Scroll viewport | Content widget | Scroll range H/V | Graph view viewport |
|---|---:|---:|---:|---:|---:|
| Regular, jobs hidden | 900×760 | 876×672 | 876×672 | 0 / 0 | 358×288 |
| Compact, one fake job shown | 720×600 | 682×498 | 776×796 | 94 / 298 | 358×288 |

The dialog minimum is now 614×156, below both requested sizes. In the regular case, content minimum hint was 744×597 and its size hint 744×655, so it fit without scrolling. In the compact case, content minimum hint was 776×712, so both scrollbars exposed the overflow. At 720×600, the five button centers were at `(53,578)`, `(151,578)`, `(279,578)`, `(437,578)`, and `(668,578)` inside the dialog. The test verified the active Refresh and New run buttons by clicking them at both sizes; the test dialog had no controller attached.

The offline test also scrolled to the workflow list and fake jobs, reached the graph viewport and node details, switched back to Result, and scrolled the full 80-line result text to its final line. Workflow selection and result text survived tab changes. No screenshot was saved. A human did not inspect a visible Windows GUI.

Static dialog labels were also measured with the existing English and `ja-JP` catalogs selected in memory. Both languages kept exact 900×760 and 720×600 sizes, positive content geometry, and visible button centers inside the dialog. Regular size needed no scrollbars in either language. With one fake job at compact size, English had horizontal/vertical ranges 94/298px; Japanese needed only 284px vertical scrolling because its 528px content minimum width fit within the 682px viewport.

## Gate 0: offline tests

Each listed file was run separately with `QT_QPA_PLATFORM=offscreen` and the existing `.venv\Scripts\python.exe -B -m pytest -q -rs`.

| Test file | Result |
|---|---:|
| `tests/test_phase_c5_gui_offline_acceptance.py` | 4 passed |
| `tests/test_console_workflow_graph_widget.py` | 8 passed |
| `tests/test_console_workflow_controller.py` | 20 passed |
| `tests/test_console_workflows_dialog.py` | 32 passed |
| `tests/test_console_api_client.py` | 20 passed |
| `tests/test_workflow_task_projection.py` | 61 passed, 1 skipped |
| `tests/test_workflow_task_api.py` | 9 passed |
| `tests/test_workflow_task_mcp.py` | 8 passed |
| `tests/test_workflow_node_events.py` | 20 passed, 1 skipped |
| `tests/test_workflow_graph.py` | 17 passed |

The two skipped tests require creating a symlink, which Windows denied (`OSError`). Skips are not included in the passed count. The requested `test_workflows_graph.py` filename is absent; the repository's actual graph test is `test_workflow_graph.py` and was run.

The GUI acceptance file uses PySide6 `QApplication`, the real dialog/graph widgets, and fake payloads only. Its scenarios cover Result-first selection, graph tab switching, succeeded/running/failed/unknown/needs-reconciliation states, unavailable coverage, zero and repeated executions, fake one/two/three-job data, virtual `@scope:` edges, graph snapshot identity and selection/history behavior, and fake timer/event pagination. The expanded layout test covers both sizes, button reachability, scrolling, and result preservation.

API regression tests use fake clients or in-process `httpx.ASGITransport`; MCP regression uses AnyIO memory streams. No external HTTP request or MCP/CLI call was made by the tests.

## Static checks

- `.venv\Scripts\ruff.exe check src tests`: PASS (`All checks passed!`)
- `.venv\Scripts\ruff.exe format --check src tests`: PASS (`295 files already formatted`)
- `.venv\Scripts\mypy.exe src`: PASS (`132 source files`, no issues)
- `.venv\Scripts\python.exe -B -m compileall -q src`: PASS (exit 0)
- `git diff --check`: PASS (exit 0)

## Execution boundaries and next step

- Actual Codex jobs: 0; Workflow HTTP POST to a live service: 0; live MCP/CLI Codex calls: 0.
- Bridge start/stop: 0; production ports `18000/18001`, production state, real sessions/jobs, existing runs, and the old B5 unknown remote outcome were not contacted or changed.
- No visible GUI process was started or stopped. GUI evidence is Qt offscreen only; screenshot: none; human visible-GUI inspection: not run.
- The previous `docs/phase_c5_p0_gui_offline_acceptance.md` was not changed.

C5 Live remains a separate stage requiring explicit approval. Its isolated proposal is three independent runs/workspaces: one run with 1 Codex job, one with 2, and one with 3. Before that stage, approval must specify the total Codex-start limit, prohibit resending an uncertain result, and prohibit production ports and existing sessions/jobs. This work does not proceed to that stage.
