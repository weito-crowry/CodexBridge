# CodexBridge MCP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the approved thin Remote MCP bridge from ChatGPT to one long-lived local `codex app-server` process.

**Architecture:** A Python package separates configuration, canonical path validation, typed state, line-delimited JSON-RPC, App Server lifecycle, bridge orchestration, and MCP registration. The ASGI lifespan starts one App Server child and the MCP SDK v2 exposes Streamable HTTP at `/mcp`; all task identity remains the native Codex `thread.id`.

**Tech Stack:** Python 3.11+, official `mcp` Python SDK v2, Starlette/uvicorn through the SDK runtime, asyncio, pytest/pytest-asyncio, Ruff, and mypy.

**Spec:** `docs/superpowers/specs/2026-08-28-codexbridge-design.md`

## Global Constraints

- Use the local Codex CLI protocol observed from `codex-cli 0.137.0`; do not invent method names or approval enums.
- Use the current official MCP Python SDK v2 and its `TransportSecuritySettings` for Host/DNS-rebinding protection.
- Use `MCPServer.streamable_http_app()` and expose `/mcp`; keep allowed hosts/origins separate from CORS.
- Start exactly one fixed-argument `codex app-server --stdio` subprocess per bridge lifespan with `asyncio.create_subprocess_exec`.
- Do not add bridge session IDs, SQLite, multi-user state, queues, schedulers, rollback, arbitrary shell/file/Git tools, or automatic approval.
- `CODEX_BRIDGE_ALLOWED_ROOTS` is required; empty/unset means every cwd is rejected.
- Never log prompt text, credentials, API keys, tokens, tunnel identifiers, complete environment data, or raw chain-of-thought.
- Normal unit tests never invoke real Codex; real App Server verification is an explicit temporary-workspace smoke test.

---

### Task 1: Repository scaffold and configuration contract

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `.env.example`
- Create: `src/codex_bridge/__init__.py`
- Create: `src/codex_bridge/config.py`
- Create: `tests/test_config.py`
- Create: `tests/conftest.py`

**Interfaces:**
- Produces `BridgeConfig.from_env() -> BridgeConfig`, with `host`, `port`, `allowed_roots`, `allowed_hosts`, `allowed_origins`, `codex_executable`, `wait_default_seconds`, `wait_max_seconds`, and `shutdown_grace_seconds`.
- `allowed_roots` is parsed with `os.pathsep`; host/origin lists are comma-separated and trimmed.
- `pyproject.toml` declares `mcp>=2,<3`, `uvicorn>=0.30`, Python `>=3.11`, and dev dependencies for pytest, pytest-asyncio, Ruff, and mypy.

- [ ] **Step 1: Write the failing configuration tests**

```python
def test_empty_allowed_roots_fail_closed(monkeypatch):
    monkeypatch.delenv("CODEX_BRIDGE_ALLOWED_ROOTS", raising=False)
    config = BridgeConfig.from_env()
    assert config.allowed_roots == ()


def test_config_parses_host_origin_and_bounded_wait(monkeypatch):
    monkeypatch.setenv("CODEX_BRIDGE_ALLOWED_ROOTS", r"C:\work;D:\repo")
    monkeypatch.setenv("CODEX_BRIDGE_ALLOWED_HOSTS", "bridge.example.com, bridge.example.com:*")
    monkeypatch.setenv("CODEX_BRIDGE_ALLOWED_ORIGINS", "https://chat.example.com")
    monkeypatch.setenv("CODEX_BRIDGE_WAIT_MAX_SECONDS", "29")
    config = BridgeConfig.from_env()
    assert config.allowed_hosts == ("bridge.example.com", "bridge.example.com:*")
    assert config.allowed_origins == ("https://chat.example.com",)
    assert config.wait_max_seconds == 29.0
```

- [ ] **Step 2: Run the focused tests and verify the expected missing-module failure**

