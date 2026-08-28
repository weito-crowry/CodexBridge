# CodexBridge

CodexBridge is a thin single-user bridge for this path:

```text
ChatGPT -> Remote MCP -> CodexBridge -> codex app-server -> Codex
```

It exposes only thread/turn control and approval or user-input forwarding. Shell execution, file changes, Git, sandbox policy, authentication, reasoning, commit, and push remain owned by Codex.

## Architecture

The Streamable HTTP MCP server owns one ASGI lifespan. Startup creates exactly one `codex app-server --stdio` child and performs the JSON-RPC `initialize` / `initialized` handshake. A dedicated JSONL reader correlates response IDs and routes notifications and server-initiated requests to the in-memory state store. MCP tools call a transport-neutral bridge over that client. Phase 2 also starts a separate read-only Starlette/Uvicorn listener for the local UI API; it is not mounted in the MCP app.

The bridge does not create a session ID or database. Codex's native `thread.id` is returned unchanged as `thread_id`; persistence and resume are delegated to Codex rollout/history data.

## Requirements

- Python 3.11+
- A locally authenticated Codex CLI
- `uv` recommended for installation and tests
- A tunnel or reverse proxy prepared separately when ChatGPT must reach the local endpoint

The implementation was developed and protocol-checked against `codex-cli 0.150.0-alpha.8`. The generated protocol bundle was obtained with:

```powershell
codex app-server generate-json-schema --experimental --out <temporary-directory>
```

The real smoke run also observed `Codex Desktop/0.150.0` in the App Server `initialize` `userAgent`; this is recorded as a protocol-reported diagnostic separately from the installed CLI's `codex --version` output.

The observed protocol includes `initialize`, `thread/start`, `thread/resume`, `thread/read`, `thread/list`, `turn/start`, `turn/steer`, `turn/interrupt`, `turn/completed`, agent-message and diff notifications, command/file/permission approval requests, and user-input requests.

## Setup

```powershell
uv sync --extra dev
$env:CODEX_BRIDGE_ALLOWED_ROOTS = 'C:\Users\you\Documents\src\MyRepo'
uv run codex-bridge
```

The MCP endpoint is:

```text
http://127.0.0.1:8000/mcp
```

The Phase 2 UI API endpoint is a separate listener:

```text
http://127.0.0.1:8001/healthz
```

It is fixed to `127.0.0.1`, is not a Tunnel target, and exposes only read-only history/status/activity GET endpoints. The viewer never calls `thread/resume`, `turn/start`, `turn/steer`, or another writer operation.

The default bind is loopback only. The tunnel is not started or configured by CodexBridge, and no tunnel identifier or token belongs in this repository.

## Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `CODEX_BRIDGE_HOST` | `127.0.0.1` | Local bind address. |
| `CODEX_BRIDGE_PORT` | `8000` | Local bind port. |
| `CODEX_BRIDGE_UI_PORT` | `8001` | Separate local UI API port; must differ from the MCP port. The UI host is always `127.0.0.1`. |
| `CODEX_BRIDGE_ALLOWED_ROOTS` | empty | Required path-separated canonical roots. Empty means every `cwd` is rejected. |
| `CODEX_BRIDGE_ALLOWED_HOSTS` | SDK loopback defaults | Exact Host allowlist, comma-separated. `example.com:*` allows any port. |
| `CODEX_BRIDGE_ALLOWED_ORIGINS` | SDK loopback defaults | Exact browser Origin allowlist, comma-separated. This is separate from Host validation. |
| `CODEX_BRIDGE_CODEX_EXECUTABLE` | `codex` | Codex executable name or path. The fixed subcommand remains `app-server --stdio`. |
| `CODEX_BRIDGE_TUNNEL_EXECUTABLE` | unset | Optional explicit `tunnel-client` executable override; an invalid explicit value fails closed. |
| `CODEX_BRIDGE_TUNNEL_PROFILE` | `codex-bridge` | Existing Secure MCP Tunnel profile name; valid values are 1–64 ASCII letters, digits, `.`, `_`, or `-`. |
| `CODEX_BRIDGE_WAIT_DEFAULT_SECONDS` | `18` | Default long-poll duration. |
| `CODEX_BRIDGE_WAIT_MAX_SECONDS` | `30` | Hard maximum long-poll duration. |
| `CODEX_BRIDGE_SHUTDOWN_GRACE_SECONDS` | `3` | Shutdown grace period for the App Server child. |

### Tunnel Host configuration

The current MCP SDK v2 enables localhost-safe DNS-rebinding protection for the loopback default. A request arriving through a tunnel uses the tunnel's `Host` header, so it can receive `421 Misdirected Request` unless that runtime hostname is explicitly allowed.

