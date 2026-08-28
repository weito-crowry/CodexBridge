# CodexBridge Phase 4C Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add authenticated, localhost-only graceful Bridge Stop/Restart control for Bridges launched by the current Console session, while preserving the existing MCP, Tunnel, tray, and Console Exit contracts.

**Architecture:** Generate a process-local `secrets.token_urlsafe(32)` token per detached launch and pass it only through the inherited child environment. Register the UI shutdown route only when the Bridge config contains a valid token and an injected callback; the route authenticates with constant-time comparison and schedules the callback as a Starlette background task, which requests `uvicorn.Server.should_exit = True`. The Console keeps ownership metadata locally, uses a bounded Qt POST, and coordinates Tunnel stop, Bridge disappearance confirmation, fresh-token relaunch, and optional Tunnel restoration through one lifecycle transition gate.

**Tech Stack:** Python 3.11+, stdlib `secrets`/`hmac`/`inspect`, Starlette, existing Uvicorn, PySide6 Qt Network/Widgets, pytest.

**Spec:** User-provided CodexBridge Phase 4C specification in the task prompt.

## Global Constraints

- Control is available only for a Bridge started by the current Console session.
- Tokens are random, ASCII URL-safe, 32-256 characters, process-local, and never persisted, logged, displayed, or sent through the Tunnel.
- The control route exists only on the fixed-loopback UI server at `POST /ui-api/control/shutdown`; it is never added to the MCP app.
- Missing, malformed, or incorrect `Authorization: Bearer <token>` returns fixed `403`; valid authentication returns fixed `202` and schedules shutdown after the response.
- Bridge shutdown uses the existing lifespan `BridgeRuntime.shutdown()` path; no PID scan, taskkill, OS signal, force kill, or MCP shutdown tool is added.
- Qt Network is the only Console HTTP client; all replies are bounded and cleaned by `abort_all()`.
- Console Exit never stops the detached Bridge; only explicit Stop/Restart controls do.
- MCP tool count remains exactly nine and no dependency is added.
- No new worktree, merge, rebase, force push, tag, release, or unrelated refactor.

---

### Task 1: Backend token configuration and authenticated UI route

**Files:**
- Modify: `src/codex_bridge/config.py`
- Modify: `src/codex_bridge/ui_api.py`
- Test: `tests/test_config.py`
- Test: `tests/test_ui_api.py`

**Interfaces:**
- `BridgeConfig.control_token: str | None` defaults to `None` for direct/manual launches.
- `create_ui_app(..., shutdown_callback=...)` accepts an optional sync/async callback.

**Steps:**
- [ ] Add failing token parsing/validation tests, including value-free errors.
- [ ] Run the focused config tests and confirm they fail for the missing field/behavior.
- [ ] Add bounded ASCII URL-safe parsing with unset/empty as `None` and no secret in errors.
- [ ] Add failing route-disabled, auth, fixed-response, and background-callback tests.
- [ ] Run focused UI tests and confirm the new tests fail for the missing route.
- [ ] Register the POST route only when token and callback are both present; use `hmac.compare_digest` and `BackgroundTask`.
- [ ] Run the focused backend tests and the existing UI route/privacy tests.

### Task 2: Outer Uvicorn graceful-exit plumbing and runtime wiring

**Files:**
- Modify: `src/codex_bridge/ui_server.py`
- Modify: `src/codex_bridge/server.py`
- Modify: `tests/test_server.py`
- Modify: `tests/test_ui_server.py`

**Interfaces:**
- `UvicornShutdownController.bind(server)` and `.request_shutdown()` provide injectable outer-server exit control.
- `build_runtime(config, *, shutdown_callback=None)` passes the callback into the UI app.

**Steps:**
- [ ] Add failing controller and callback-plumbing tests.
- [ ] Run them to confirm the expected `should_exit` and callback failures.
- [ ] Implement the small controller and preserve `BridgeRuntime.shutdown()` ordering and single ownership.
- [ ] Keep one-argument custom runtime factories compatible in `create_app`.
- [ ] Run server/lifespan/UI-server regression tests.

