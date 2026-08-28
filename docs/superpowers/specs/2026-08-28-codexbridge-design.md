# CodexBridge MCP Design

**Status:** Approved for implementation

**Goal:** Provide a thin single-user Remote MCP bridge from ChatGPT to one long-lived local `codex app-server` process, preserving Codex's native thread identity and delegating coding, shell, Git, sandbox, and approval policy to Codex.

## Scope and constraints

- Python 3.11+.
- Use the current official MCP Python SDK v2 and Streamable HTTP at `/mcp`.
- Bind HTTP to localhost by default; host and port are configurable by environment variables.
- Start exactly one `codex app-server --stdio` subprocess for the bridge lifetime.
- Do not create a bridge session ID, database, job queue, scheduler, GUI, rollback, worktree generator, or dedicated shell/file/Git tools. Phase 2 adds only the explicitly specified process-local Activity subscriber queues and read-only HTTP API.
- Return Codex's native `thread.id` unchanged as `thread_id`.
- Keep prompts, credentials, API keys, tokens, tunnel identifiers, and complete raw event payloads out of normal logs and bounded status responses.
- Require `CODEX_BRIDGE_ALLOWED_ROOTS`; an unset or empty allowlist rejects every `cwd`.

## External MCP interface

The original bridge surface exposes eight control tools. Phase 1 adds the ninth, read-only
`codex_status` tool described in the Activity observation addendum below:

| Tool | Input | Behavior |
| --- | --- | --- |
| `codex_start` | `cwd`, `prompt` | Validate cwd, call `thread/start`, call `turn/start`, and return immediately with native thread/turn IDs and `in_progress`. |
| `codex_continue` | `thread_id`, `prompt` | Resume an unloaded native thread, then call `turn/start`; return immediately. |
| `codex_wait` | `thread_id`, `turn_id`, optional bounded timeout | Wait for terminal state, pending approval, pending user input, App Server error, or timeout. Return normalized state, latest safe agent message, current diff, pending request, and error. |
| `codex_steer` | `thread_id`, `turn_id`, `prompt` | Call `turn/steer` with `expectedTurnId` equal to the supplied turn ID. |
| `codex_approval` | `request_id`, `decision` | Resolve one pending command/file/permission approval using the exact schema enum: `accept`, `acceptForSession`, `decline`, or `cancel`. No passthrough payload. |
| `codex_user_input` | `request_id`, `answers` | Resolve exactly one pending user-input request. Answers are keyed by the request's question IDs and contain string arrays. |
| `codex_interrupt` | `thread_id`, `turn_id` | Call `turn/interrupt`; only a later terminal event determines final state. |
| `codex_threads` | optional `thread_id`, history flag, bounded list pagination | Call `thread/list` for discovery or `thread/read` for a selected native thread and sanitize output. |
| `codex_status` | `thread_id`, optional `turn_id`, `activity_limit` 1-100 | Return the current safe snapshot and bounded recent normalized activities without a second App Server writer. |

Approval and user-input requests are held in memory and surfaced through `codex_wait`. A response is accepted only when its request ID is still pending; duplicate, unknown, or mismatched requests fail without affecting another request.

## Architecture and data flow

```text
ChatGPT MCP client
        |
        v
MCP Streamable HTTP /mcp
        |
        v
MCP tool handlers -> Bridge orchestration -> State store
                                      |
                                      v
                         AppServerClient / JSON-RPC transport
                                      |
                                      v
                         codex app-server --stdio
```

`JsonRpcTransport` owns line-delimited JSON-RPC framing, request ID correlation, one reader task, response futures, notification dispatch, and server-request dispatch. `AppServerClient` owns subprocess startup, `initialize`/`initialized`, native method calls, stderr diagnostics, and shutdown. `StateStore` owns bounded in-memory thread/turn snapshots and wait conditions. `ActivityStore` owns a separate process-local ring buffer of safe normalized observations. `Bridge` translates native events and calls into the MCP-facing behaviors. The server module owns SDK registration and ASGI lifespan.

The reader recognizes:

- responses for bridge-originated request IDs;
- notifications such as `turn/started`, `turn/completed`, `turn/diff/updated`, and `item/agentMessage/delta`;
- server requests `item/commandExecution/requestApproval`, `item/fileChange/requestApproval`, `item/permissions/requestApproval`, and `item/tool/requestUserInput`.

Terminal normalization maps native turn status as follows: `completed` → `completed`, `interrupted` → `interrupted`, `failed` → `failed`, and `inProgress` → `in_progress`. A pending approval or input takes precedence while the turn remains active. Process exit or protocol failure produces `failed` with bounded diagnostic text.

## Host and path security

