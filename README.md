# CodexBridge

CodexBridge is a local-first MCP bridge that lets an MCP client such as ChatGPT control a locally
authenticated Codex App Server while keeping Codex itself responsible for execution, sandboxing,
approvals, file changes, Git operations, and reasoning.

```text
ChatGPT / MCP client
        |
        v
   CodexBridge
        |
        +--> local Codex App Server
        |
        +--> optional remote CodexBridge target
        |
        +--> optional GitHub Remote MCP mount
```

> **Status:** v0.1 public beta. The project is usable for day-to-day dogfooding, but tool schemas,
> Codex App Server compatibility, packaging, and setup details may still change.

## Highlights

- Start, continue, wait for, steer, interrupt, inspect, and resume Codex threads.
- Forward Codex approval and user-input requests without automatically approving them.
- Select execution target, model, and reasoning effort through an MCP Apps setup UI when supported
  by the client.
- Route Codex work to one local machine or configured remote CodexBridge targets.
- Optionally mount GitHub Remote MCP tools into the same MCP namespace.
- Use a Windows-oriented PySide6 Console for runtime status, thread history, activity, bridge/tunnel
  control, Codex usage, and usage-history graphs.
- Persist local Codex usage snapshots in SQLite and export selected history ranges as CSV.
- Keep the local UI API loopback-only and separate from the MCP/tunnel endpoint.
- Expose bounded diagnostic probes used to inspect MCP protocol/capability behavior without invoking
  Codex.

CodexBridge does **not** provide its own arbitrary shell, filesystem, Git, or automatic-approval
tools. Those operations remain under Codex or the mounted upstream MCP.

## Architecture

The Streamable HTTP MCP server owns one ASGI lifespan. Startup creates one
`codex app-server --stdio` child and performs the Codex App Server JSON-RPC
`initialize` / `initialized` handshake. A dedicated JSONL reader correlates response IDs and
routes notifications and server-initiated requests into the bridge state model.

The MCP transport and the Codex App Server protocol are separate layers:

```text
MCP client
   |
   | Streamable HTTP MCP
   v
CodexBridge
   |
   | stdio JSON-RPC
   v
codex app-server
```

CodexBridge does not create a routing database or replace native Codex persistence. Local Codex
`thread.id` values stay unchanged. Remote thread IDs retain target affinity so later calls and bridge
restarts route back to the same execution target.

A separate local UI API is served on loopback for the desktop Console. It is not mounted on the MCP
listener and is not a tunnel target.

## MCP compatibility

CodexBridge uses MCP SDK v2 and Streamable HTTP. During ChatGPT dogfooding on 2026-10-01, the
bridge observed MCP protocol version `2026-07-28`, including per-request client metadata and the
new MCP routing headers.

The built-in diagnostic tools can report bounded, allowlisted protocol/capability information. They
are included for interoperability testing and are not required for normal Codex operation.

The setup UI uses the MCP Apps / UI extension. A compatible client can render the bundled
`text/html;profile=mcp-app` resource and use it to choose target, model, and reasoning effort.
Clients without MCP Apps support can still use the normal CodexBridge tools.

## Requirements

- Python 3.11+
- A locally installed and authenticated Codex CLI
- `uv` recommended for source installation and development
- PySide6 when using the desktop Console
- A separately configured HTTPS tunnel or reverse proxy when a remote MCP client such as ChatGPT
  must reach the local MCP endpoint

CodexBridge does not bundle Codex CLI credentials or create tunnel identities.

The implementation has been exercised against Codex CLI / Desktop App Server builds in the 0.150
series. Codex App Server is still an evolving interface, so newer Codex releases may require bridge
updates.

## Quick start from source

Clone the repository and install the Console extras:

```powershell
git clone https://github.com/weito-crowry/CodexBridge.git
cd CodexBridge
uv sync --extra dev --extra console
```

Create a user configuration with at least one allowed root.

On Windows:

```text
%APPDATA%\CodexBridge\config.toml
```

On Linux/macOS:

```text
$XDG_CONFIG_HOME/codexbridge/config.toml
```

or, when `XDG_CONFIG_HOME` is not set:

```text
~/.config/codexbridge/config.toml
```

Minimal configuration:

```toml
[bridge]
allowed_roots = ["C:\\Users\\you\\Documents\\src"]

[console]
ui_port = 8001

[targets.local]
name = "Local PC"
kind = "local"
```

Start the Console:

```powershell
uv run codex-bridge-console
```

For development or troubleshooting you can run the bridge directly:

```powershell
uv run codex-bridge
```

Default endpoints:

```text
MCP:    http://127.0.0.1:8000/mcp
UI API: http://127.0.0.1:8001/healthz
```

