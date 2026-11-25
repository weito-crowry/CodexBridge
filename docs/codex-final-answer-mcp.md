# `codex_final_answer` MCP contract

`codex_final_answer(thread_id, turn_id, target_id?)` reads only the exact requested turn. It
returns `target_id`, `thread_id`, `turn_id`, and one of `retrieved`, `pending`, `incomplete`, or
`unavailable`. A `retrieved` result includes the item ID, exact text, and source marker
`app_server.item/completed.agentMessage.phase=final_answer`. Other results include a specific
`reason` and never include text.

Bridge captures only `item/completed` notifications whose item is an `agentMessage` with phase
`final_answer` and valid thread, turn, item, and nonempty text. The text is limited to 256 Ki UTF-16
code units; oversized text is rejected without truncation. IDs are opaque printable strings capped
at 256 UTF-16 code units; path separators and control characters are rejected. A result is
retrieved only when exactly one final item is accepted and the same turn has persisted terminal
status `completed`. Duplicate events with the same item ID and content are idempotent. Different
content for the same item ID, multiple final items, conflicting terminal statuses, and failed,
interrupted, or unknown terminal states do not return text. The existing `latest_agent_message`
projection remains unchanged. This API never reads it.

## Storage and privacy

Final text and its item ID are stored in a versioned SQLite database at
`%LOCALAPPDATA%\CodexBridge\final-answers.sqlite3` on Windows, with the existing per-user Local
AppData fallback. Other platforms use `$XDG_STATE_HOME/CodexBridge/final-answers.sqlite3` or
`~/.local/state/CodexBridge/final-answers.sqlite3`. SQLite transactions use full synchronous writes;
terminal evidence is committed before a subsequent read can return `retrieved`. If a process stops
between item capture and terminal capture, the answer remains pending or unavailable.

The App Server reader awaits Bridge's async notification handler in arrival order. The handler runs
the existing state/activity projection synchronously, then enqueues final-answer writes in a
bounded FIFO queue of 4 entries. One worker performs SQLite writes in order; a full queue applies
backpressure, and it does not create one worker thread per notification. An MCP read enqueues a
barrier (waiting for a queue slot if full), releases the Bridge lock, and waits until preceding
writes commit. Once queued, later JSON-RPC messages can continue while the read waits. Canceling a
read or shutdown waiter does not cancel queued writes. Bridge shutdown stops the App Server reader,
drains the queue, and then closes its worker. SQLite reads use a read-only connection and deferred
read transaction; they do not
initialize the schema, create a missing database, or acquire an immediate write lock. A locked or
unreadable store may return `unavailable` rather than delay the event loop.

Retention is 30 days, at most 256 turns, and 24 MiB of answer text; the SQLite database is capped at
64 MiB. Pruning happens transactionally on capture writes: expired rows are removed first, then the
oldest turns are removed to satisfy the turn and text caps. Reads do not prune. Logs contain error
categories only, never answer text. The database relies on the operating system's per-user file
permissions and is not application-encrypted.

## Target and CPP behavior

Local reads do not call App Server or mutate a thread. Remote reads are forwarded only after that
remote target's `tools/list` explicitly advertises `codex_final_answer`; unsupported or unavailable
targets return a structured `unavailable` result and never fall back to local state. Thread and turn
identities are checked again on the response. CPP can discover availability from its limited
principal's `tools/list`; this tool is added to that principal's allowlist.

Older turns whose final item was not captured are not reconstructed from thread history, rollout
JSONL, `codex_status.latest_agent_message`, or commentary/delta events. A storage error is reported
as `unavailable`.

## Acceptance plan

1. Run isolated tests for phase filtering, duplicate/conflicting items, terminal ordering and
   status, text-size boundary, restart persistence, storage failure, retention, target routing, and
   CPP principal visibility/call acceptance.
2. In a later CPP acceptance, confirm `codex_final_answer` appears in CPP `tools/list`; call it with
   the exact thread and turn returned by `codex_wait`; verify the confirmed source marker and exact
   text only for a captured completed turn.
3. Verify a legacy uncaptured turn and an unsupported remote target return a reason without text,
   and that existing `codex_status`, `codex_threads`, and `codex_wait` behavior remains unchanged.

The CPP/live acceptance in steps 2–3 is a follow-up and was not run as part of this offline change.