Run: `python -m pytest tests/test_config.py -q`

Expected: FAIL because `codex_bridge.config` and `BridgeConfig` do not exist.

- [ ] **Step 3: Add the minimal package metadata and configuration implementation**

Implement `BridgeConfig` as a frozen dataclass. Defaults are `127.0.0.1`, `8000`, `codex`, `15.0`, `30.0`, and `3.0`. Reject malformed numeric values and clamp/reject a default wait above the maximum. Do not load `.env` automatically. `.env.example` contains names with dummy values only.

- [ ] **Step 4: Run the focused tests and verify they pass**

Run: `python -m pytest tests/test_config.py -q`

Expected: PASS with all configuration tests green.

- [ ] **Step 5: Commit the scaffold**

```powershell
git add pyproject.toml .gitignore .env.example src tests
git commit -m "feat: add CodexBridge configuration scaffold"
```

### Task 2: Canonical cwd allowlist and typed state model

**Files:**
- Create: `src/codex_bridge/models.py`
- Create: `src/codex_bridge/paths.py`
- Create: `src/codex_bridge/state.py`
- Create: `tests/test_paths.py`
- Create: `tests/test_state.py`

**Interfaces:**
- Produces `AllowedPathPolicy(allowed_roots: tuple[str, ...])` and `AllowedPathPolicy.validate_cwd(cwd: str) -> str`.
- Produces `ThreadState`, `TurnState`, `PendingRequest`, and `StateStore`.
- `StateStore.wait_for_change(thread_id, turn_id, timeout) -> bool` waits on an `asyncio.Condition`; it never returns another thread's event.
- Public normalized states are `in_progress`, `needs_approval`, `needs_input`, `completed`, `interrupted`, and `failed`.

- [ ] **Step 1: Write failing path and state tests**

```python
def test_relative_cwd_is_rejected(tmp_path):
    with pytest.raises(PathPolicyError):
        AllowedPathPolicy((str(tmp_path),)).validate_cwd(".")


def test_sibling_prefix_is_rejected(tmp_path):
    allowed = tmp_path / "repo"
    sibling = tmp_path / "repo-other"
    allowed.mkdir()
    sibling.mkdir()
    with pytest.raises(PathPolicyError):
        AllowedPathPolicy((str(allowed),)).validate_cwd(str(sibling))


@pytest.mark.asyncio
async def test_wait_for_change_does_not_cross_talk():
    store = StateStore()
    store.ensure_turn("thread-a", "turn-a")
    store.ensure_turn("thread-b", "turn-b")
    waiter = asyncio.create_task(store.wait_for_change("thread-a", "turn-a", 0.05))
    store.update_latest_message("thread-b", "turn-b", "other")
    assert await waiter is False
```

- [ ] **Step 2: Run tests and verify the expected failures**

Run: `python -m pytest tests/test_paths.py tests/test_state.py -q`

Expected: FAIL because path policy and state store are not implemented.

- [ ] **Step 3: Implement canonical path validation and state storage**

Resolve allowed roots and requested cwd with `Path.resolve(strict=True)`, require existing directories, compare canonical paths with `os.path.commonpath`, and use `os.path.normcase` on Windows. Store only bounded latest message/diff, terminal state, error, active turn, pending requests, and a small recent-event deque.

- [ ] **Step 4: Run focused tests and verify green**

Run: `python -m pytest tests/test_paths.py tests/test_state.py -q`

Expected: PASS, including a Windows symlink/junction escape test when the platform permits creating one; otherwise the test records a platform skip without weakening the normal canonical-resolution assertion.

- [ ] **Step 5: Commit the boundary model**

```powershell
git add src/codex_bridge/models.py src/codex_bridge/paths.py src/codex_bridge/state.py tests/test_paths.py tests/test_state.py
git commit -m "feat: add cwd policy and in-memory turn state"
```

### Task 3: JSON-RPC transport and App Server lifecycle

