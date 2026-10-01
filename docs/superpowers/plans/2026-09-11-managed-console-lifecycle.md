# Managed Console Lifecycle Implementation Plan

> **Execution mode:** Single-agent sequential implementation only. Do not use subagents, multi-agent delegation, parallel agent work, or model escalation. The design spec is approved and must be treated as authoritative; do not stop for brainstorming/planning re-approval.

**Goal:** Make CodexBridge Console automatically bring Bridge/App Server/Tunnel to a usable state, recover routine failures automatically, and reduce normal user interaction to launch, tray-hide, status/retry/restart, and tray exit.

**Architecture:** Keep the existing Console/MainWindow, RuntimeLauncher, and TunnelSupervisor architecture. Add bounded managed-lifecycle state/timers around existing start/stop/readiness paths instead of introducing a new daemon/service. MainWindow owns Bridge lifecycle/recovery and aggregate status; TunnelSupervisor owns Tunnel autostart/recovery; existing ownership boundaries remain authoritative.

**Tech Stack:** Python 3.10+, PySide6/Qt timers and QProcess, existing CodexBridge UI API and RuntimeLauncher/TunnelSupervisor, pytest, Ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-09-11-managed-console-lifecycle-design.md`

## Global Constraints
- Treat the approved spec as authoritative; no redesign.
- Do not create a new worktree when the current checkout is safe.
- Preserve all existing uncommitted user changes; never reset, checkout, stash, or delete them.
- Do not change Bridge/App Server protocols unless strictly required by existing interfaces; this plan requires no protocol change.
- Do not implement Windows Service/daemon architecture or external Tunnel discovery protocol.
- Do not redesign Usage or Codex update-check behavior.
- No unrelated refactor or incidental feature additions.
- User-owned external processes must never be killed.
- Review is performed by ChatGPT after implementation; implementation completion is not merge approval.

---

### Task 1: Managed Bridge startup and bounded retry

**Files:**
- Modify: `src/codex_bridge/console/main_window.py`
- Test: `tests/test_console_main_window.py`

**Interfaces:**
- Reuse `_start_bridge`, `_launch_bridge`, `_on_readiness_tick`, `_apply_runtime_observation`, `_set_runtime_state`, `_update_start_button`.
- Add internal managed-start retry state/timer only inside MainWindow.
- Produce context action `_retry_now()` for Task 4.

- [ ] **Step 1: Write failing tests for startup automation and retry schedule**

Add tests that establish these observable contracts:

```python
assert existing_ready_bridge_does_not_launch_new_process()
assert unavailable_bridge_with_valid_config_auto_starts()
assert bridge_launch_failures_retry_after_ms == [2000, 5000, 10000]
assert retries_exhausted_sets_managed_bridge_error()
assert retry_now_cancels_wait_and_attempts_immediately()
```

Use existing FakeClient/FakeLauncher patterns. Test timer intervals/state directly; do not sleep.

- [ ] **Step 2: Run only the new startup/retry tests and verify they fail for missing behavior**

Run the exact pytest node ids added in Step 1 with `pytest -q`.

- [ ] **Step 3: Implement minimal managed Bridge startup state**

Add constants equivalent to:

```python
_BRIDGE_START_RETRY_DELAYS_MS = (2_000, 5_000, 10_000)
_BRIDGE_LOSS_THRESHOLD = 2
```

Add a single-shot retry timer and fields for retry index/exhaustion. Extract a non-UI `_can_start_bridge()` predicate from the existing Start Bridge enablement conditions so both UI and automation use the same rules. After health/status have established that no usable Bridge exists, automatically call the existing start path. On launch failure/10-second readiness timeout, schedule the next delay. After all three retries are exhausted, stop automatic startup until `_retry_now()` or a new availability epoch.

- [ ] **Step 4: Run startup/retry tests and existing Bridge lifecycle tests**

Run the new node ids plus existing tests covering `_start_bridge`, `_stop_bridge`, `_restart_bridge`, readiness timeout, and external ownership.

- [ ] **Step 5: Commit Task 1**

Commit only Task 1 code/tests with message `feat: automate managed bridge startup`.

---

### Task 2: Automatic Bridge loss recovery and external takeover

**Files:**
- Modify: `src/codex_bridge/console/main_window.py`
- Test: `tests/test_console_main_window.py`

**Interfaces:**
- Consume Task 1 `_can_start_bridge()` and managed retry state.
- Reuse `_runtime_state`, `_bridge_control_allowed`, `_apply_runtime_observation`, `_request_bridge_status`, and existing ownership fields.

- [ ] **Step 1: Write failing tests for loss detection and takeover**

Add tests with these contracts:

```python
assert one_failed_status_observation_does_not_restart_owned_bridge()
assert two_consecutive_failed_status_observations_restart_owned_bridge()
assert successful_status_observation_resets_loss_counter()
assert two_consecutive_failed_status_observations_take_over_external_bridge()
assert external_ready_bridge_is_never_stopped_or_killed()
```

Count loss once per completed Bridge status observation, not once per individual health/status callback, to avoid double-counting a single polling cycle.

- [ ] **Step 2: Run new recovery tests and verify failure**

Run only the new node ids.

- [ ] **Step 3: Implement consecutive-loss recovery**

Maintain a consecutive unavailable-status counter in MainWindow. A ready `bridge-status` observation resets it. Two consecutive unavailable/failed status observations trigger recovery only when Console is not closing and no explicit Bridge transition is already active. For Console-owned Bridge, enter the existing restart/start path without OS-level killing. For external Bridge, forget only Console-side external availability and start a new Console-owned Bridge once the configured UI endpoint is actually unavailable. Never send shutdown to the external Bridge.

- [ ] **Step 4: Run recovery and ownership tests**

Run the new tests plus current external/managed ownership, stop confirmation, and exit tests.

- [ ] **Step 5: Commit Task 2**

Commit message: `feat: recover managed bridge availability`.

---

### Task 3: Tunnel autostart and recovery in TunnelSupervisor

**Files:**
- Modify: `src/codex_bridge/console/tunnel_supervisor.py`
- Modify if required for wiring only: `src/codex_bridge/console/main_window.py`
- Test: existing TunnelSupervisor test file(s)
- Test: `tests/test_console_main_window.py` only for integration-visible status/wiring

**Interfaces:**
- Reuse `set_bridge_ready`, `_start_version_once`, `_start_doctor_once`, `_on_doctor_finished`, `start`, `_start_process`, `_finish_unexpected_exit`, `stop`, `restart`, `close`, `action_state`.
- Add public `retry_now()` to TunnelSupervisor for Task 4.

- [ ] **Step 1: Write failing TunnelSupervisor tests**

Add deterministic timer/state tests proving:

```python
assert doctor_pass_and_bridge_ready_auto_starts_tunnel()
assert bridge_not_ready_never_auto_starts_tunnel()
assert unexpected_exit_retry_delays_ms == [1000, 3000, 10000, 30000, 60000, 60000]
assert successful_running_or_ready_state_resets_recovery_sequence()
assert explicit_stop_does_not_schedule_recovery()
assert close_does_not_schedule_recovery()
assert retry_now_attempts_immediately_when_bridge_and_preflight_are_ready()
```

Use fake QProcess/timer hooks already present in tests; do not sleep.

- [ ] **Step 2: Run new TunnelSupervisor tests and verify failure**

Run only the new node ids.

- [ ] **Step 3: Implement Tunnel autostart and recovery**

Add constants equivalent to:

```python
_TUNNEL_RECOVERY_DELAYS_MS = (1_000, 3_000, 10_000, 30_000)
_TUNNEL_RECOVERY_FALLBACK_MS = 60_000
```

Use one single-shot recovery timer and a retry index. When Bridge is ready and doctor passes, autostart if no managed process/transition exists. Unexpected process exit schedules the bounded sequence, then repeats at 60 seconds. Explicit stop/restart/close paths must cancel or suppress unintended recovery. Losing Bridge readiness cancels pending Tunnel recovery. `retry_now()` cancels a pending wait and immediately reuses preflight/start state when possible; if preflight has not passed, it restarts the existing preflight path rather than bypassing doctor/version checks.

If Tunnel start fails with an already-in-use/bind-style error, do not attempt to kill an unknown process. Surface failed/degraded state and keep the normal recovery schedule.

- [ ] **Step 4: Run TunnelSupervisor tests and Console tunnel integration tests**

Run all TunnelSupervisor tests plus MainWindow tests that cover tunnel controls, Bridge restart tunnel behavior, and exit.

- [ ] **Step 5: Commit Task 3**

Commit message: `feat: automate tunnel recovery`.

---

### Task 4: Aggregate status, Retry now, Restart CodexBridge, and Advanced controls

**Files:**
- Modify: `src/codex_bridge/console/main_window.py`
- Test: `tests/test_console_main_window.py`

**Interfaces:**
- Consume Task 1 `_retry_now()` Bridge behavior and Task 3 `TunnelSupervisor.retry_now()`.
- Reuse `_restart_bridge`, `_sync_overall_status`, `_apply_tunnel_state`, `_show_status`, `_refresh_status`, existing bridge/tunnel action buttons.

- [ ] **Step 1: Write failing UI/status tests**

Add tests for these contracts:

```python
assert overall_is_starting_during_managed_start_or_recovery()
assert overall_is_ready_only_when_bridge_app_server_and_tunnel_are_usable()
assert tunnel_failure_with_local_bridge_ready_is_degraded()
assert usage_failure_does_not_change_ready_to_degraded()
assert update_check_failure_does_not_change_ready_to_degraded()
assert retry_now_routes_to_bridge_when_bridge_unavailable()
assert retry_now_routes_to_tunnel_when_bridge_ready_but_tunnel_unavailable()
assert restart_codexbridge_uses_existing_managed_restart_path()
assert restart_codexbridge_is_disabled_for_ready_external_bridge()
assert advanced_controls_are_hidden_by_default_and_preserve_existing_individual_actions()
```

- [ ] **Step 2: Run new UI/status tests and verify failure**

Run only the new node ids.

- [ ] **Step 3: Implement four-state aggregate status**

Store the latest Tunnel state in MainWindow and map aggregate state to exactly `Starting`, `Ready`, `Degraded`, or `Error`. Bridge/App Server startup/recovery drives Starting/Error; Tunnel unavailable/failed with local Bridge/App Server ready drives Degraded; Usage/update status is excluded from aggregate degradation. Preserve the compact main header with Overall, Codex Usage, optional update notice, and Status.

- [ ] **Step 4: Implement normal Status actions and Advanced container**

Add `Retry now` and `Restart CodexBridge` outside Advanced. `Retry now` is disabled while a start/restart request is already actively in flight; otherwise it immediately retries the currently blocking managed layer. `Restart CodexBridge` delegates to the existing managed Bridge restart path and relies on Tunnel autostart after recovery. While `_runtime_state == "external"` and the external Bridge is Ready, disable this button and show a tooltip explaining that the Bridge is externally managed.

Move the existing individual Start/Stop/Restart Bridge and Start/Stop/Restart Tunnel controls into one Advanced container hidden by default. The Advanced toggle changes only visibility; action semantics remain unchanged.

- [ ] **Step 5: Run MainWindow tests**

Run all `tests/test_console_main_window.py` tests in a Windows-safe manner; if combined Qt teardown crashes after assertions, also run affected groups/files separately and report both results without calling the crashing aggregate run a pass.

- [ ] **Step 6: Commit Task 4**

Commit message: `feat: simplify console lifecycle controls`.

---

### Task 5: Preserve tray/exit semantics and perform regression verification

**Files:**
- Modify only if a regression requires it: `src/codex_bridge/console/main_window.py`
- Test: `tests/test_console_main_window.py`
- Test: existing RuntimeLauncher/TunnelSupervisor tests

**Interfaces:**
- Reuse `closeEvent`, `_begin_exit`, `_on_exit_tunnel_stopped`, `_finish_exit`, RuntimeLauncher ownership/control-token behavior.

- [ ] **Step 1: Add or strengthen exit regression tests**

Ensure tests prove:

```python
assert window_close_with_tray_hides_without_stopping_runtime()
assert tray_exit_stops_console_owned_tunnel()
assert tray_exit_requests_shutdown_only_for_console_owned_bridge()
assert tray_exit_never_shuts_down_ready_external_bridge()
assert pending_bridge_and_tunnel_recovery_timers_are_cancelled_on_exit()
```

- [ ] **Step 2: Run the exit tests before any code change**

If they already pass, do not modify exit implementation merely for cleanup.

- [ ] **Step 3: Apply only necessary regression fixes**

No change is required if existing shutdown behavior already satisfies the tests. Any fix must preserve the current bounded graceful shutdown/watchdog behavior and must not add PID kill/taskkill for Bridge.

- [ ] **Step 4: Run focused and static verification**

Run, in order:

```text
pytest -q <affected MainWindow node groups>
pytest -q <TunnelSupervisor test file(s)>
pytest -q <RuntimeLauncher/exit-related test file(s)>
ruff check .
ruff format --check .
mypy src
python -m compileall -q src

