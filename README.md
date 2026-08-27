# CodexBridge

CodexBridge is a thin single-user bridge for this path:

```text
ChatGPT -> Remote MCP -> CodexBridge -> codex app-server -> Codex
```

It exposes only thread/turn control and approval or user-input forwarding. Shell execution, file changes, Git, sandbox policy, authentication, reasoning, commit, and push remain owned by Codex.

## Architecture

The Streamable HTTP MCP server owns one ASGI lifespan. Startup creates exactly one `codex app-server --stdio` child and performs the JSON-RPC `initialize` / `initialized` handshake. A dedicated JSONL reader correlates response IDs and routes notifications and server-initiated requests to the in-memory state store. MCP tools call a transport-neutral bridge over that client.

The bridge does not create a session ID or database. Codex's native `thread.id` is returned unchanged as `thread_id`; persistence and resume are delegated to Codex rollout/history data.

## Requirements

- Python 3.11+
- A locally authenticated Codex CLI
- `uv` recommended for installation and tests
- A tunnel or reverse proxy prepared separately when ChatGPT must reach the local endpoint

The implementation was developed and protocol-checked against `codex-cli 0.137.0`. The generated protocol bundle was obtained with:

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

The default bind is loopback only. The tunnel is not started or configured by CodexBridge, and no tunnel identifier or token belongs in this repository.

## Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `CODEX_BRIDGE_HOST` | `127.0.0.1` | Local bind address. |
| `CODEX_BRIDGE_PORT` | `8000` | Local bind port. |
| `CODEX_BRIDGE_ALLOWED_ROOTS` | empty | Required path-separated canonical roots. Empty means every `cwd` is rejected. |
| `CODEX_BRIDGE_ALLOWED_HOSTS` | SDK loopback defaults | Exact Host allowlist, comma-separated. `example.com:*` allows any port. |
| `CODEX_BRIDGE_ALLOWED_ORIGINS` | SDK loopback defaults | Exact browser Origin allowlist, comma-separated. This is separate from Host validation. |
| `CODEX_BRIDGE_CODEX_EXECUTABLE` | `codex` | Codex executable name or path. The fixed subcommand remains `app-server --stdio`. |
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

The server publishes exactly eight tools:

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

Normalized states are `in_progress`, `needs_approval`, `needs_input`, `completed`, `interrupted`, and `failed`.

## Example workflow

1. Call `codex_start` with an allowed absolute `cwd` and a complete task prompt.
2. Call `codex_wait` with the returned native IDs.
3. If the result is `needs_approval`, show `pending_request` to the user and call `codex_approval` with an explicit decision.
4. If the result is `needs_input`, show the questions and call `codex_user_input` with answers keyed by their IDs.
5. Continue polling `codex_wait` until `completed`, `interrupted`, or `failed`.
6. Use `codex_steer` while the turn is running, or `codex_interrupt` when it must stop.

After a CodexBridge restart, call `codex_continue` with the same native `thread_id`. The bridge calls `thread/resume`; it does not edit rollout/history files or repair a failed resume.

## Approval flow

Codex server requests are stored only in process memory and surfaced by `codex_wait`. Approval responses are method-specific JSON-RPC responses with the closed decision enum from the installed App Server schema; arbitrary JSON passthrough and automatic approval are intentionally absent. User-input responses must match the pending question IDs and use the App Server shape `{ "answers": ["..."] }` per question. Unknown, duplicate, or mismatched request IDs are rejected without resolving another request.

## Shutdown behavior

SIGINT, SIGTERM, and ASGI lifespan shutdown best-effort interrupt active turns, wait for the configured short grace period, close protocol pipes, and terminate the App Server child if it has not exited. Infinite waits, hard-kill recovery, rollback, and crash repair are out of scope.

## Security assumptions and limitations

- This is single-user/local-use software; it has no authentication database or multi-user isolation.
- The cwd allowlist is an additional bridge boundary, not a replacement for Codex sandbox and approval policy.
- `cwd` must be an absolute existing directory. Canonical resolution rejects `..`, sibling-prefix collisions, case bypasses, and symlink/junction escapes outside allowed roots.
- Logs record lifecycle/state metadata only. Prompt text, credentials, API keys, tokens, complete environment data, raw event history, and raw chain-of-thought are not logged or returned by bounded status tools.
- State is intentionally process-memory only. Native Codex persistence is required for resume after restart.
- One App Server process serves all threads. Multi-process workers, shared state, and durable pending approvals are not supported.
- A real approval flow depends on Codex policy and runtime conditions; protocol routing is covered by fake App Server tests and the smoke test reports approval cases that cannot be safely reproduced.
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
uv run python -m compileall -q src tests
```

The explicit real integration smoke test uses a temporary workspace and is not part of the normal suite:

```powershell
uv run python scripts/integration_smoke.py
```

It attempts App Server startup, a temporary file task, completion, continuation, normal shutdown, process restart, native-thread resume, and a running-turn steer. It never targets a source repository. For the exact temporary file-change request, it exercises `codex_approval` with `accept`; any other approval request remains unresolved and is reported instead of bypassing Codex's safety boundary.

## Out of scope for the initial version

SQLite, bridge session IDs, multi-user support, authentication storage, web UI, scheduler, job queue, automatic rollback, worktree generation, PR-specific automation, automatic approval, arbitrary shell/filesystem/Git MCP tools, execution-engine abstraction, telemetry SaaS, and complete hard-crash recovery are deliberately excluded.