**Files:**
- Create: `src/codex_bridge/jsonrpc.py`
- Create: `src/codex_bridge/app_server.py`
- Create: `tests/test_jsonrpc.py`
- Create: `tests/test_app_server.py`
- Create: `tests/fakes.py`

**Interfaces:**
- Produces `JsonRpcTransport.send_request(method: str, params: dict[str, object]) -> dict[str, object]`.
- Produces `AppServerClient.start()`, `request()`, `respond()`, and `shutdown()` async methods.
- `AppServerClient.start()` runs `codex app-server --stdio`, sends `initialize`, waits for its response, then sends `initialized` notification.
- A reader routes response IDs to futures, notifications to a callback, and server requests to a callback. Protocol errors fail pending requests and transition the client to failed.

- [ ] **Step 1: Write failing correlation, routing, handshake, abnormal-exit, and shutdown tests**

```python
@pytest.mark.asyncio
async def test_jsonrpc_correlates_out_of_order_responses(fake_stream):
    transport = JsonRpcTransport(fake_stream.reader, fake_stream.writer)
    task = asyncio.create_task(transport.run())
    first = asyncio.create_task(transport.send_request("one", {}))
    second = asyncio.create_task(transport.send_request("two", {}))
    ids = await fake_stream.read_request_ids(2)
    await fake_stream.send_response(ids[1], {"value": 2})
    await fake_stream.send_response(ids[0], {"value": 1})
    assert await first == {"value": 1}
    assert await second == {"value": 2}
    await transport.close()
    await task


@pytest.mark.asyncio
async def test_app_server_sends_initialize_then_initialized(fake_process):
    client = AppServerClient(process_factory=fake_process.factory)
    await client.start()
    assert fake_process.methods == ["initialize", "initialized"]
    await client.shutdown()
```

- [ ] **Step 2: Run tests and verify missing implementation failures**

Run: `python -m pytest tests/test_jsonrpc.py tests/test_app_server.py -q`

Expected: FAIL because transport and client do not exist.

- [ ] **Step 3: Implement line-delimited JSON-RPC and fixed subprocess startup**

Use monotonically increasing integer request IDs, one reader task over stdout, `json.loads` per non-empty line, `json.dumps` plus newline for writes, and an asyncio lock around writes. Use `asyncio.create_subprocess_exec(config.codex_executable, "app-server", "--stdio", stdin=PIPE, stdout=PIPE, stderr=PIPE)`; never invoke a shell. Consume stderr in a separate diagnostic task and log only bounded non-secret messages.

- [ ] **Step 4: Run focused tests and verify green**

Run: `python -m pytest tests/test_jsonrpc.py tests/test_app_server.py -q`

Expected: PASS with no real `codex` process started.

- [ ] **Step 5: Commit the transport**

```powershell
git add src/codex_bridge/jsonrpc.py src/codex_bridge/app_server.py tests/test_jsonrpc.py tests/test_app_server.py tests/fakes.py
git commit -m "feat: add App Server JSON-RPC client"
```

### Task 4: Native RPC bridge and event normalization

**Files:**
- Create: `src/codex_bridge/bridge.py`
- Modify: `src/codex_bridge/models.py`
- Create: `tests/test_bridge.py`

**Interfaces:**
- `Bridge.start(cwd: str, prompt: str) -> dict[str, object]`.
- `Bridge.continue_thread(thread_id: str, prompt: str) -> dict[str, object]`.
- `Bridge.wait(thread_id: str, turn_id: str, timeout_seconds: float | None) -> dict[str, object]`.
- `Bridge.steer(thread_id: str, turn_id: str, prompt: str) -> dict[str, object]`.
- `Bridge.approve(request_id: str, decision: ApprovalDecision) -> dict[str, object]`.
- `Bridge.answer_user_input(request_id: str, answers: dict[str, UserInputAnswer]) -> dict[str, object]`.
- `Bridge.interrupt(thread_id: str, turn_id: str) -> dict[str, object]`.
- `Bridge.threads(thread_id: str | None, include_history: bool, limit: int, cursor: str | None) -> dict[str, object]`.

