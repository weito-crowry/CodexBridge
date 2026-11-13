# Phase 5F-4: two independent workflows

The shared `requests.sqlite` ledger admits up to two nonterminal workflow runs.
Each run keeps its own run directory, DBOS database, Codex job ledger, task
events, worker process, and lifecycle log. A workflow's `max_live_codex_jobs`
limit remains 1, 2, or 3 and its Codex jobs still run sequentially.

The two runs must use separate workspace trees. Equal workspaces and
ancestor/descendant workspace pairs conflict; Windows path case differences
refer to the same workspace. A full capacity or workspace conflict returns
HTTP 409 with the existing `workflow_capacity_reached` error.

Queued, starting, running, `needs_reconciliation`, and `unknown` runs occupy a
slot. `unknown` means the stored evidence cannot establish a terminal result;
the service keeps the slot and does not retry the POST. Reusing a request ID
with the same payload returns its existing run even when capacity is full. A
different payload for that ID returns the existing request ID conflict.

Use workflow list/detail/result to distinguish runs. For process-level
investigation, correlate each run's `run_id` and `workflow_id` with its
`worker_spawn_succeeded`, `shutdown_signal_requested`, and `worker_exited`
lifecycle events; `launch_id` identifies a particular worker launch and the
PID links its spawn and exit records.

Live two-workflow dogfood: **NOT_RUN** (scheduled for Phase 5F-5).
