# Secure MCP Tunnel Supervision and Tray Lifecycle Implementation Plan

> **For agentic workers:** This plan is executed inline in the existing checkout. Subagents, delegation, parallel agent work, model escalation, and new worktrees are prohibited by the approved Phase 4B request.

**Goal:** Add safe Console-owned Secure MCP Tunnel supervision and system-tray lifecycle while preserving the detached Bridge lifetime contract and the existing nine-tool read-only backend.

**Architecture:** Extend the console-only configuration with a bounded profile and optional executable override. Resolve only explicit or PATH candidates, then let a `TunnelSupervisor` own one asynchronous doctor `QProcess`, one managed tunnel `QProcess`, loopback health polling, and terminate-then-kill cleanup. `MainWindow` consumes the supervisor's state and action booleans for both window buttons and tray actions; close-to-tray and explicit Exit remain separate paths.

**Tech Stack:** Python 3.11+, stdlib, PySide6 `QProcess`, `QTimer`, `QTcpServer`, `QNetworkAccessManager`, `QSystemTrayIcon`; existing pytest and strict mypy setup.

**Spec:** Approved Phase 4B request in `C:\Users\weito\.codex\attachments\c9a87898-cb8e-4c3a-9640-0159b9482619\pasted-text.txt`; update `docs/superpowers/specs/2026-08-28-codexbridge-design.md` and `README.md` with the Phase 4B addendum.

## Global Constraints

- Manage only the Console-owned Tunnel; never stop, restart, terminate, or kill Bridge, App Server, Codex, active turns, or external Tunnel processes.
- Preserve `sys.executable -m codex_bridge` plus `startDetached()` in `runtime_launcher.py` and preserve Console close semantics for Bridge.
- Use no shell, `cmd.exe`, PowerShell, process enumeration, PID scan, taskkill, pkill, new dependency, new mutation endpoint, or profile creation/editing UI.
- Use `CODEX_BRIDGE_TUNNEL_EXECUTABLE` and bounded `CODEX_BRIDGE_TUNNEL_PROFILE` only; default profile is `codex-bridge` and valid characters are `[A-Za-z0-9._-]`, length 1..64.
- Explicit invalid Tunnel executable configuration fails closed without PATH fallback; `.ps1` candidates are excluded and Windows deduplication is canonical, case-insensitive.
- Doctor runs once asynchronously after Bridge readiness with inherited environment, `--health.listen-addr 127.0.0.1:0`, a 10-second timeout, and an 8 KiB combined stdout/stderr bound; raw output is discarded.
- Tunnel run uses an OS-selected loopback ephemeral health port stored only in process memory and a direct `QProcess.setProgram()` / `setArguments()` launch.
- Health polling is asynchronous, bounded at startup to about 10 seconds, uses `/healthz` and `/readyz`, discards response bodies, and continues at low frequency after `not_ready`.
- Automatic restart is disabled. Stop is `terminate()`, a QTimer-bounded two-second fallback to `kill()`, and no GUI-thread `waitForFinished()`.
- Tray uses only standard PySide6 classes. Available tray: close hides and ignores the event. Unavailable tray: close performs explicit cleanup. Explicit Exit stops only the Console-owned Tunnel before `QApplication.quit()`.
- Keep the MCP surface at nine tools and leave the Bridge backend unchanged.

---

### Task 1: Tunnel configuration and resolver

**Files:**
- Modify: `src/codex_bridge/console/config.py`
- Create: `src/codex_bridge/console/tunnel_resolver.py`
- Test: `tests/test_console_config.py`
- Create: `tests/test_console_tunnel_resolver.py`

**Interfaces:**
- `parse_tunnel_profile(value: object, *, source: str = "Tunnel profile") -> str` validates one non-empty profile using `[A-Za-z0-9._-]{1,64}`.
- `ConsoleConfig` gains `tunnel_executable: str | None = None` and `tunnel_profile: str = "codex-bridge"`; `from_sources()` reads `CODEX_BRIDGE_TUNNEL_EXECUTABLE` and `CODEX_BRIDGE_TUNNEL_PROFILE` without reading any secret environment value.
- `TunnelCandidate(path: str, source: str)` and `TunnelResolutionError(ValueError)` mirror the existing resolver boundary.
- `enumerate_candidates(environ: Mapping[str, str] | None = None, *, platform: str | None = None, which: Callable[[str], str | None] = shutil.which) -> tuple[TunnelCandidate, ...]` returns explicit-or-PATH candidates in priority order and raises for an invalid explicit override.

- [ ] **Step 1: Write the failing configuration and resolver tests.** Cover default profile, environment profile, invalid profile values, explicit executable, PATH `tunnel-client.exe` then `tunnel-client`, explicit invalid fail-closed, `.ps1` exclusion, and Windows case-insensitive deduplication.
- [ ] **Step 2: Run the focused tests to verify the missing interfaces fail.**

  Run: `uv run pytest -q tests/test_console_config.py tests/test_console_tunnel_resolver.py`

  Expected: collection or assertion failures because the Tunnel fields, parser, and resolver do not exist yet.