- [ ] **Step 1: Write failing native-RPC and normalization tests**

```python
@pytest.mark.asyncio
async def test_start_returns_native_ids_without_waiting_for_completion(fake_app):
    bridge = Bridge(fake_app, StateStore(), path_policy)
    result = await bridge.start(str(allowed_dir), "inspect this")
    assert result["thread_id"] == "native-thread"
    assert result["turn_id"] == "native-turn"
    assert result["state"] == "in_progress"
    assert fake_app.methods == ["thread/start", "turn/start"]


@pytest.mark.asyncio
async def test_steer_uses_expected_turn_id(fake_app):
    bridge = Bridge(fake_app, StateStore(), path_policy)
    await bridge.steer("thread", "turn", "change direction")
    assert fake_app.last_params["expectedTurnId"] == "turn"


@pytest.mark.asyncio
async def test_terminal_events_normalize_completed_interrupted_and_failed(fake_app):
    store = StateStore()
    bridge = Bridge(fake_app, store, path_policy)
    for native, expected in (
        ("completed", "completed"),
        ("interrupted", "interrupted"),
        ("failed", "failed"),
    ):
        bridge.handle_notification(
            {
                "method": "turn/completed",
                "params": {"threadId": "t", "turn": {"id": "u", "status": native}},
            }
        )
        assert store.snapshot("t", "u")["state"] == expected
```

- [ ] **Step 2: Run the focused tests and verify missing bridge failures**

Run: `python -m pytest tests/test_bridge.py -q`

Expected: FAIL because the bridge and event normalizer do not exist.

- [ ] **Step 3: Implement native calls and event handling**

Call `thread/start` with canonical cwd, then `turn/start` with `input=[{"type":"text","text":prompt}]`. Resume only when a thread is not in the current loaded set. Use `turn/steer` with `expectedTurnId`, `turn/interrupt` without treating its response as terminal, `thread/list`, and `thread/read`. Handle server requests by storing exact request IDs and method-specific metadata. Approval decisions are the closed set `accept`, `acceptForSession`, `decline`, `cancel`; user-input answers must match pending question IDs and serialize as `{question_id: {"answers": ["..."]}}`.

- [ ] **Step 4: Add notification-driven waits and bounded sanitization**

Aggregate only agent-message delta text, the latest diff, terminal status, safe command/file summaries, and bounded errors. Resolve `codex_wait` immediately for terminal state, pending approval, pending user input, process failure, or timeout. Exclude raw reasoning items and cap returned text; never return the complete event history.

- [ ] **Step 5: Run focused bridge tests and verify green**

Run: `python -m pytest tests/test_bridge.py -q`

Expected: PASS with coverage for start, resume, turn start, completed/interrupted/failed, steer, approval, user input, interrupt, and event cross-talk.

- [ ] **Step 6: Commit the bridge**

```powershell
git add src/codex_bridge/bridge.py src/codex_bridge/models.py tests/test_bridge.py
git commit -m "feat: bridge native Codex threads and turns"
```

### Task 5: MCP server, Streamable HTTP, and lifecycle integration

**Files:**
- Create: `src/codex_bridge/server.py`
- Create: `src/codex_bridge/__main__.py`
- Create: `tests/test_server.py`

**Interfaces:**
- `create_app(config: BridgeConfig, bridge_factory: Callable[[], Bridge]) -> Starlette`.
- Register exactly `codex_start`, `codex_continue`, `codex_wait`, `codex_steer`, `codex_approval`, `codex_user_input`, `codex_interrupt`, and `codex_threads`.
- The app uses `TransportSecuritySettings(allowed_hosts=..., allowed_origins=...)` only when configured; otherwise the SDK's localhost-safe defaults remain active.

