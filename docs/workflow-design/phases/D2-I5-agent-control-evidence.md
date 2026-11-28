# D2-I5 Agent control implementation evidence

## Usage availability

I5 keeps `usage_supported=false` and returns `usage=null` for Codex Agent results.
The current Bridge sources do not provide token counts attributable to an exact
`(thread_id, turn_id)` pair.

The Console parser in `src/codex_bridge/console/usage.py` reads
`rateLimitsByLimitId.codex` (or the legacy `rateLimits` shape), then extracts
`windowDurationMins` and `usedPercent` for the 5-hour and weekly account windows.
Those values describe account rate-limit state, not tokens consumed by one turn.

`src/codex_bridge/console/usage_snapshots.py` keys snapshots by thread and turn,
but captures one account-level snapshot and associates it with every pending turn
in that request. That association is UI display context, not exact per-turn usage
evidence, so its values are not projected into Agent usage.

The Agent read path is limited to exact-turn status, turn history, and item history
through `CodexAgentHTTPClient.get_agent_thread_status`,
`read_agent_turns_page`, and `read_agent_items_page`. The history projection in
`src/codex_bridge/history.py` selects turn identity, status, timestamps, duration,
error, and projected items; it does not expose input, output, or total token counts.
The current Agent observation path likewise emits no `usage` value. In the
provider-neutral I2 result schema, the four usage fields remain nullable; no
aggregate rate-limit value or inferred delta is substituted for them.

If a future Bridge or app-server history source exposes token counts directly on
an exact turn record, keyed by both thread and turn, the Agent provider can map
those values and identify that source. Until then, exact-turn usage is unavailable
and is not an I5 blocker.