- [ ] **Step 3: Implement the smallest config/resolver code.** Use `Path.is_file()` for explicit and filesystem PATH results, use `shutil.which` only for the two bounded command names, reject `.ps1`, and compare `os.path.realpath()` through `ntpath.normcase()` on Windows.
- [ ] **Step 4: Run the focused tests and keep the resolver green.**

  Run: `uv run pytest -q tests/test_console_config.py tests/test_console_tunnel_resolver.py`

- [ ] **Step 5: Commit the independently testable configuration/resolver unit.**

  Run: `git add src/codex_bridge/console/config.py src/codex_bridge/console/tunnel_resolver.py tests/test_console_config.py tests/test_console_tunnel_resolver.py; git commit -m "feat: add Tunnel console configuration resolver"`

### Task 2: Doctor preflight and Tunnel process supervisor

**Files:**
- Create: `src/codex_bridge/console/tunnel_supervisor.py`
- Create: `tests/test_console_tunnel_supervisor.py`

**Interfaces:**
- `TunnelSupervisor(QObject)` exposes `state_changed = Signal(str)`, `message_changed = Signal(str)`, and `controls_changed = Signal(bool, bool, bool)`.
- Constructor: `TunnelSupervisor(*, executable: str | None, profile: str, process_factory: Callable[[QObject], Any] | None = None, network_manager: Any | None = None, health_port_provider: Callable[[], int] | None = None, clock: Callable[[], float] = monotonic, parent: QObject | None = None)`.
- `set_bridge_ready(ready: bool) -> None`, `start() -> bool`, `stop(*, on_finished: Callable[[], None] | None = None) -> bool`, `restart() -> bool`, `close(*, on_finished: Callable[[], None] | None = None) -> None`, `state` property, and `action_state` property are the only MainWindow-facing controls.
- `default_health_port_provider() -> int` binds `127.0.0.1:0` through `QTcpServer`, returns the selected port, closes immediately, and raises a bounded runtime error if allocation fails.
- `tunnel_state_label(state: str) -> str` returns only fixed safe UI text.

- [ ] **Step 1: Write failing tests for doctor command construction and safe bounded preflight.** Assert direct program/arguments, profile, `127.0.0.1:0`, inherited environment (no environment dump/setup), exit-zero pass, nonzero fail, timeout kill, combined 8 KiB bound, and absence of raw output from emitted messages.
- [ ] **Step 2: Run the doctor tests and verify they fail for the expected missing supervisor.**

  Run: `uv run pytest -q tests/test_console_tunnel_supervisor.py -k doctor`

- [ ] **Step 3: Implement asynchronous doctor preflight.** Start it once when `set_bridge_ready(True)` is first observed, count and discard both output channels, stop/kill only the doctor process, and transition to `ready_to_start` or `failed` with fixed safe text.
- [ ] **Step 4: Run the doctor tests to verify green.**

  Run: `uv run pytest -q tests/test_console_tunnel_supervisor.py -k doctor`
- [ ] **Step 5: Write failing tests for managed process lifecycle and ephemeral port use.** Assert exactly one start, `starting` then `running`, direct `run --profile ... --health.listen-addr 127.0.0.1:<injected-port>`, unexpected exit to `failed`, stop terminate, timer-bounded kill, restart only after finished with a new injected port, and duplicate transition requests are ignored.
- [ ] **Step 6: Run the lifecycle tests and verify they fail before implementation.**

  Run: `uv run pytest -q tests/test_console_tunnel_supervisor.py -k lifecycle`
- [ ] **Step 7: Implement the managed QProcess state machine.** Keep exactly one process reference, never enumerate or signal any other process, emit only fixed lifecycle events/messages, and allow no automatic restart.
- [ ] **Step 8: Run lifecycle tests and verify green.**

  Run: `uv run pytest -q tests/test_console_tunnel_supervisor.py -k lifecycle`
- [ ] **Step 9: Write failing tests for health/readiness polling.** Use fake replies/manager to cover health 2xx, ready 2xx, ten-second startup timeout to `not_ready`, later readiness recovery, bounded five-second steady polling, and body discard without body text in state/messages.
- [ ] **Step 10: Run health tests and verify the expected failure.**

  Run: `uv run pytest -q tests/test_console_tunnel_supervisor.py -k health`
- [ ] **Step 11: Implement asynchronous loopback health polling.** Track only reply identity and status code, abort/delete owned replies during stop/close, poll every 350 ms during startup and every 5 seconds after timeout, and transition to `ready` only for `/readyz` 2xx while the process is alive.
- [ ] **Step 12: Run the complete supervisor tests.**

  Run: `uv run pytest -q tests/test_console_tunnel_supervisor.py`