git diff --check
```

Then attempt the repository's full pytest suite once. If Windows sandbox/TEMP/named-pipe/Qt teardown issues prevent a clean aggregate result, do not report success; record the exact failure and rely only on separately successful focused suites as evidence.

- [ ] **Step 5: Real Console dogfood**

If launching the real Console is possible in the current environment without disrupting user-owned external processes, verify:

```text
Console launch -> existing Bridge attach OR automatic managed Bridge start
Bridge Ready -> Tunnel automatic start
main header -> Ready + Usage
window X -> tray hide with runtime still alive
Status -> Advanced hidden initially
Status -> Retry/Restart controls consistent with ownership
Tray Exit -> owned components shut down; external Bridge untouched
```

If real dogfood cannot safely be run, explicitly report it as not run and why.

- [ ] **Step 6: Final commit/push**

If Task 5 required code/test changes, commit them with message `test: harden managed lifecycle regressions`. Push all task commits normally to the existing upstream branch. Do not merge, force-push, rebase public history, tag, release, or deploy.

## Final Report
Report:
- starting and ending branch/HEAD
- exact files changed
- managed lifecycle behavior implemented
- focused pytest results
- full pytest result separately
- Ruff/mypy/compileall/diff-check results
- dogfood result or explicit reason not run
- commit SHAs and push destination
- final working-tree state
- any remaining limitation, especially external Tunnel discovery/status ambiguity