Set runtime values without committing them:

```powershell
$env:CODEX_BRIDGE_ALLOWED_HOSTS = 'your-tunnel.example.com,your-tunnel.example.com:*'
$env:CODEX_BRIDGE_ALLOWED_ORIGINS = 'https://your-chat-origin.example.com'
```

Only use the actual host and origin values supplied by the tunnel/client deployment. Do not hard-code tunnel hostnames, identifiers, tokens, or secrets. Host/DNS-rebinding protection is not CORS; `CODEX_BRIDGE_ALLOWED_HOSTS` controls Host validation and `CODEX_BRIDGE_ALLOWED_ORIGINS` controls Origin validation through the SDK's `TransportSecuritySettings`.

## MCP tools

The server publishes exactly nine tools:

| Tool | Inputs | Result |
| --- | --- | --- |
| `codex_start` | `cwd`, `prompt` | Native `thread_id`, `turn_id`, and `in_progress` state; does not wait for completion. |
| `codex_continue` | `thread_id`, `prompt` | Starts a new turn, calling `thread/resume` first when the thread is not loaded in this process. |
| `codex_wait` | `thread_id`, `turn_id`, optional `timeout_seconds` | Bounded normalized state, latest agent message, latest diff, pending request, and error. |
| `codex_steer` | `thread_id`, `turn_id`, `prompt` | Uses `turn/steer` with `expectedTurnId` equal to the supplied turn ID. |
| `codex_approval` | `request_id`, `decision` | Resolves one pending approval. Decisions are `accept`, `acceptForSession`, `decline`, or `cancel`. |
| `codex_user_input` | `request_id`, `answers` | Resolves one pending user-input request keyed by exact question IDs. |
| `codex_interrupt` | `thread_id`, `turn_id` | Requests interruption; the later terminal event determines the final state. |
| `codex_threads` | optional `thread_id`, history flag, limit, cursor | Lists native threads or reads one sanitized native thread/history response. |
| `codex_status` | `thread_id`, optional `turn_id`, `activity_limit` (1-100, default 20) | Returns the current safe turn snapshot plus bounded recent Activity records. |

Normalized states are `in_progress`, `needs_approval`, `needs_input`, `completed`, `interrupted`, and `failed`.

## Example workflow

1. Call `codex_start` with an allowed absolute `cwd` and a complete task prompt.
2. Call `codex_wait` with the returned native IDs.
3. If the result is `needs_approval`, show `pending_request` to the user and call `codex_approval` with an explicit decision.
4. If the result is `needs_input`, show the questions and call `codex_user_input` with answers keyed by their IDs.
5. Continue polling `codex_wait` until `completed`, `interrupted`, or `failed`.
6. Use `codex_steer` while the turn is running, or `codex_interrupt` when it must stop.
7. Use `codex_status` for read-only observation without opening a second App Server writer.

After a CodexBridge restart, call `codex_continue` with the same native `thread_id`. The bridge first reads the persisted thread metadata, validates its canonical `cwd`, and calls `thread/resume` only when that cwd is allowed; it does not edit rollout/history files or repair a failed resume.

## Approval flow

Codex server requests are stored only in process memory and surfaced by `codex_wait`. Approval responses are method-specific JSON-RPC responses with the closed decision enum from the installed App Server schema; arbitrary JSON passthrough and automatic approval are intentionally absent. Command/file approvals use the native `decision` response. Permission approvals retain only the schema-shaped requested file-system/network subset plus bounded `cwd`, `environmentId`, reason, and available turn/session scopes; accept grants that subset, `acceptForSession` selects session scope, and decline/cancel returns an empty native grant. User-input responses must match the pending question IDs and use the App Server shape `{ "answers": ["..."] }` per question. Unknown, duplicate, or mismatched request IDs are rejected without resolving another request.

The allowed-root policy applies to every bridge entry point that can select a thread: `codex_start` validates its input `cwd`; `codex_continue` validates persisted metadata before resume; `codex_threads` validates detail/history before returning it and filters list rows by canonical `cwd`. A list page may contain fewer rows than its native page size because disallowed or malformed rows are omitted.

Unsupported App Server server-initiated requests fail closed with a bounded JSON-RPC error and do not stop the reader. MCP `mcpServer/elicitation/request` is answered with the schema-valid `action: cancel`. Terminal events and `serverRequest/resolved` notifications clear stale pending requests defensively.

## Activity observation