`CODEX_BRIDGE_HOST` defaults to `127.0.0.1` and `CODEX_BRIDGE_PORT` defaults to `8000`. `CODEX_BRIDGE_ALLOWED_HOSTS` and optional `CODEX_BRIDGE_ALLOWED_ORIGINS` are parsed as comma-separated exact allowlists. When unset, the SDK's localhost-safe transport security defaults are retained; configured tunnel hostnames/origins are runtime values only and never repository data. CORS configuration is kept separate from Host/DNS-rebinding protection.

`CODEX_BRIDGE_ALLOWED_ROOTS` is a platform-separated list of existing directories. Every requested cwd must be absolute, existing, a directory, canonicalized with symlinks/junctions resolved, and contained by a canonical allowed root using Windows case-insensitive comparison where applicable. Relative paths, traversal outside a root, sibling-prefix collisions, and canonical escapes are rejected.

## Lifecycle and shutdown

ASGI lifespan startup creates one App Server client and performs the handshake before serving tools. On SIGINT/SIGTERM or lifespan exit, active turns receive best-effort `turn/interrupt`; the bridge waits only a short grace period, then closes pipes and terminates the child. Hard-kill and crash recovery are out of scope. Native rollout/history files are never edited. After restart, `codex_continue` calls native `thread/resume` and reports resume errors without auto-repair.

## Testing and verification

Unit tests use a fake App Server or transport double and never invoke real Codex. They cover handshake, request correlation, notifications, server-request routing, all nine tools, state normalization, Activity bounds/privacy, concurrent threads, cross-talk prevention, lifecycle failures, path security, and graceful shutdown. A separate explicit integration smoke script uses a temporary workspace and a real local Codex installation; it is never part of the normal unit suite.

## Phase 1 Activity observation addendum

`ActivityStore` keeps at most 500 `Activity` records per native thread in memory. Each record has a process-unique ID, UTC ISO-8601 timestamp, native thread/turn identity, optional item identity, normalized type/status, bounded summary, and a small allowlisted details mapping. It records turn start/completion/failure/interruption, command start/completion, file-change start/completion, completed agent messages, approval and user-input request/resolution, and bounded errors. Agent-message deltas update `StateStore.latest_agent_message` and do not create Activity records. Reasoning items, raw/encrypted fields, raw JSON-RPC payloads, command output, full diffs, credentials, API keys, authentication tokens, and tunnel identifiers are not stored. File display paths are reduced to allowed-root-relative paths and outside-root paths are omitted.

`codex_status` validates `activity_limit` between 1 and 100 (default 20), selects the supplied turn, the active turn, or the latest known turn, and returns only the existing safe state snapshot plus serialized Activity records. It returns `state: not_loaded` with no activities when no matching turn is known and never fabricates a turn.

## Phase 2 read-only history and localhost UI API addendum

Phase 2 keeps the MCP listener and UI listener as separate ASGI applications. MCP remains at `config.host:config.port` (default `127.0.0.1:8000`); the UI listener binds in code to `127.0.0.1:config.ui_port` (default `8001`). `CODEX_BRIDGE_UI_PORT` is the only new setting, must be between 1 and 65535, and must differ from the MCP port. No UI host setting exists. The Tunnel target remains the MCP listener only.

`Bridge.read_thread_turns()` and `Bridge.read_thread_items()` first call `thread/read` with `includeTurns=false`, validate the returned thread ID and cwd through the existing allowed-root policy, and only then perform history reads. `historyMode: paginated` uses `thread/turns/list` with `itemsView: notLoaded` or `thread/items/list` with exact native cursor/limit/sort fields. Missing or `legacy` mode uses bounded `thread/read(includeTurns=true)` fallback; legacy cursors are rejected rather than synthesized. Public history uses snake-case bounded fields and null cursors plus an explicit truncation flag.

`src/codex_bridge/history.py` owns strict public projection. It copies only known safe fields for user messages, agent messages, plans, command executions, file changes, MCP calls, dynamic calls, function-call outputs, collaboration summaries, sub-agent activity, image views, compaction, and review-mode markers. Reasoning, hook prompts, unknown future items, command output, full diffs, tool arguments/results, prompts, raw payloads, and unsafe absolute paths are omitted. User text and agent/plan/command/error text are bounded; user image inputs become a generic marker.

`src/codex_bridge/ui_api.py` exposes only GET `/healthz`, `/ui-api/status`, `/ui-api/threads`, `/ui-api/threads/{thread_id}`, `/ui-api/threads/{thread_id}/turns`, `/ui-api/threads/{thread_id}/items`, `/ui-api/threads/{thread_id}/status`, and `/ui-api/events`. The independent app uses Starlette `TrustedHostMiddleware` for `127.0.0.1` and `localhost`, has no CORS middleware, and maps invalid requests, invisible threads, unavailable App Server, and upstream failures to fixed safe HTTP responses. `src/codex_bridge/ui_server.py` owns bounded Uvicorn start/shutdown; runtime startup cleans up the App Server if UI startup fails.