- [ ] **Step 1: Write failing server registration, lifespan, and host-security tests**

```python
def test_server_registers_exactly_eight_tools(app):
    names = {tool.name for tool in app_mcp_tools(app)}
    assert names == {
        "codex_start",
        "codex_continue",
        "codex_wait",
        "codex_steer",
        "codex_approval",
        "codex_user_input",
        "codex_interrupt",
        "codex_threads",
    }


@pytest.mark.asyncio
async def test_lifespan_starts_and_shutdowns_one_bridge(fake_bridge):
    async with app_lifespan(fake_bridge) as bridge:
        assert bridge.start_count == 1
    assert bridge.shutdown_count == 1
```

- [ ] **Step 2: Run server tests and verify expected failures**

Run: `python -m pytest tests/test_server.py -q`

Expected: FAIL because the MCP server module and app factory do not exist.

- [ ] **Step 3: Implement typed MCP tool handlers and custom ASGI lifespan**

Construct one `MCPServer`, register the eight tools, call `mcp.streamable_http_app(transport_security=security)`, and wrap it in a Starlette app with a lifespan that enters `mcp.session_manager.run()`, starts one Bridge/App Server client, yields, interrupts active turns on exit, and shuts down within the configured grace period. Use `uvicorn.run(app, host=config.host, port=config.port)` in `__main__.py`.

- [ ] **Step 4: Verify host/origin settings are separate and bounded**

Pass exact configured host entries such as `bridge.example.com` and `bridge.example.com:*` to `allowed_hosts`; pass only configured origins to `allowed_origins`. Do not add generic CORS middleware or infer a tunnel hostname from environment values outside the explicit settings.

- [ ] **Step 5: Run focused tests and verify green**

Run: `python -m pytest tests/test_server.py -q`

Expected: PASS with exactly eight tool registrations and one lifecycle-owned Bridge.

- [ ] **Step 6: Commit the MCP surface**

```powershell
git add src/codex_bridge/server.py src/codex_bridge/__main__.py tests/test_server.py
git commit -m "feat: expose CodexBridge Streamable HTTP tools"
```

### Task 6: README, explicit integration smoke, and operational logging

**Files:**
- Create: `README.md`
- Create: `scripts/integration_smoke.py`
- Create: `tests/test_logging.py`

**Interfaces:**
- `scripts/integration_smoke.py` is opt-in and exits nonzero on a failed real check.
- README documents the observed Codex version, App Server protocol findings, rejected `codex mcp-server` Spike, endpoint, tunnel Host/origin setup, eight tools, setup, environment variables, allowlist, workflow, resume, steer, approval, shutdown, limitations, unit tests, and smoke command.

- [ ] **Step 1: Write failing log redaction tests and smoke harness tests**

```python
def test_log_record_does_not_include_prompt_or_secret(caplog):
    log_thread_started(thread_id="native-thread", prompt="do not log this", token="secret")
    assert "do not log this" not in caplog.text
    assert "secret" not in caplog.text


def test_smoke_requires_explicit_opt_in():
    result = subprocess.run([sys.executable, "scripts/integration_smoke.py", "--help"], check=False)
    assert result.returncode == 0
```

- [ ] **Step 2: Run focused tests and verify expected failures**

Run: `python -m pytest tests/test_logging.py -q`

Expected: FAIL because logging helpers and smoke script do not exist.

- [ ] **Step 3: Implement safe operational logging and real smoke flow**

Log startup/shutdown, Codex version, App Server lifecycle, thread start/resume, turn start/terminal state, approval request/resolution, user-input request/resolution, protocol errors, and abnormal child exit. The smoke script creates a temporary directory under the OS temp directory, runs a trivial file-writing task through the bridge or App Server client, verifies completion and continuation, shuts down/restarts, resumes by native thread ID, and attempts a short running-turn steer. It never targets a repository checkout and never prints credentials or complete prompts.