`ActivityStore` is a process-local in-memory ring buffer with up to 500 normalized Activity records per native thread. It records turn lifecycle, command execution, file-change, agent-message completion, approval/user-input, and error observations. Agent-message deltas update the authoritative latest message only; they do not create one Activity per delta. Command output, full diffs, raw JSON-RPC payloads, reasoning items, credentials, tokens, and tunnel identifiers are never stored in the Activity history. File paths are reduced to allowed-root-relative display paths, and paths outside the allowlist are omitted.

`codex_status` selects the requested turn, otherwise the active turn, otherwise the latest known turn. If no matching turn is known it returns `state: "not_loaded"` with an empty activity list; it never creates a synthetic turn.

## Phase 2 read-only history and UI API

Stored history is read through `thread/read` metadata validation followed by the App Server's `thread/turns/list` and `thread/items/list` pagination APIs when `historyMode` is `paginated`. Older or missing-mode threads use bounded `thread/read(includeTurns=true)` fallback; legacy responses do not pretend to support cursors and return null cursors plus an explicit truncation flag. Every history item passes a strict allowlist projection: reasoning and unknown items are omitted, user/agent/plan text is bounded, command output and full file diffs are omitted, MCP/dynamic tool arguments and results are omitted, and file/image paths are reduced to allowed-root-relative paths.

The independent UI listener serves `GET /healthz`, `/ui-api/status`, `/ui-api/threads`, `/ui-api/threads/{thread_id}`, `/ui-api/threads/{thread_id}/turns`, `/ui-api/threads/{thread_id}/items`, `/ui-api/threads/{thread_id}/status`, and `/ui-api/events`. It binds only to `127.0.0.1:<CODEX_BRIDGE_UI_PORT>`, permits only `127.0.0.1` and `localhost` Host values, has no CORS wildcard, and is never exposed through the Tunnel, whose target remains the MCP listener only. `/ui-api/events` is an SSE stream of new safe process-local Activity records; subscriber queues are bounded to 100 with oldest-drop backpressure, and no SQLite/replay persistence exists.

## Phase 3 read-only desktop Console

The optional Windows-oriented PySide6 Console is a read-only viewer for the UI API. It can connect to an already-running Bridge, and Phase 4A can also detect Codex and start a Bridge from the Console:

```powershell
uv sync --extra dev --extra console
uv run codex-bridge
uv run codex-bridge-console
```

The Console uses `CODEX_BRIDGE_UI_PORT` (default `8001`) or an explicit `--ui-port`, always connects to `http://127.0.0.1:<port>`, and uses Qt Network for asynchronous JSON GET and SSE reads. It shows the thread list, selected thread history, current status, pending approval/input summaries, recent Activity, and live Activity stream. It has no approval, input, steer, interrupt, new-thread, or other mutation controls. The UI API is loopback-only and is not a Tunnel target.

Console close stops its timers and aborts only its own HTTP/SSE replies. It does not stop CodexBridge, the Codex App Server, the Tunnel, or an active Codex turn.

## Phase 4A Codex detection and detached Bridge launch

Phase 4A adds a small runtime area to the Console. It asynchronously discovers and verifies a Codex executable with the priority `CODEX_BRIDGE_CODEX_EXECUTABLE`, the Windows Codex App native installation, PATH (`codex.exe`, `codex.cmd`, `codex`), and `%APPDATA%\npm\codex.cmd`. Candidate paths are deduplicated with Windows case-insensitive canonical comparison. The `--version` probe has a roughly three-second timeout and bounded output, and the Console shows only the detected version and source, never raw subprocess output or environment values.

When the existing UI API is ready, the Console reports `Runtime: external` and disables Start Bridge. The existing external Bridge is never replaced. A valid detected Codex enables Start Bridge only while the Bridge is unavailable. The button starts exactly one detached child using `sys.executable -m codex_bridge`; the child inherits the current environment, with only `CODEX_BRIDGE_CODEX_EXECUTABLE` and `CODEX_BRIDGE_UI_PORT` controlled by the launcher. The Console waits for `/healthz` and `/ui-api/status` readiness before showing `Runtime: started by Console`.

The detached Bridge and its App Server continue after the Console closes. A launch timeout is safe and does not trigger a retry; there is no automatic restart. Phase 4A has no Stop or Restart UI and does not kill, terminate, or interrupt a Bridge, App Server, or turn. Tunnel/tray remain out of scope for Phase 4A.

## Phase 4B Secure MCP Tunnel supervision and system tray

Phase 4B lets the Console supervise a configured Secure MCP Tunnel profile. Tunnel profile creation remains external: CodexBridge does not run `tunnel-client init`, edit profiles, configure connectors, or accept Tunnel IDs, API keys, OAuth data, or other identity input. The Console resolves the executable in this order: explicit `CODEX_BRIDGE_TUNNEL_EXECUTABLE`, PATH `tunnel-client.exe`, then PATH `tunnel-client`; invalid explicit values fail closed and `.ps1` files are excluded. The profile defaults to `codex-bridge` and is bounded to 1–64 ASCII letters, digits, `.`, `_`, or `-`.

