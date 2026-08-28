# CodexBridge Phase 3 Desktop Console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Windows-oriented PySide6 read-only desktop viewer that consumes the existing localhost UI API and displays threads, persisted history, status, recent Activity, and live SSE Activity without owning or mutating Bridge/App Server processes.

**Architecture:** Keep the existing Bridge backend and nine-tool MCP surface unchanged. Add a small console-only configuration/entrypoint, a Qt Network client using one event-loop-owned `QNetworkAccessManager`, a pure incremental SSE parser, and focused Qt widgets coordinated by `MainWindow`; all selection and stream callbacks carry a generation so stale replies cannot update the selected thread.

**Tech Stack:** Python 3.11+, PySide6 `>=6.8,<7` optional extra, Qt Network, Qt Widgets, standard library JSON/argparse, pytest, Ruff, mypy.

**Spec:** User-approved Phase 3 request in `C:\Users\weito\.codex\attachments\93007c31-51b1-4e48-83d3-f633e779aa3f\pasted-text.txt`, with existing backend contract in `docs/superpowers/specs/2026-08-28-codexbridge-design.md`.

## Global Constraints

- PySide6 is optional and must not become a Bridge server runtime dependency.
- The only new dependency is `PySide6>=6.8,<7`; no `httpx`, `requests`, `aiohttp`, or `qasync`.
- Console network I/O uses Qt's event loop and `QNetworkAccessManager`; no blocking calls, `asyncio`, `QThread`, `waitForReadyRead()`, or `waitForFinished()`.
- The console performs only bounded GET requests to the existing UI API and never implements mutation, approval, input, start, resume, steer, or interrupt operations.
- The UI API host is fixed to `127.0.0.1`; port precedence is explicit CLI option, `CODEX_BRIDGE_UI_PORT`, then `8001`; valid range is `1..65535`.
- JSON errors and GUI errors are bounded messages and never expose raw response bodies, tracebacks, credentials, arguments, results, output, diffs, or reasoning.
- SSE parsing is incremental UTF-8, chunk-safe, event-boundary aware, and bounded to 256 KiB pending data; only safe JSON Activity objects are accepted.
- Thread selection increments a generation, aborts old selection requests and SSE, applies responses only when generation and thread match, then obtains a status snapshot before opening the new stream.
- Activity display is deduplicated by `activity_id` and bounded to 200 rows; persisted `items` are reversed from API `desc` order into old-to-new chronology.
- Closing the console stops timers and aborts owned replies only; it never shuts down Bridge, App Server, Tunnel, turns, or threads.
- No new worktree, sub-agent, delegation, merge into `main`, or unrelated refactor.

---

### Task 1: Optional packaging, console configuration, and lazy entrypoint

**Files:**
- Create: `src/codex_bridge/console_entry.py`
- Create: `src/codex_bridge/console/__init__.py`
- Create: `src/codex_bridge/console/config.py`
- Modify: `pyproject.toml`
- Test: `tests/test_console_config.py`
- Test: `tests/test_console_entry.py`

**Interfaces:**
- `ConsoleConfig(host: str, port: int)` is immutable and exposes `base_url` as `http://127.0.0.1:<port>`.
- `ConsoleConfig.from_sources(explicit_port: int | None = None, environ: Mapping[str, str] | None = None) -> ConsoleConfig` applies explicit CLI, environment, and default precedence.
- `parse_ui_port(value: object, *, source: str = "UI port") -> int` accepts only integer ports in `1..65535` and raises `ConsoleConfigurationError` with a bounded message.
- `console_entry.main(argv: Sequence[str] | None = None) -> int` parses `--ui-port`, lazily imports PySide6/main-window code only after configuration, and returns a nonzero bounded error when the `console` extra is absent.

- [ ] Add failing tests for default port 8001, environment override, explicit override, invalid low/high/non-integer values, and precedence.
- [ ] Add a failing structural/import test proving importing `codex_bridge.console_entry` does not import PySide6 and a failing missing-extra error-path test using a controlled import failure.
- [ ] Run `pytest tests/test_console_config.py tests/test_console_entry.py -q` and confirm failures are feature-missing failures.
- [ ] Add the `console = ["PySide6>=6.8,<7"]` optional extra and `codex-bridge-console = "codex_bridge.console_entry:main"` script without changing existing dependencies/scripts.
- [ ] Implement the immutable config and lazy entrypoint; use `argparse` for `--ui-port` and print only `CodexBridge Console requires the 'console' extra. Install with: uv sync --extra console` on import failure.
- [ ] Run the focused tests and verify they pass with PySide6 unavailable or mocked.

### Task 2: Pure incremental SSE parser and safe Activity validation

**Files:**
- Create: `src/codex_bridge/console/sse.py`
- Test: `tests/test_console_sse.py`