### Task 3: Qt control POST and detached-launch token environment

**Files:**
- Modify: `src/codex_bridge/console/api_client.py`
- Modify: `src/codex_bridge/console/runtime_launcher.py`
- Test: `tests/test_console_api_client.py`
- Test: `tests/test_console_runtime_launcher.py`

**Interfaces:**
- `ApiClient.post_control_shutdown(token, key)` performs a bodyless POST with `Accept` and `Authorization` headers and emits fixed safe success/failure signals.
- `BridgeRuntimeLauncher.launch(..., control_token)` overrides exactly `CODEX_BRIDGE_CODEX_EXECUTABLE`, `CODEX_BRIDGE_UI_PORT`, and `CODEX_BRIDGE_CONTROL_TOKEN`.

**Steps:**
- [ ] Extend fakes and write failing POST/header/status/error/abort tests.
- [ ] Run focused client tests to observe the red failures.
- [ ] Implement one bounded POST helper with no response-body propagation and reply cleanup.
- [ ] Write the failing fresh-token environment/argument test and run it red.
- [ ] Add the third inherited-environment override without changing detached `sys.executable -m codex_bridge` startup.
- [ ] Run client and launcher regression tests.

### Task 4: Console ownership state, Stop confirmation, Restart, and shared controls

**Files:**
- Modify: `src/codex_bridge/console/main_window.py`
- Modify: `tests/test_console_main_window.py`
- Modify: `tests/test_console_tray.py`

**Interfaces:**
- Add top/tray `Start Bridge`, `Stop Bridge`, and `Restart Bridge` actions backed by one enabled-state calculation.
- Keep process-local token/PID/generation and a non-blocking Qt timer for roughly ten-second Bridge disappearance confirmation.

**Steps:**
- [ ] Add failing tests for external protection, owned readiness, control gating, tunnel ordering, 202-vs-disappearance, timeout/no-kill, confirmed-stop relaunch, sudden-unreachable guard, and restart restoration.
- [ ] Run focused Console tests and verify the new behaviors fail before implementation.
- [ ] Generate a fresh token for every detached launch and retain it only while that launch remains owned and not confirmed stopped.
- [ ] Add one lifecycle transition gate that disables Bridge/Tunnel actions during checking/starting/stopping/restarting.
- [ ] Implement Stop as optional owned Tunnel stop, authenticated POST, then asynchronous health/status-unavailable confirmation.
- [ ] Implement confirmed-stop cleanup and Start re-enable; preserve fail-closed timeout/control-failure behavior.
- [ ] Implement Restart as remember Tunnel state, stop, shutdown/confirm, fresh launch, readiness, and one conditional Tunnel start.
- [ ] Preserve tray/window close and explicit Exit behavior, including no Bridge control POST on Exit.
- [ ] Run all Console tests, then the full unit suite.

### Task 5: Documentation, static validation, and isolated real dogfood

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-08-28-codexbridge-design.md`
- Modify: `tests/test_console_docs.py`
- Test/script as needed: `scripts/integration_smoke.py` (only if an isolated Phase 4C probe is safely reusable)

**Steps:**
- [ ] Add failing documentation assertions for the Phase 4C security/lifecycle boundaries.
- [ ] Update README/design spec without rewriting historical Phase 1-4B material.
- [ ] Run documentation tests and the full `uv run --no-sync pytest -q` suite.
- [ ] Run `uv run --no-sync ruff check .`, `uv run --no-sync ruff format --check .`, `uv run --no-sync mypy src`, and `uv run --no-sync python -m compileall -q src tests scripts`.
- [ ] Run only alternate-port, temporary-root, fresh-token Bridge control dogfood; never touch the existing production Bridge/Tunnel.
- [ ] Run the Restart probe and optional disposable Tunnel coordination probe; report any unavailable Codex smoke separately.
- [ ] Review diff/status, commit as `feat: add graceful Bridge control`, push `origin/feature/bridge-control`, and report exact evidence.
