# CodexBridge Phase 4A Runtime Launcher Implementation Plan

> **For agentic workers:** Execute this plan inline in the existing checkout. Do not dispatch subagents, create worktrees, or broaden scope into Phase 4B.

**Goal:** Add Windows-oriented Codex executable detection/version probing and a safe detached Bridge launcher to the existing read-only PySide6 Console while preserving the Phase 3 backend and close contract.

**Architecture:** Keep candidate discovery and version parsing in a small resolver module. Use a Qt-owned asynchronous process for bounded `--version` probes, and a separate launcher that uses `sys.executable -m codex_bridge` with inherited environment plus only the two authorized overrides. MainWindow coordinates resolver state, existing UI polling, detached launch state, and bounded readiness polling without owning or terminating the Bridge child.

**Tech Stack:** Python 3.11+, stdlib, PySide6 `QProcess`/`QTimer`/`QNetworkAccessManager`, pytest, existing Ruff/mypy/compileall checks.

**Spec:** User-approved Phase 4A request in the task message; design addendum is recorded in `docs/superpowers/specs/2026-08-28-codexbridge-design.md`.

## Global Constraints

- Preserve `--ui-port` then `CODEX_BRIDGE_UI_PORT` then `8001` precedence.
- Preserve the existing nine MCP tools and Phase 1–3 backend; do not add `/ui-api/status` fields.
- Use no new dependency beyond the existing PySide6 optional Console extra and stdlib.
- Explicit `CODEX_BRIDGE_CODEX_EXECUTABLE` is fail-closed: no fallback after invalid, unlaunchable, or unprobeable explicit input.
- Candidate order is explicit, Codex App native, PATH, then npm; dedupe canonical Windows paths case-insensitively.
- Probe `--version` asynchronously with about a 3-second timeout and bounded output; never block the GUI thread or expose raw output.
- Launch only `sys.executable -m codex_bridge`; never use shell, `codex-bridge.exe`, `.ps1`, `cmd.exe`, PowerShell, or `start` for Bridge launch.
- Inherit the current child environment and override only `CODEX_BRIDGE_CODEX_EXECUTABLE` and `CODEX_BRIDGE_UI_PORT`.
- Detached launch success is not readiness; readiness requires `/healthz` OK plus ready/connected Bridge and App Server status.
- Never auto-restart, stop, kill, terminate, interrupt, or replace an existing or Console-started Bridge.
- Do not display or persist raw environment, raw process output, credentials, tokens, tunnel identifiers, or reasoning.

### Task 1: Resolver candidates and asynchronous version probe

**Files:**
- Create: `src/codex_bridge/console/codex_resolver.py`
- Create: `tests/test_console_codex_resolver.py`

**Interfaces:**
- `CodexCandidate(path: str, source: str)` is the bounded candidate value.
- `CodexResolution(path: str, version: str, source: str)` is the bounded successful result.
- `enumerate_candidates(environ: Mapping[str, str] | None = None, *, platform: str | None = None, which: Callable[[str], str | None] = shutil.which) -> tuple[CodexCandidate, ...]` enumerates in priority order and raises a bounded explicit-override error rather than falling back when the override is set but not a usable path candidate.
- `parse_codex_version(output: bytes) -> str` accepts only bounded `codex-cli <version>` output and returns the version token.
- `CodexVersionProbe(QObject)` asynchronously probes candidates through injectable QProcess construction, emits one bounded success/failure signal, and aborts only its temporary probe process on close.

- [x] Write resolver candidate-order, explicit fail-closed, App native discovery, dedupe, version-parser, and fake-process probe tests.
- [x] Run the focused resolver tests and observe the expected missing-module/probe failures.
- [x] Implement only the discovery, bounded parsing, and Qt asynchronous probe behavior needed by those tests.
- [x] Run focused resolver tests to green, then run existing Console tests.

### Task 2: Inherited-environment detached launcher

**Files:**
- Create: `src/codex_bridge/console/runtime_launcher.py`
- Create: `tests/test_console_runtime_launcher.py`

**Interfaces:**
- `DetachedLaunchResult(started: bool, pid: int | None)` reports only process-local launch outcome.
- `BridgeRuntimeLauncher.launch(*, codex_executable: str, ui_port: int) -> DetachedLaunchResult` configures a QProcess with `sys.executable`, `['-m', 'codex_bridge']`, inherited environment, and exactly the two authorized overrides, then calls detached start once.
- `BridgeRuntimeLauncher.close()` releases launcher-owned temporary Qt state and never calls child termination APIs.

- [x] Write tests proving command, inherited environment, only-two-key override, detached call count, PID retention, and close non-termination.
- [x] Run focused launcher tests and observe the expected missing-module failures.
- [x] Implement the minimal launcher with injectable process/environment factories for deterministic tests.
- [x] Run focused launcher tests to green.

### Task 3: MainWindow runtime state and readiness integration

**Files:**
- Modify: `src/codex_bridge/console/main_window.py`
- Modify: `tests/test_console_main_window.py`
- Modify: `tests/test_console_entry.py` only if constructor injection requires preserving entrypoint behavior

**Interfaces:**
- Keep existing `ApiClient` GET/SSE behavior and selection-generation rules unchanged.
- Add runtime state labels and a `Start Bridge` button without changing the three-pane layout or adding writer controls.
- Detect/probe Codex at startup; observe existing Bridge via normal health/status polling and mark it `external` when already ready.
- On valid Codex plus unavailable Bridge, disable immediately on click, call detached launch once, monitor readiness every bounded interval for at most 10 seconds, and never re-enable after a successful detached launch merely because polling is unavailable.
- Stop all Console timers, abort client replies, abort version probe, and stop launcher readiness timers in `closeEvent`; do not terminate any Bridge/App Server/turn process.

- [x] Add failing UI tests for external ready detection, valid-candidate enablement, launching/button disablement, ready transition, timeout/no-relaunch, and close semantics.
- [x] Run focused UI tests and verify they fail for the missing runtime controls/state.
- [x] Implement the smallest MainWindow integration, keeping existing Phase 3 methods and statuses intact.
- [x] Run focused UI and full Console regression tests.

### Task 4: Documentation and verification

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-08-28-codexbridge-design.md`

- [x] Add the Phase 4A behavior, detached lifetime, external-Bridge non-replacement, no auto-restart, and Phase 4B exclusions without rewriting historical Phase 3 plans.
- [x] Run `pytest`, `ruff check .`, `ruff format --check .`, `mypy src`, and `python -m compileall -q src tests scripts` with the Console environment.
- [x] Perform external-Bridge dogfood without stopping the existing Bridge or Tunnel; perform detached dogfood only on isolated temporary ports/roots if safe, otherwise report why it was not run.
- [x] Inspect diff/status, commit the implementation, and push only `feature/runtime-launcher` to `origin`.