**Interfaces:**
- `SseEvent(event: str | None, event_id: str | None, data: str)` is an immutable parsed event.
- `SseParser(max_buffer_bytes: int = 256 * 1024)` exposes `feed(chunk: bytes) -> tuple[SseEvent, ...]`, `finish() -> tuple[SseEvent, ...]`, and `reset() -> None`.
- `SseProtocolError` is raised for invalid UTF-8, pending-buffer overflow, or malformed field input that cannot be safely bounded.
- `parse_activity_event(event: SseEvent) -> dict[str, object] | None` accepts only `event == "activity"`, parses a JSON object, requires `activity_id`, `timestamp`, `thread_id`, `turn_id`, `type`, `status`, `summary`, and `details` with safe scalar/list shapes, and returns `None` for keepalives, unknown events, malformed JSON, or invalid payloads.

- [ ] Add failing tests for one event, multiple events, event/id/data fields, comment keepalive, network line/chunk splits, multiple `data:` lines joined by `\n`, split UTF-8 multibyte characters, malformed JSON, incomplete final data, and 256 KiB overflow.
- [ ] Run `pytest tests/test_console_sse.py -q` and confirm the expected missing-module failures.
- [ ] Implement an incremental decoder plus line buffer; dispatch only on blank lines, ignore comments, reset event fields after dispatch, and discard state after overflow.
- [ ] Implement bounded Activity shape validation without returning raw payloads or unknown details.
- [ ] Re-run the SSE tests and verify all parser cases pass.

### Task 3: Qt Network API client and non-blocking stream lifecycle

**Files:**
- Create: `src/codex_bridge/console/api_client.py`
- Test: `tests/test_console_api_client.py`

**Interfaces:**
- `ApiClient(QObject)` owns one `QNetworkAccessManager` and signals `json_succeeded(key: str, payload: object)`, `json_failed(key: str, message: str)`, `activity_received(generation: int, activity: dict[str, object])`, and `stream_state_changed(generation: int, state: str)`.
- `ApiClient.get_json(path: str, *, key: str, query: Mapping[str, object] | None = None) -> bool` starts one asynchronous GET unless the same key is already in flight.
- `ApiClient.start_stream(thread_id: str, generation: int) -> None` aborts/replaces the prior stream, creates `/ui-api/events?thread_id=...`, and consumes `readyRead()` through `SseParser`.
- `ApiClient.abort_json_group(prefix: str) -> None`, `ApiClient.stop_stream() -> None`, and `ApiClient.abort_all() -> None` abort and `deleteLater()` owned replies.
- `parse_json_reply(status_code: int, body: bytes) -> object` returns parsed JSON only for 2xx and raises bounded internal errors for non-2xx/invalid JSON.

- [ ] Add failing pure tests for URL construction, fixed loopback base URL, safe mapping of HTTP/network/JSON failures, and GET-only request method.
- [ ] Add failing Qt/fake-reply tests that prove `finished` handles JSON and `readyRead` handles streamed SSE without blocking, duplicate keys are suppressed, replies are deleted later, and `abort_all()` aborts both JSON and stream replies.
- [ ] Run the focused tests and confirm feature-missing failures.
- [ ] Implement Qt Network request setup with `QNetworkRequest`, `QUrl`, `QUrlQuery`, `QNetworkReply.finished`, `QNetworkReply.readyRead`, HTTP status inspection, bounded error mapping, and per-reply parser/generation state.
- [ ] Ensure stale stream callbacks are ignored when the reply is no longer the active generation/reply and reconnect-worthy failures are signaled without raising into the GUI event loop.
- [ ] Re-run API client tests and verify no forbidden HTTP dependency or mutation verb exists in console code.

### Task 4: Safe timeline/activity projection helpers and focused widgets

**Files:**
- Create: `src/codex_bridge/console/widgets.py`
- Test: `tests/test_console_widgets.py`

**Interfaces:**
- `timeline_entries(items_payload: Mapping[str, object]) -> tuple[TimelineEntry, ...]` reverses safe API `desc` items and skips unknown/malformed item types.
- `TimelineEntry(turn_id: str, item_id: str, kind: str, title: str, body: str, status: str | None, details: tuple[str, ...])` contains only display-safe text.
- `activity_row(activity: Mapping[str, object]) -> str` renders timestamp/type/status/summary plus allowlisted `exit_code`, `paths`, or `decision`, never a dict dump.
- `ThreadListPane(QWidget)`, `HistoryPane(QWidget)`, and `ActivityPane(QWidget)` expose `set_threads`, `set_timeline`, `set_snapshot`, `append_activity`, `set_empty_state`, and `set_error` methods; message bodies use plain-text widgets and work cards use compact labels.

