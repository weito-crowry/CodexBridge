# Phase C5-P3B isolated Workflow read-only projection audit

This tool checks whether the already completed C5 isolated attempt can be read
through the product Workflow projections. It does not start, resume, reconcile,
or modify a Workflow or Codex job. It does not start Bridge, Worker, or GUI
processes and does not call HTTP, MCP, or external services.

The --inspect mode is intentionally reserved for a separate instruction after
GitHub review. This implementation turn must run only the synthetic --prepare
mode and offline tests.

## Offline preparation

Run from the repository root on Windows native:

~~~powershell
Set-Location D:\CodexBridge
& .\.venv\Scripts\python.exe -m scripts.phase_c5_isolated_readonly_projection
& .\.venv\Scripts\python.exe -m scripts.phase_c5_isolated_readonly_projection --prepare
~~~

The first command prints help. --prepare validates an in-memory synthetic
marker and imports the required read-only projection functions. It does not
read the attempt marker or enumerate Temp.

## Read-only inspection after review

Only after a separate instruction to inspect a specific completed attempt:

~~~powershell
Set-Location D:\CodexBridge
$ApprovalId = Read-Host "Approval ID supplied for this inspection"
& .\.venv\Scripts\python.exe -m scripts.phase_c5_isolated_readonly_projection --inspect --approval-id=$ApprovalId
~~~

The supplied value must be 32 lowercase hexadecimal characters. The tool reads
only the matching marker under the repository, direct Temp children named
codexbridge-phase-b5-*, the corresponding requests.sqlite files, and the
three run directories under the single uniquely matched state root. SQLite is
opened with URI mode=ro and PRAGMA query_only=ON. It does not create a
missing database or follow a symlink, junction, or Windows reparse point.

The fixed JSON output contains only case labels A/B/C, request/public status,
terminal and worker state, result presence, public job row count, task/event
coverage codes, bounded counts, and the event cursor. It never prints workflow,
run, job, or thread identifiers, definitions, input, result or event content,
paths, tokens, or exception messages. A public succeeded projection is
reported separately from a raw request row that remains running; this tool
does not reconcile or change either value.

## Interpretation

- PASS means the single marker/state-root match was validated and all three
  read-only product projections returned safe, schema-valid summaries.
- BLOCKED identifies only a fixed stage/code, and optionally an exception
  class. It does not include exception text or a traceback.
- task_coverage and event_coverage distinguish complete, partial, and
  unavailable evidence. task_coverage_reason is an allowlisted code, none, or
  unknown. A partial or unavailable projection is not upgraded to complete.
- public_jobs_count is null for run A because its single-job DTO uses the
  legacy job fields. Runs B/C report a count only when the public jobs tuple is
  available.
- A successful audit establishes local read-only projection availability. It
  does not establish visible GUI acceptance or authorize execution.

No --execute option exists. Do not use this utility to retry an attempt or to
inspect a different attempt without a separate instruction.
