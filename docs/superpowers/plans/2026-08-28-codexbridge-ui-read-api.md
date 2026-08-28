# CodexBridge Phase 2 Local Read-Only UI API Implementation Plan

> **For agentic workers:** Implement this plan inline in the current checkout. Do not dispatch subagents, use delegation, escalate models, or create worktrees; the authoritative Phase 2 request requires one sequential agent.

**Goal:** Add a localhost-only read-only history/UI backend and safe Activity SSE stream while preserving the nine-tool MCP surface and Phase 1 writer semantics.

**Architecture:** Keep the MCP ASGI app and the UI ASGI app as separate listeners. Add a focused history projection module for App Server paginated and legacy reads, expose it through Bridge methods, and let a runtime-owned local UI server serve only safe Bridge/Activity views. ActivityStore publishes bounded safe Activity objects to non-blocking subscriber queues; no replay persistence is added.

**Tech Stack:** Python 3.11+, Starlette, Uvicorn, existing MCP SDK, asyncio, pytest/pytest-asyncio, Ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-08-28-codexbridge-design.md` plus the user's Phase 2 authoritative request.

## Global Constraints

- MCP remains at `config.host:config.port`, default `127.0.0.1:8000`.
- UI binds in code to `127.0.0.1` only and uses `CODEX_BRIDGE_UI_PORT`, default `8001`.
- UI port must be `1..65535` and differ from MCP port; no configurable UI host is added.
- The MCP surface remains exactly nine tools; no history methods become MCP tools.
- History calls validate `thread/read(includeTurns=false)` and allowed cwd before any history RPC.
- Paginated history uses only `thread/turns/list` or `thread/items/list`; legacy history uses bounded `thread/read(includeTurns=true)`.
- Raw ThreadItems, reasoning, opaque payloads, output bodies, full diffs, unsafe paths, and raw App Server notifications never enter public UI responses or SSE.
- UI routes are GET-only, have no CORS middleware, and use exact loopback TrustedHost validation.
- ActivityStore remains bounded to 500 records per thread; subscriber queues are bounded to 100 and drop oldest on overflow without blocking `add()`.
- Do not implement PySide6/Qt, Tunnel management, executable autodetection, SQLite, replay, WebSocket, mutation endpoints, or auto-restart.

### Task 1: Configuration and safe history projection

**Files:**
- Create: `src/codex_bridge/history.py`
- Modify: `src/codex_bridge/config.py`
- Test: `tests/test_config.py`, `tests/test_history.py`

**Interfaces:**
- `HistoryValidationError(ValueError)` is raised for invalid `limit`, `sort_direction`, unsupported legacy cursors, malformed upstream history, and unsafe history data.
- `project_turns_response(thread_id, response, limit) -> dict[str, object]` returns bounded `history_mode`, safe turn metadata, `next_cursor`, `backwards_cursor`, and `truncated`.
- `project_items_response(thread_id, requested_turn_id, response, limit) -> dict[str, object]` returns bounded safe `{turn_id,item}` entries and cursor metadata.
- `project_legacy_thread(thread_id, response, limit) -> dict[str, object]` returns bounded legacy turns/items with null cursors and explicit truncation.

- [ ] Add failing tests for default/parsed `ui_port`, invalid range, and UI/MCP collision.
- [ ] Add failing projection tests covering all required known item types, reasoning/unknown omission, bounded text, safe command/file/MCP/dynamic fields, and relative path filtering.
- [ ] Run `pytest tests/test_config.py tests/test_history.py -q` and confirm failures are feature-missing failures.
- [ ] Implement configuration parsing/validation and strict allowlist projection using `AllowedPathPolicy.safe_relative_path()`.
- [ ] Run the focused tests and confirm green.

### Task 2: Bounded Activity subscribers

**Files:**
- Modify: `src/codex_bridge/activity.py`
- Test: `tests/test_activity.py`

**Interfaces:**
- `ActivityStore.subscribe(thread_id: str | None = None) -> ActivitySubscription` registers a process-local subscription.
- `ActivitySubscription.get() -> Awaitable[Activity]` waits for the next matching Activity.
- `ActivitySubscription.close() -> None` unregisters idempotently.

- [ ] Add failing tests for subscription delivery, unsubscribe, optional thread filtering, 100-event backpressure, and latest-event retention.
- [ ] Run the focused tests and confirm they fail before implementation.
- [ ] Implement `asyncio.Queue(maxsize=100)` subscribers with oldest-drop `put_nowait()` publication from `add()` and explicit cleanup.
- [ ] Re-run Activity tests and the existing ring-buffer tests.

### Task 3: Bridge read-only history methods

**Files:**
- Modify: `src/codex_bridge/bridge.py`
- Test: `tests/test_bridge.py`, `tests/test_history.py`

**Interfaces:**
- `Bridge.read_thread_turns(thread_id, limit=20, cursor=None, sort_direction="desc") -> dict[str, Any]`.
- `Bridge.read_thread_items(thread_id, turn_id=None, limit=100, cursor=None, sort_direction="desc") -> dict[str, Any]`.

- [ ] Add failing fake-App-Server tests proving metadata validation precedes paginated RPC, exact params (`threadId`, optional cursor, `limit`, `sortDirection`, `itemsView=notLoaded`), cursor mapping, and item `turnId` preservation.
- [ ] Add failing legacy tests proving `includeTurns=true`, bounded output, reasoning exclusion, null cursors, and cursor rejection.
- [ ] Run the bridge/history tests and confirm expected failures.
- [ ] Implement metadata preflight via `thread/read` with `includeTurns=false`, projection delegation, and safe error propagation.
- [ ] Re-run focused tests and all existing Bridge tests.

### Task 4: Independent localhost UI ASGI application

**Files:**
- Create: `src/codex_bridge/ui_api.py`
- Test: `tests/test_ui_api.py`

**Interfaces:**
- `create_ui_app(bridge: Bridge, activity_store: ActivityStore, config: BridgeConfig) -> Starlette`.
- GET routes: `/healthz`, `/ui-api/status`, `/ui-api/threads`, `/ui-api/threads/{thread_id}`, `/ui-api/threads/{thread_id}/turns`, `/ui-api/threads/{thread_id}/items`, `/ui-api/threads/{thread_id}/status`, `/ui-api/events`.
- SSE emits `event: activity`, safe `id`, and JSON `data`; it sends `: keepalive` comments without recording them.

- [ ] Add failing API tests for happy paths, query validation, safe fixed errors (400/404/502/503), no mutation routes, and no `/mcp` route.
- [ ] Add failing security tests for TrustedHost rejection, no CORS wildcard, fixed status fields, and privacy-safe SSE payloads.
- [ ] Run UI tests and confirm missing-app failures.
- [ ] Implement GET-only Starlette routes, safe exception mapping, and a disconnect-aware async SSE generator.
- [ ] Re-run UI tests and existing MCP route/tool isolation tests.

### Task 5: UI server lifecycle integration

**Files:**
- Create: `src/codex_bridge/ui_server.py`
- Modify: `src/codex_bridge/server.py`, `src/codex_bridge/__main__.py`
- Test: `tests/test_ui_server.py`, `tests/test_server.py`

**Interfaces:**
- `LocalUiServer(app: Starlette, port: int)` owns a bounded `uvicorn.Server` task bound to `127.0.0.1`.
- `LocalUiServer.start()`, `LocalUiServer.shutdown(timeout: float)` are async and cleanup-safe.
- `BridgeRuntime` owns `LocalUiServer` and starts UI only after App Server startup; startup failure shuts down already-started resources.

- [ ] Add failing lifecycle tests for exact bind host/port, start order, cleanup after UI failure, bounded shutdown, and MCP/UI route separation.
- [ ] Run focused lifecycle tests and confirm failures.
- [ ] Implement the smallest Uvicorn wrapper and runtime integration without redesigning MCP `run_server()`.
- [ ] Re-run lifecycle tests plus existing server tests; verify MCP tool count remains nine.

### Task 6: Documentation and smoke coverage

**Files:**
- Modify: `README.md`, `docs/superpowers/specs/2026-08-28-codexbridge-design.md`
- Modify if needed: `scripts/integration_smoke.py`

- [ ] Add the Phase 2 listener, tunnel boundary, read-only/history, process-local Activity, and no-replay documentation.
- [ ] Extend the opt-in real smoke only with UI health/status and read-only history checks; use a fresh temporary allowed-root thread and never touch Tunnel.
- [ ] Run unit tests and every requested static check.

### Task 7: Delivery verification

- [ ] Run `pytest`, `ruff check`, `ruff format --check`, `mypy`, and `python -m compileall` with fresh output.
- [ ] Run the real smoke using the specified Codex executable when available; report each unavailable/failed stage honestly and do not blind-retry.
- [ ] Inspect `git diff --check`, tracked-secret patterns, `git diff --stat`, and `git status --short`.
- [ ] Commit Phase 2 as `feat: add local read-only UI API`.
- [ ] Push only `feature/ui-read-api` to `origin/feature/ui-read-api`; never merge Phase 2 into `main`.
- [ ] Verify local and remote Phase 2 SHA equality and clean status.
