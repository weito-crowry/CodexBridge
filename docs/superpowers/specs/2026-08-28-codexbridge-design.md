# CodexBridge MCP Design

**Status:** Approved for implementation

**Goal:** Provide a thin single-user Remote MCP bridge from ChatGPT to one long-lived local `codex app-server` process, preserving Codex's native thread identity and delegating coding, shell, Git, sandbox, and approval policy to Codex.

## Scope and constraints

- Python 3.11+.
- Use the current official MCP Python SDK v2 and Streamable HTTP at `/mcp`.
- Bind HTTP to localhost by default; host and port are configurable by environment variables.
- Start exactly one `codex app-server --stdio` subprocess for the bridge lifetime.
- Do not create a bridge session ID, database, queue, scheduler, web UI, rollback, worktree generator, or dedicated shell/file/Git tools.
- Return Codex's native `thread.id` unchanged as `thread_id`.
- Keep prompts, credentials, API keys, tokens, tunnel identifiers, and complete raw event payloads out of normal logs and bounded status responses.
- Require `CODEX_BRIDGE_ALLOWED_ROOTS`; an unset or empty allowlist rejects every `cwd`.

## External MCP interface

The server exposes exactly these eight tools:

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

`JsonRpcTransport` owns line-delimited JSON-RPC framing, request ID correlation, one reader task, response futures, notification dispatch, and server-request dispatch. `AppServerClient` owns subprocess startup, `initialize`/`initialized`, native method calls, stderr diagnostics, and shutdown. `StateStore` owns bounded in-memory thread/turn snapshots and wait conditions. `Bridge` translates native events and calls into the eight MCP-facing behaviors. The server module owns SDK registration and ASGI lifespan.

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

Unit tests use a fake App Server or transport double and never invoke real Codex. They cover handshake, request correlation, notifications, server-request routing, all eight tools, state normalization, concurrent threads, cross-talk prevention, lifecycle failures, path security, and graceful shutdown. A separate explicit integration smoke script uses a temporary workspace and a real local Codex installation; it is never part of the normal unit suite.