- [ ] Add failing projection tests for desc-to-chronological ordering, turn separators, known message/work types, safe fields only, unknown-item omission, plain-text `<` handling, activity detail allowlist, 200-row cap, and `activity_id` dedupe.
- [ ] Run the focused tests and confirm the expected failures.
- [ ] Implement pure projection/render helpers first, then compact Qt widgets using native fonts, selectable `QTextEdit`/`QLabel` text, and no external assets/icons.
- [ ] Implement filter matching across name/preview/cwd/id and row display priority name, preview, id.
- [ ] Re-run widget tests and inspect the output strings for absence of raw dict/unsafe content.

### Task 5: MainWindow orchestration, polling, stale protection, and cleanup

**Files:**
- Create: `src/codex_bridge/console/main_window.py`
- Test: `tests/test_console_main_window.py`

**Interfaces:**
- `MainWindow(ApiClient, ConsoleConfig, parent: QWidget | None = None)` creates a `QMainWindow` sized approximately `1400x850` with left thread pane, center history pane, right activity pane, and bottom bounded status label.
- `MainWindow.select_thread(thread_id: str | None) -> None` increments a private selection generation, clears stale panes, aborts old selection/SSE, fetches detail/turns/items/status, and applies only matching results.
- `MainWindow.apply_json_result(key: str, payload: object) -> None` is the single guarded response application path; a key includes selection generation where needed.
- `MainWindow.closeEvent(event: QCloseEvent) -> None` stops timers, aborts client replies/stream, and accepts the event without invoking any Bridge mutation or shutdown method.

- [ ] Add failing tests for construction, three-pane layout, disconnected empty state, connected state fields (`bridge`, `app_server`, `stream`), selection request set, A-response rejection after B-selection, stale SSE rejection, activity append/dedupe/cap, and close cleanup.
- [ ] Add failing tests for fixed 5-second health/status polling, 10-second thread polling, selected status polling, no duplicate in-flight requests, Load older cursor/prepend/dedupe, and 1.5-second reconnect after a non-intentional stream error with status resync first.
- [ ] Run the focused main-window tests using `QT_QPA_PLATFORM=offscreen` and confirm failures are caused by missing implementation.
- [ ] Implement `QApplication`-safe construction, Fusion style/dark compact stylesheet, splitters, timers, signal wiring, and guarded generation handling.
- [ ] Implement thread refresh/filter/selection, four snapshot GETs, older-page loading, status/pending display, safe activity append, SSE start-after-snapshot, and fixed-delay reconnect.
- [ ] Keep all close behavior local to timers/replies; do not call `Bridge`, `AppServerClient`, QProcess, or any mutation route.
- [ ] Re-run main-window tests and verify the smoke assertions pass offscreen.

### Task 6: Documentation, dependency lock, and Phase 3 spec reconciliation

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-08-28-codexbridge-design.md`
- Modify: `uv.lock`
- Test: `tests/test_console_docs.py`

- [ ] Add a failing documentation test for the exact commands `uv sync --extra dev --extra console`, `uv run codex-bridge`, and `uv run codex-bridge-console`, plus read-only/process/loopback/Tunnel boundaries.
- [ ] Run the documentation test and confirm the expected failure.
- [ ] Update README with Phase 3 setup, entrypoint, read-only semantics, Bridge-first launch order, close behavior, loopback-only UI API, and the replacement for the old PySide6-out-of-scope statement.
- [ ] Add a concise Phase 3 addendum to the design spec covering the Qt Network/SSE architecture and explicit exclusions without changing Phase 2 backend contracts.
- [ ] Run `uv lock`/`uv sync --extra dev --extra console` and verify only PySide6 and its resolver-required transitive packages are added; preserve the existing `dev` extra.
- [ ] Re-run the documentation test and the complete unit suite.

### Task 7: Full verification, real smoke/dogfood, commit, and push

**Files:**
- Modify only files required by the preceding tasks; do not change backend API behavior.

- [ ] Run `uv sync --extra dev --extra console` and capture exit status.
- [ ] Run `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src`, and `uv run python -m compileall -q src tests scripts`; report each command independently.
- [ ] Run the existing real Codex smoke with `C:\Users\weito\AppData\Local\OpenAI\Codex\bin\a5c9108151f176e9\codex.exe` when the required environment is available; report each start, approval, continue, UI history, SSE, restart/resume, and steer stage without blind retry.
- [ ] Dogfood the Console against a separately started Bridge, verifying launch, connected status, thread list, selection, history, status, SSE Activity, and Console close.
- [ ] Verify after Console close that the Bridge process and any active Codex turn/control path remain alive; do not shut down or mutate them from the Console.
- [ ] Inspect `git diff --check`, forbidden mutation strings/imports, `git diff --stat`, and `git status --short`; confirm no worktree was created and MCP tool count remains nine.
- [ ] Commit all Phase 3 changes on `feature/desktop-console` with `feat: add read-only desktop console`.
- [ ] Push to `origin/feature/desktop-console`, then verify local/remote SHA equality and report the exact commit, branch, stats, status, and any unresolved environmental limitation.