The MCP listener defaults to loopback. ChatGPT therefore needs a separately configured HTTPS tunnel
or reverse proxy to reach it.

## Configuration

CodexBridge reads a user-local TOML file. Set `CODEX_BRIDGE_CONFIG` to use a different file.

A fuller example:

```toml
[bridge]
allowed_roots = ["C:\\Users\\you\\Documents\\src"]
# codex_executable = "C:\\path\\to\\codex.exe"

[console]
ui_port = 8001

[tunnel]
# executable = "C:\\path\\to\\tunnel-client.exe"
profile = "codex-bridge"

[github_mcp]
enabled = false
url = "https://api.githubcopilot.com/mcp/x/all"
prefix = "github_"
include = ["*"]
exclude = []
toolsets = []
max_tools = 0

[targets.main-pc]
name = "Main PC"
kind = "local"

[targets.notebook]
name = "Notebook PC"
kind = "remote"
url = "https://notebook.example.test/mcp"
```

Configuration precedence is:

```text
explicit CLI option
    >
environment variable
    >
user config file
    >
built-in default
```

`allowed_roots` is required at Bridge startup and every supplied or persisted working directory is
validated as an absolute, existing, canonicalized directory inside one of those roots.

An explicit target configuration must contain exactly one local target. Without target tables,
CodexBridge creates the legacy implicit target `local` named `Local PC`.

Remote targets run a normal CodexBridge instance on the remote machine. Nested routing gateways are
not supported. CodexBridge does not synchronize project files between machines.

### Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `CODEX_BRIDGE_HOST` | `127.0.0.1` | MCP bind address. |
| `CODEX_BRIDGE_PORT` | `8000` | MCP bind port. |
| `CODEX_BRIDGE_UI_PORT` | `8001` | Separate loopback-only UI API port. |
| `CODEX_BRIDGE_ALLOWED_ROOTS` | empty | Required path-separated allowed working roots. |
| `CODEX_BRIDGE_ALLOWED_HOSTS` | SDK loopback defaults | Exact Host allowlist for MCP transport security. |
| `CODEX_BRIDGE_ALLOWED_ORIGINS` | SDK loopback defaults | Exact browser Origin allowlist. |
| `CODEX_BRIDGE_CODEX_EXECUTABLE` | resolver | Explicit Codex executable name or path. |
| `CODEX_BRIDGE_CONTROL_TOKEN` | unset | Per-launch Console control token; never persisted. |
| `CODEX_BRIDGE_TUNNEL_EXECUTABLE` | resolver | Explicit tunnel client path. |
| `CODEX_BRIDGE_TUNNEL_PROFILE` | `codex-bridge` | Existing tunnel profile name. |
| `CODEX_BRIDGE_WAIT_DEFAULT_SECONDS` | `50` | Default `codex_wait` long-poll duration. |
| `CODEX_BRIDGE_WAIT_MAX_SECONDS` | `55` | Maximum configured long-poll duration. |
| `CODEX_BRIDGE_SHUTDOWN_GRACE_SECONDS` | `3` | App Server shutdown grace period. |
| `CODEX_BRIDGE_GITHUB_MCP_ENABLED` | `false` | Enable the GitHub Remote MCP mount. |
| `CODEX_BRIDGE_GITHUB_MCP_URL` | `https://api.githubcopilot.com/mcp/x/all` | GitHub Remote MCP endpoint. |
| `CODEX_BRIDGE_GITHUB_MCP_PREFIX` | `github_` | Prefix for exposed upstream tools. |
| `CODEX_BRIDGE_GITHUB_MCP_INCLUDE` | `*` | Included upstream tool-name globs. |
| `CODEX_BRIDGE_GITHUB_MCP_EXCLUDE` | empty | Excluded upstream tool-name globs. |
| `CODEX_BRIDGE_GITHUB_MCP_TOOLSETS` | empty | Optional `X-MCP-Toolsets` value. |
| `CODEX_BRIDGE_GITHUB_MCP_MAX_TOOLS` | `0` | Maximum exposed upstream tools; `0` means unlimited. |
| `CODEX_BRIDGE_GITHUB_PAT` | unset | Dedicated GitHub bearer token; environment-only. |

The config file intentionally does not accept API keys, runtime control tokens, or the GitHub MCP
PAT.

## Connecting through a tunnel

CodexBridge does not create or configure tunnel identities. Configure the tunnel separately, then
point it at the local MCP listener.

MCP SDK v2 enables DNS-rebinding protection. A request arriving through a tunnel uses the tunnel's
`Host` header, so the runtime hostname must be explicitly allowed. For example:

```powershell
$env:CODEX_BRIDGE_ALLOWED_HOSTS = 'your-tunnel.example.com,your-tunnel.example.com:*'
$env:CODEX_BRIDGE_ALLOWED_ORIGINS = 'https://your-chat-origin.example.com'
```