After Bridge and App Server readiness, one asynchronous `tunnel-client doctor --profile <profile> --explain --health.listen-addr 127.0.0.1:0` preflight runs with inherited process environment, a ten-second timeout, and an 8 KiB combined output bound. Output is discarded and never shown. A managed Tunnel uses a Console-owned `QProcess`, a fresh OS-assigned `127.0.0.1` ephemeral health port, and direct program/argument configuration; the Console does not shell out, dump the environment, or construct private Tunnel targets. `/healthz` and `/readyz` are polled asynchronously, response bodies are discarded, automatic Tunnel restart is disabled, and unexpected exit is reported as `Tunnel: failed`.

The Console exposes `Start Tunnel`, `Stop Tunnel`, and `Restart Tunnel` only for its own Tunnel process. Stop uses bounded `terminate` then `kill`; external Tunnel is never discovered/taken over, scanned, killed, or restarted. `QSystemTrayIcon`, `QMenu`, and `QAction` provide `Show Console`, `Hide Console`, Tunnel controls, and `Exit`. When available, window close minimizes/hides to tray when available and keeps Bridge and the Console-owned Tunnel running. explicit Exit stops Console-owned Tunnel and then quits; Bridge remains running on Console Exit. Tunnel secrets/identity are not stored by CodexBridge. Bridge Stop/Restart is Phase 4C.

## Shutdown behavior

SIGINT, SIGTERM, and ASGI lifespan shutdown best-effort interrupt active turns and allow a short terminal-notification grace. It then stops the UI listener, closes protocol pipes, and performs bounded `terminate -> wait -> kill -> final wait` process cleanup. If UI startup fails, already-started App Server resources are cleaned up. Infinite waits, rollback, and crash repair are out of scope; if even the OS-level final wait times out, the process reference is retained rather than silently orphaned.

## Security assumptions and limitations

- This is single-user/local-use software; it has no authentication database or multi-user isolation.
- The cwd allowlist is an additional bridge boundary, not a replacement for Codex sandbox and approval policy; it applies to new starts, persisted-thread resume, detail/history reads, and list filtering.
- `cwd` must be an absolute existing directory. Canonical resolution rejects `..`, sibling-prefix collisions, case bypasses, and symlink/junction escapes outside allowed roots.
- Logs record lifecycle/state metadata only. Prompt text, credentials, API keys, tokens, complete environment data, raw event history, and raw chain-of-thought are not logged or returned by bounded status tools.
- State is intentionally process-memory only. Native Codex persistence is required for resume after restart.
- One App Server process serves all threads. Multi-process workers, shared state, and durable pending approvals are not supported.
- A real approval flow depends on Codex policy and runtime conditions. The smoke test exercised a temporary file-change approval; permission, command-execution, and user-input cases are covered by fake App Server/schema-shaped tests and are not automatically bypassed.
- The bridge does not provide dedicated commit/push/shell/file/Git tools. Put those instructions in the Codex task prompt.

## `codex mcp-server` Spike

The installed `codex mcp-server` was initialized over stdio and reported only two tools: `codex` and `codex-reply`. They cover a blocking initial session and a reply by thread ID, but do not expose the required bounded status polling, approval/user-input forwarding, interrupt, thread list/history, or running-turn steer controls. The Spike therefore does not satisfy this project and the App Server wrapper is retained.

## Tests

Normal unit tests use fake streams/processes and do not invoke real Codex:

```powershell
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run python -m compileall -q src tests scripts
```

The explicit real integration smoke test uses a temporary workspace and is not part of the normal suite:

```powershell
uv run python scripts/integration_smoke.py
```

It attempts App Server startup, a temporary file task, completion, continuation, normal shutdown, process restart, native-thread resume, and a running-turn steer. It never targets a source repository. For the exact temporary file-change request, it exercises `codex_approval` with `accept`; any other approval request remains unresolved and is reported instead of bypassing Codex's safety boundary.

## Out of scope for the initial version

SQLite, bridge session IDs, multi-user support, authentication storage, browser control UI, scheduler, job queue, automatic rollback, worktree generation, PR-specific automation, automatic approval, arbitrary shell/filesystem/Git MCP tools, execution-engine abstraction, telemetry SaaS, and complete hard-crash recovery are deliberately excluded. Bridge Stop/Restart remains deferred to Phase 4C.