- [ ] **Step 4: Write README from the implemented contract**

Record `/mcp`, default `127.0.0.1:8000`, `CODEX_BRIDGE_ALLOWED_HOSTS`, `CODEX_BRIDGE_ALLOWED_ORIGINS`, `CODEX_BRIDGE_ALLOWED_ROOTS`, the need for tunnel hostname allowlisting, localhost defaults, the exact eight tools, `codex-cli 0.137.0`, and known limitations including no bridge persistence, no authentication database, no hard-kill recovery, and approval-realism limits in smoke tests.

- [ ] **Step 5: Run focused docs/log tests and verify green**

Run: `python -m pytest tests/test_logging.py -q`

Expected: PASS; the normal suite still does not start real Codex.

- [ ] **Step 6: Commit docs and smoke harness**

```powershell
git add README.md scripts/integration_smoke.py tests/test_logging.py
git commit -m "docs: document CodexBridge operations and smoke test"
```

### Task 7: Full verification, final integration check, and push

**Files:**
- Modify only files identified by failed checks; do not include generated schema bundles, temporary workspaces, `.env`, or authentication files.

- [ ] **Step 1: Run the complete unit suite**

Run: `python -m pytest -q`

Expected: all unit tests pass and no real Codex process is started.

- [ ] **Step 2: Run lint, format, type, and compile checks**

Run: `python -m ruff check .`; `python -m ruff format --check .`; `python -m mypy src`; `python -m compileall -q src tests`

Expected: all commands exit 0 with no warnings that indicate a correctness issue.

- [ ] **Step 3: Run the explicit real smoke test when local credentials are available**

Run: `python scripts/integration_smoke.py`

Expected: the report identifies each attempted lifecycle step, exact completed/failed outcome, and any approval or steer scenario that was not safely reproducible. A smoke failure is reported honestly and is not retried blindly.

- [ ] **Step 4: Inspect repository safety before commit**

Run: `git status --short`; `git diff --check`; `git ls-files | rg '(^|/)(\.env|.*token.*|.*credential.*|.*auth.*)$'`.

Expected: only intended source/docs/tests are present; no secret files are tracked.

- [ ] **Step 5: Push main without force**

```powershell
git push -u origin main
```

Expected: non-force push creates `origin/main`; if GitHub credentials or repository permission is unavailable, report the exact push error without changing remote history.

- [ ] **Step 6: Verify final remote and local commit**

Run: `git rev-parse HEAD`; `git ls-remote origin refs/heads/main`; `git status --short --branch`.

Expected: the remote ref equals the final local SHA and the worktree is clean.

---

## Review Fix Plan for `f108c6c`

**Goal:** Preserve the existing eight-tool bridge while closing the reviewed security, protocol, lifecycle, and process-recovery defects.

**Schema evidence:** Regenerate with `codex app-server generate-json-schema --experimental`; in the installed `codex-cli 0.137.0` schema, `Thread.cwd` is required, `PermissionsRequestApprovalParams` contains `cwd`, `environmentId`, `permissions`, and `reason`, `PermissionsRequestApprovalResponse` requires `permissions` and permits `scope` `turn|session`, `ServerRequestResolvedNotification` carries `requestId` and `threadId`, and `McpServerElicitationRequestResponse` requires action `accept|decline|cancel`.

### Review Task 1: Enforce canonical cwd policy on persisted threads

**Files:** modify `src/codex_bridge/models.py`, `src/codex_bridge/state.py`, `src/codex_bridge/bridge.py`, and `tests/test_bridge.py`.