- [ ] **Step 13: Commit the supervisor unit.**

  Run: `git add src/codex_bridge/console/tunnel_supervisor.py tests/test_console_tunnel_supervisor.py; git commit -m "feat: add managed Tunnel supervisor"`

### Task 3: Console controls and system tray lifecycle

**Files:**
- Modify: `src/codex_bridge/console/main_window.py`
- Modify: `tests/test_console_main_window.py`
- Modify: `tests/test_console_entry.py`
- Create: `tests/test_console_tray.py`

**Interfaces:**
- `MainWindow` accepts optional `tunnel_supervisor`, `tray_factory`, `tray_available`, and `quit_application` test seams while retaining all existing constructor seams.
- Window buttons `start_tunnel_button`, `stop_tunnel_button`, and `restart_tunnel_button` call supervisor methods; their enabled state and tray action enabled state are applied from the one `action_state` signal/property.
- A ready Bridge observation calls `set_bridge_ready(True)`; loss of Bridge readiness never stops a running Tunnel.
- Available tray menu has `Show Console`, `Hide Console`, separator, `Start Tunnel`, `Stop Tunnel`, `Restart Tunnel`, separator, `Exit`; unavailable tray falls back to normal close cleanup.

- [ ] **Step 1: Extend fake supervisor/tray seams and write failing window/tray tests.** Cover Tunnel status label, Bridge-gated doctor start, common button/action enablement, available close hiding with no cleanup/stop, explicit Exit cleanup then quit, unavailable-tray close cleanup, and Bridge detached launcher behavior remaining unchanged.
- [ ] **Step 2: Run the focused UI tests and verify missing controls fail.**

  Run: `uv run pytest -q tests/test_console_main_window.py tests/test_console_tray.py`
- [ ] **Step 3: Implement the minimal MainWindow integration.** Add the fixed Tunnel label and three buttons, wire supervisor signals, use a single `_apply_tunnel_controls()` for window and tray actions, and keep existing Bridge status/timers unchanged.
- [ ] **Step 4: Implement tray construction and close/Exit distinction.** Create standard `QSystemTrayIcon`, `QMenu`, and `QAction` objects only when `isSystemTrayAvailable()` is true; `closeEvent()` hides/ignores only in that case, while explicit Exit stops timers/replies/probe first and then invokes `TunnelSupervisor.close()` before quitting.
- [ ] **Step 5: Run all Console tests and verify green.**

  Run: `uv run pytest -q tests/test_console_config.py tests/test_console_tunnel_resolver.py tests/test_console_tunnel_supervisor.py tests/test_console_main_window.py tests/test_console_tray.py tests/test_console_runtime_launcher.py tests/test_console_entry.py`
- [ ] **Step 6: Commit the Console/tray unit.**

  Run: `git add src/codex_bridge/console/main_window.py tests/test_console_main_window.py tests/test_console_tray.py tests/test_console_entry.py; git commit -m "feat: add Console Tunnel controls and tray lifecycle"`

### Task 4: Documentation and regression validation

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-08-28-codexbridge-design.md`
- Modify: `tests/test_console_docs.py`

- [ ] **Step 1: Write failing documentation assertions for the Phase 4B boundaries.** Assert both documents mention external profile creation, secret/identity exclusion, available-tray close, explicit Exit Tunnel cleanup, Bridge persistence, no external takeover, no auto-restart, and Phase 4C Bridge Stop/Restart scope.
- [ ] **Step 2: Run documentation tests and verify the new assertions fail.**

  Run: `uv run pytest -q tests/test_console_docs.py`
- [ ] **Step 3: Add only the README/spec Phase 4B addenda.** Do not rewrite historical Phase 1–4A text; document the config variables, direct process ownership, safe states, tray semantics, and out-of-scope Bridge lifecycle.
- [ ] **Step 4: Run documentation tests to verify green.**

  Run: `uv run pytest -q tests/test_console_docs.py`
- [ ] **Step 5: Run the full required validation.**

  Run: `uv run pytest`

  Run: `uv run ruff check .`

  Run: `uv run ruff format --check .`

  Run: `uv run mypy src`

  Run: `uv run python -m compileall -q src tests scripts`
- [ ] **Step 6: Run the existing real Codex smoke only if it is safe and does not stop/change existing Bridge/Tunnel/turn state.** Report inability or environmental reason rather than treating a skipped real run as success.
- [ ] **Step 7: Use a fake/disposable Tunnel executable for bounded supervisor dogfood.** Verify managed start, health/readiness, stop, restart, and tray Exit cleanup without touching a production profile or creating a new profile.
- [ ] **Step 8: Inspect the diff and status, then create the requested feature commit if any documentation or test fixes remain.**

  Run: `git diff --check; git status --short; git log --oneline -5`
- [ ] **Step 9: Push only `feature/tunnel-supervisor` to `origin/feature/tunnel-supervisor`; do not merge to main, force-push, rebase, tag, or release.**
