# CodexBridge Dogfood Hardening Implementation Plan

> **For agentic workers:** This approved request is executed inline by the single agent. No subagents, delegation, new worktree, model escalation, merge, or user-config mutation is allowed.

**Goal:** Fix the dogfood-discovered configuration, resolver, Console state, shutdown, Tunnel diagnostics, MCP error, and thread metadata defects while preserving existing public tools and security boundaries.

**Architecture:** Add one stdlib-only user-config loader and a pure shared Codex executable resolver, then let Bridge and Console consume the same effective configuration precedence. Extend existing Console lifecycle/state machines with bounded ownership-aware shutdown, usable standard tray icon, explicit idle/empty states, and Tunnel version metadata; keep the MCP/App Server protocols unchanged and add thread metadata only as a nested snapshot field.

**Tech Stack:** Python 3.11+, `tomllib`, asyncio, Starlette/MCPServer 2.x, PySide6 Qt event loop, pytest, Ruff, mypy, uv.

**Spec:** User-provided dogfood hardening specification in the task prompt.

## Global Constraints

- Preserve existing MCP tool names, required arguments, response fields, history, approval/input, diff, activity, cwd validation, root security, ownership tracking, and Tunnel doctor/readiness behavior.
- Precedence is explicit CLI option > environment variable > user config > existing default.
- Empty allowed roots fail closed; never infer Documents or home.
- `CODEX_BRIDGE_ALLOWED_ROOTS` remains `os.pathsep`-separated; TOML uses `array[string]`.
- Minimum Tunnel client version is `0.0.14`; do not add OAuth metadata workarounds or commit binaries.
- Never modify the real user config, `.tools` contents, active self-hosting services, or existing uncommitted files.

### Task 1: Shared configuration and allowed-root preflight

**Files:**
- Create: `src/codex_bridge/config_file.py`
- Modify: `src/codex_bridge/config.py`
- Modify: `src/codex_bridge/console/config.py`
- Modify: `src/codex_bridge/__main__.py`
- Modify: `src/codex_bridge/console_entry.py`
- Modify: `src/codex_bridge/console/runtime_launcher.py`
- Test: `tests/test_config.py`, `tests/test_console_config.py`, `tests/test_console_runtime_launcher.py`, `tests/test_paths.py`

- [x] Write RED tests for missing/valid/malformed/wrong-type TOML, config override, precedence, secret exclusion, root-array parsing, empty-root fail-closed startup, and child environment propagation.
- [x] Run the focused tests and confirm failures are caused by missing config/preflight behavior.
- [x] Implement a `tomllib` loader with Windows `%APPDATA%\\CodexBridge\\config.toml`, XDG fallback, `CODEX_BRIDGE_CONFIG`, strict supported types, and bounded configuration errors.
- [x] Implement explicit CLI options needed for Bridge/Console effective settings without changing existing environment names; keep existing defaults and secrets environment-only.
- [x] Validate configured roots as absolute, existing directories before runtime creation; expose root count/readiness to Console and inject effective roots into Console-launched Bridge children.
- [x] Re-run focused tests, then refactor only after green.

### Task 2: Common Codex resolver and Tunnel resolver/version diagnostics

**Files:**
- Create: `src/codex_bridge/codex_resolver.py`
- Modify: `src/codex_bridge/console/codex_resolver.py`
- Modify: `src/codex_bridge/console/tunnel_resolver.py`
- Modify: `src/codex_bridge/console/tunnel_supervisor.py`
- Modify: `src/codex_bridge/console/main_window.py`
- Modify: `src/codex_bridge/server.py`, `src/codex_bridge/app_server.py`
- Test: `tests/test_console_codex_resolver.py`, `tests/test_console_tunnel_resolver.py`, `tests/test_console_tunnel_supervisor.py`, `tests/test_config.py`