- Add `ThreadState.validated_cwd` and make `mark_loaded()` require the canonical cwd.
- Before `thread/resume`, call `thread/read` with `includeTurns=false`, validate `thread.cwd` through `AllowedPathPolicy`, and only then resume; store the validated cwd after the native response matches the requested thread.
- For `codex_threads(thread_id=...)`, validate metadata before optionally fetching history; for list responses, validate each native `cwd` and exclude invalid/out-of-root rows while preserving native cursors.
- First add tests proving allowed continue, pre-resume rejection, no resume call, detail/history rejection, list filtering, sibling-prefix/case/symlink enforcement, then run those tests red before implementing.

### Review Task 2: Make permission approvals method-aware and schema-shaped

**Files:** modify `src/codex_bridge/models.py`, `src/codex_bridge/bridge.py`, and `tests/test_bridge.py`.

- Add a bounded typed permission request payload containing requested permissions, cwd, environment id, reason, and safe scope-related data; do not retain arbitrary credential-like fields.
- Keep command/file approvals as `{"decision": ...}`. For permission approvals, map `accept` to `{"permissions": requested, "scope": "turn"}`, `acceptForSession` to the same requested subset with `scope: "session"`, and `decline`/`cancel` to the schema-required empty grant profile with turn scope.
- Add tests asserting exact response dictionaries against the generated 0.137.0 response schema shape and that the public pending request is bounded and credential-free.

### Review Task 3: Reconcile pending requests on resolution and terminal events

**Files:** modify `src/codex_bridge/state.py`, `src/codex_bridge/bridge.py`, and `tests/test_bridge.py`.

- Handle `serverRequest/resolved` by matching both native request id and thread id, removing only that pending request.
- Terminal transitions purge all pending requests for the turn and clear `pending_request_id`; resolving a stale or duplicate request must not move a terminal turn back to `in_progress`.
- Add tests for approval/user-input resolution, duplicate/unknown resolution, pending-plus-completed, pending-plus-interrupt-then-completed, and failed-terminal cleanup.

### Review Task 4: Isolate unsupported server requests and callback failures

**Files:** modify `src/codex_bridge/jsonrpc.py`, `src/codex_bridge/bridge.py`, `src/codex_bridge/app_server.py`, and tests under `tests/`.

- Add a native JSON-RPC error response method. Unsupported server requests receive a bounded `-32601` error and the reader continues; `mcpServer/elicitation/request` receives the schema-valid `{"action": "cancel"}` response without adding a ninth MCP tool.
- Ensure callback exceptions settle all pending client futures; server-request callback failures receive a bounded error response and do not kill the reader loop.
- Add fake-stream tests that send an unsupported request followed by notification/response, verify no active turn is failed, verify no pending future hangs, and validate elicitation cancellation against the generated response shape.

### Review Task 5: Hard-bound App Server shutdown and document the boundary

**Files:** modify `src/codex_bridge/app_server.py`, `src/codex_bridge/server.py`, `tests/fakes.py`, `tests/test_app_server.py`, `tests/test_server.py`, `README.md`, and `scripts/integration_smoke.py` only if needed for the explicit smoke report.

- Extend `ProcessLike` with `kill`; implement `terminate -> bounded wait -> kill -> bounded final wait`, retaining a process reference if an OS-level final wait still times out.
- Give interrupted turns a short terminal-notification grace before closing the protocol, while keeping shutdown bounded.
- Add terminate-respecting and terminate-ignoring fake processes, assert kill fallback, and assert pending cleanup after interruption/terminal events.
- Document that allowed-root filtering applies to start, resume, detail/history, and list; permission responses are method-aware; unsupported requests fail closed with a protocol error or elicitation cancel.

### Review verification and delivery

- Run `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src`, `uv run python -m compileall -q src tests scripts`, and `uv lock --check`.
- Re-run `uv run python scripts/integration_smoke.py` in a temporary workspace and report start, continue, shutdown/restart, resume, steer, and file-change approval separately from unmeasured permission/user-input cases.
- Scan tracked paths for secrets, commit as an additional commit on `main`, push with ordinary `git push`, and verify local/remote SHA equality without amend, rebase, or force push.