`ActivityStore` remains a process-local 500-record-per-thread ring buffer. Its optional subscriber queues are bounded to 100 records and publish non-blockingly, dropping the oldest queued record when full. `/ui-api/events` emits only new `Activity.to_dict()` values as SSE (`event: activity`, Activity ID, safe JSON) with optional `thread_id` filtering and a keepalive comment; there is no `Last-Event-ID` replay or SQLite persistence. UI observation never resumes threads or starts, steers, interrupts, approves, or otherwise writes to the App Server.

## Phase 3 desktop Console addendum

Phase 3 adds an optional Windows PySide6 read-only desktop viewer without changing the Phase 2 backend or the nine-tool MCP surface. `codex-bridge-console` uses a console-only configuration with fixed host `127.0.0.1`, port precedence `--ui-port` then `CODEX_BRIDGE_UI_PORT` then `8001`, and validation in the inclusive range `1..65535`. PySide6 is an optional `console` extra (`PySide6>=6.8,<7`) and is not imported by the Bridge server or by the console entrypoint until GUI startup.

The Console owns one Qt `QNetworkAccessManager` and performs only asynchronous GET requests to the existing `/healthz`, `/ui-api/status`, thread, history, status, and `/ui-api/events` endpoints. JSON replies are handled by `finished`; SSE is consumed by `readyRead` through a pure incremental UTF-8 parser with a 256 KiB pending-buffer limit. SSE Activity payloads must be JSON objects with the required safe Activity fields; malformed, unknown, or oversized data is discarded or terminates the stream without exposing raw payloads.

The window has a resizable left thread list, center old-to-new persisted history timeline, right bounded Activity/status pane, top Bridge/App Server/Stream state, and bottom bounded error/status text. Thread selection aborts the old stream, fetches the four selected-thread snapshots, then opens the new stream. Selection generations and stream reply identity prevent stale responses/events from updating a newer thread. Activity rows are deduplicated by `activity_id` and bounded to 200; reconnect waits a fixed 1.5 seconds and re-synchronizes selected-thread status before opening a new stream.

In the Phase 3 viewer, Console close stopped its timers and aborted its own replies only. Phase 4A adds a detached Bridge start path but retains the same close contract: the Console never stops Bridge, App Server, Tunnel, or Codex turns, and never implements approval/input mutation, resume, start, steer, interrupt, SQLite persistence, replay, WebSocket, browser UI, or installer behavior. The Console is loopback-only and is not a Tunnel target.

## Phase 4A Codex detection and detached Bridge launcher addendum

Phase 4A extends the optional Windows PySide6 Console with Codex executable discovery, bounded asynchronous `--version` verification, Bridge runtime state, and a `Start Bridge` button. Candidate priority is explicit `CODEX_BRIDGE_CODEX_EXECUTABLE`, Windows Codex App native paths under `%LOCALAPPDATA%\\OpenAI\\Codex\\bin\\*\\codex.exe`, PATH (`codex.exe`, `codex.cmd`, `codex`), then `%APPDATA%\\npm\\codex.cmd`. Windows canonical paths are compared case-insensitively and candidates are deterministic. An invalid, unlaunchable, or unverifiable explicit override fails closed without fallback. The probe uses Qt asynchronous process signals, a roughly three-second timeout, and a 4 KiB output bound; raw output is not shown or stored.

The Console keeps detected Codex separate from the running Bridge's internal Codex. A ready Bridge observed at startup is `Runtime: external`, and the existing external Bridge is never replaced. Start Bridge is enabled only for an unavailable Bridge with a verified Codex, is disabled immediately on click, and can succeed only once per Console session. The launcher configures an inherited child environment, overriding only `CODEX_BRIDGE_CODEX_EXECUTABLE` and `CODEX_BRIDGE_UI_PORT`, and starts the detached child as `sys.executable -m codex_bridge`; it does not use a console script executable or a shell. The Console treats detached start as `launching` until `/healthz` is OK and `/ui-api/status` reports ready/connected Bridge and App Server state.

The detached Bridge and App Server continue after the Console closes. A failed start or ten-second readiness timeout is shown as a bounded safe status; there is no automatic restart and later polling may recognize a late-ready child without launching another one. Phase 4A provides no Stop or Restart UI and never kills, terminates, interrupts, or shuts down the detached Bridge, App Server, or turns. Tunnel/tray remain out of scope for Phase 4A. The Phase 1–3 backend, nine-tool MCP surface, and `/ui-api/status` schema remain unchanged.