- [x] Write RED tests for shared Codex priority, clear not-found error, Console/direct resolver identity, Tunnel explicit/config/local/PATH priority, unrelated-project exclusion, invalid explicit/config fail-closed, semantic version parsing, and the no-doctor-on-old-version rule.
- [x] Run focused tests and confirm expected failures.
- [x] Move pure Codex candidate enumeration and not-found validation out of the PySide6-only module; retain the existing Qt version probe and native `.cmd` handling.
- [x] Add checkout-local Tunnel marker detection and carry candidate source/path/version into the supervisor and top status UI.
- [x] Add bounded `--version` validation before `doctor`, accepting `>=0.0.14` including future versions and rejecting missing/invalid/old versions without exposing raw output.
- [x] Make direct Bridge startup resolve the executable before spawning and report a safe `ConfigurationError` rather than `FileNotFoundError`.
- [x] Re-run focused tests and keep all existing doctor/readiness tests green.

### Task 3: MCP expected errors and thread metadata snapshots

**Files:**
- Modify: `src/codex_bridge/server.py`
- Modify: `src/codex_bridge/bridge.py`
- Modify: `src/codex_bridge/state.py`
- Modify: `src/codex_bridge/models.py`
- Modify: `src/codex_bridge/ui_api.py`
- Modify: `README.md`
- Test: `tests/test_server.py`, `tests/test_bridge.py`, `tests/test_state.py`, `tests/test_ui_api.py`

- [x] Write RED tests showing `PathPolicyError` reaches MCP callers as a safe expected `ToolError`, while unrelated internal exceptions remain `UnexpectedToolError`; add metadata preservation/update tests for start, continue/resume, public snapshots, wait, missing metadata, and existing fields.
- [x] Run the focused tests and verify the expected failures.
- [x] Raise SDK 2.x `ToolError` only for expected/config/input exceptions at the MCP tool boundary; preserve generic handling for unexpected failures.
- [x] Add null-safe normalized `thread_metadata` storage to `StateStore`, update it from thread/start/read/resume/list responses when available, and include it in all public snapshots without removing fields.
- [x] Re-run focused tests and then the existing bridge/UI tests.

### Task 4: Console empty state, stream state, tray icon, and unified bounded shutdown

**Files:**
- Modify: `src/codex_bridge/console/main_window.py`
- Modify: `src/codex_bridge/console/api_client.py`
- Modify: `src/codex_bridge/console_entry.py`
- Test: `tests/test_console_main_window.py`, `tests/test_console_tray.py`, `tests/test_console_api_client.py`, `tests/test_console_entry.py`

- [x] Write RED tests for ready/no-thread, ready/thread-list/no-selection, deselection, recovery, idle stream, non-null standard icon, tray usability fallback, one-time notification, Ctrl+C signal routing, external-vs-owned Bridge shutdown ordering, bounded timeout, and idempotent callbacks.
- [x] Run focused tests and verify they fail for current behavior.
- [x] Separate Bridge availability from thread selection and use the specified History/Activity empty states; set stream idle on no selection and reconnect only for an active selected thread.
- [x] Create one non-null Qt standard icon for window/tray, enable hide-on-close only when actually usable, and preserve all existing tray actions.
- [x] Route tray Exit, window close, and SIGINT through one idempotent shutdown path: stop Console-owned Tunnel first, stop only Console-owned Bridge via control endpoint, bounded confirmation, cancel Console-owned requests/probes/timers, hide tray icon, quit QApplication; external Bridge is never stopped.
- [x] Re-run focused tests and inspect Qt warnings/process-safe behavior without touching the active production runtime.

### Task 5: Documentation and full verification

**Files:**
- Modify: `README.md`
- Test: all existing and new tests

- [x] Add config locations/schema/precedence, required roots, resolver order, Tunnel version floor/source/path display, shutdown behavior, thread metadata meaning, and optional plugin note.
- [x] Run `uv sync --extra dev --extra console` without stopping active processes; if a locked executable prevents sync, use the repository environment with `uv run --no-sync` and report the limitation.
- [x] Run `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src`, and `uv run python -m compileall src`.
- [x] Review diff and tracked/untracked scope, verify `.tools/` remains untouched, commit the implementation, push the feature branch, and report any unavailable checks and remaining manual dogfood steps.