Use only the real host and origin values from your deployment. Do not commit tunnel hostnames,
tokens, credentials, or identities.

## Native MCP tools

The current ChatGPT-visible native catalog contains **16 tools** before optional Remote MCP mounts:

- 10 Codex control/observation tools
- 1 MCP Apps setup tool
- 5 bounded MCP diagnostic probes

The setup App also exposes two App-only helper tools used internally by the UI; those are not normal
LLM-facing tools.

### Codex control and observation

| Tool | Purpose |
| --- | --- |
| `codex_targets` | List configured execution targets and best-effort availability. |
| `codex_start` | Start a Codex thread/turn with optional target, model, reasoning effort, and sandbox mode. |
| `codex_continue` | Start a new turn on an existing thread, resuming it when necessary. |
| `codex_wait` | Long-poll one turn without creating another turn. |
| `codex_steer` | Send additional input to the expected active turn. |
| `codex_approval` | Resolve one pending command/file/permission approval. |
| `codex_user_input` | Resolve one pending Codex user-input request. |
| `codex_interrupt` | Request interruption of an active turn. |
| `codex_threads` | List threads or read one sanitized thread/history view. |
| `codex_status` | Read the current safe turn state and recent normalized activity. |

Normalized turn states are `in_progress`, `needs_approval`, `needs_input`, `completed`,
`interrupted`, and `failed`.

### MCP Apps setup

| Tool | Purpose |
| --- | --- |
| `codex_setup` | Open/describe the target, model, reasoning, and access-mode setup flow. |

When the client advertises MCP Apps support, `codex_setup` can render the bundled setup UI. The UI
revalidates the selected target/model/reasoning/access combination on the server before confirming
it. Access defaults to `Default` (`inherit`).

### Diagnostic probes

| Tool | Purpose |
| --- | --- |
| `mcp_tasks_probe` | Report protocol version and advertised Tasks capabilities. |
| `mcp_long_wait_probe` | Hold one MCP tool call for a bounded interval without invoking Codex. |
| `mcp_long_wait_progress_probe` | Attempt bounded MCP progress reporting during a wait. |
| `mcp_progress_token_probe` | Report safe progress-token/client-capability metadata. |
| `mcp_request_headers_probe` | Report only allowlisted MCP routing headers and comparisons. |

The probes intentionally avoid returning raw credentials, cookies, authorization headers, session
IDs, or arbitrary metadata values.

## Recommended Codex workflow

1. Call `codex_start` with an allowed absolute `cwd` and a complete task prompt. Optionally set
   `sandbox_mode="danger-full-access"` when the thread needs Full Access.
2. Call `codex_wait` with the returned `thread_id` and `turn_id`.
3. If the result is `needs_approval`, inspect `pending_request` and call `codex_approval` with an
   explicit decision.
4. If the result is `needs_input`, answer the supplied question IDs through `codex_user_input`.
5. Continue calling `codex_wait` until the turn becomes terminal.
6. Use `codex_steer` only for a running-turn correction and `codex_interrupt` when it must stop.
7. Use `codex_status` for read-only observation.

Repeating `codex_wait` with the same IDs is expected and does not create a new Codex turn.

After a bridge restart, `codex_continue` can resume a persisted native Codex thread after its stored
working directory passes the same allowed-root validation.

## Full Access

`codex_start` accepts `sandbox_mode="inherit"` or `sandbox_mode="danger-full-access"`. The default
is `inherit`, which leaves the sandbox setting to Codex. Full Access selects Codex's
`danger-full-access` sandbox mode for that new thread. It does not disable or change the Codex
approval policy; approvals remain active according to that policy.

The configured `allowed_roots` check remains in force for the thread's working directory. It limits
which `cwd` CodexBridge can start or resume and is independent of the sandbox mode used after the
thread starts.

## Approval and data boundaries

Pending Codex approval and user-input requests are kept only in process memory. Approval decisions
use the closed schema supported by the App Server; CodexBridge intentionally provides no arbitrary
approval JSON passthrough and no automatic approval.

History and status responses are sanitized. Reasoning items and unknown item types are omitted,
text is bounded, command output and complete file diffs are not exposed through history, tool
arguments/results are not copied into history, and file/image paths are reduced to safe
allowed-root-relative display paths when possible.

The bridge logs lifecycle/state metadata rather than prompts, credentials, raw chain-of-thought, or
complete environment dumps.

## GitHub Remote MCP mount

When enabled, CodexBridge connects to the configured GitHub Remote MCP endpoint, optionally sends
the configured `X-MCP-Toolsets` header, fetches all `tools/list` pages, applies:

```text
include
  -> exclude
  -> stable name sort
  -> max_tools
```

and publishes the resulting tools with the configured prefix in the same MCP namespace as the
native tools.

The remote catalog is fixed for the process lifetime. A later upstream disconnect does not silently
remove tools from the catalog. Communication failures are surfaced explicitly, and a request with an
unknown outcome is not automatically retried.

The GitHub PAT is accepted only through `CODEX_BRIDGE_GITHUB_PAT`; it is never read from TOML,
logged, or returned by tools.

## Desktop Console

The optional PySide6 Console is the recommended daily entry point on Windows.

It provides:

- Bridge, App Server, tunnel, and Codex status.
- Thread list, sanitized thread history, and recent Activity.
- Live SSE activity updates from the loopback-only UI API.
- Detection of local Codex installations.
- Start/Stop/Restart for a Bridge launched by the current Console session.
- Start/Stop/Restart for a tunnel launched by the current Console session.
- System-tray integration.
- Codex usage display.
- Usage History with 7-day, 1-month, 1-year, and custom ranges.
- 5-hour and weekly remaining-percentage series.
- Confirmed weekly-reset candidate markers/history.
- CSV export for the selected usage-history range.
- Review and resolve pending approvals for a Bridge started by this Console, using the same approval
  handling as the `codex_approval` MCP tool.

Usage snapshots are stored locally in SQLite. On Windows the default location is under
`%LOCALAPPDATA%\CodexBridge\usage-history.sqlite3`; on other platforms it follows
`XDG_STATE_HOME` or `~/.local/state`.

The Console never takes ownership of an already-running external Bridge. Lifecycle controls remain
disabled for external Bridge processes. Pending approval details can be viewed, but approval
controls are unavailable without the Console-owned control token; resolve those requests through an
MCP client instead.

The local UI API stays bound to `127.0.0.1` and is never exposed through the MCP tunnel.

## Windows packaged app

A Windows build can be produced with:

```powershell
uv sync --extra dev --extra console --extra package
.\scripts\build_windows.ps1
```

The build creates:

```text
dist\CodexBridge\
dist\CodexBridge-windows.zip
```

The PyInstaller package uses `onedir` + windowed mode. It includes the CodexBridge application
icon and the bundled MCP Apps setup HTML resource.

It does **not** include:

- Codex CLI
- Codex authentication
- GitHub PATs
- tunnel credentials or identities
- user configuration
- project working files

The target machine therefore still needs an authenticated Codex CLI and any separately configured
tunnel client/profile that you intend to use.

## Shutdown behavior

Console Exit follows one bounded shutdown path. It stops only Console-owned tunnel/Bridge processes
and never takes over an external Bridge. Bridge shutdown requests use the loopback-only authenticated
control API with a fresh per-launch token.

Normal cleanup is graceful and bounded. CodexBridge does not perform automatic taskkill/PID-kill
fallbacks for Bridge control failures.

Stopping or restarting a Bridge can interrupt active Codex turns.

## Security assumptions and limitations

- CodexBridge is single-user/local-use software; it has no authentication database or multi-user
  isolation.
- The allowed-root policy is an additional bridge boundary, not a replacement for Codex sandbox and
  approval policy.
- Working directories are canonicalized and checked against configured allowed roots.
- The UI API is loopback-only.
- The MCP listener defaults to loopback and must be explicitly exposed through your own tunnel or
  reverse proxy for remote clients.
- No secrets belong in repository configuration.
- GitHub PATs are environment-only.
- Runtime control tokens are generated per Console launch and are not persisted.
- State such as pending approvals remains process-local; native Codex persistence is used for thread
  resume after restart.
- One App Server process serves all local threads.
- Automatic approval, arbitrary shell/filesystem/Git bridge tools, multi-user tenancy, a durable job
  queue, automatic rollback, and complete hard-crash recovery are out of scope for v0.1.

## Tests

Normal tests use fake streams/processes and do not invoke real Codex:

```powershell
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run python -m compileall -q src tests scripts
```

The explicit real integration smoke test uses a temporary workspace:

```powershell
uv run python scripts/integration_smoke.py
```

It exercises App Server startup, a temporary file task, completion, continuation, shutdown/restart,
native-thread resume, and running-turn steer without targeting the CodexBridge source repository.

## Development notes

The repository contains historical design/specification documents under `docs/superpowers/`. Those
documents describe the implementation phases that led to the current code and may contain historical
scope statements. The current README and source code should be treated as the user-facing v0.1
description.

The project intentionally wraps Codex App Server rather than `codex mcp-server`, because the latter
does not currently expose the bounded polling, approval/input forwarding, interruption,
thread/history, and running-turn steering controls required by CodexBridge.

## License

MIT. See [LICENSE](LICENSE).
