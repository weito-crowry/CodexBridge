"""Read-only turn observations, independent of App Server load state."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def observation(
    turn_id: object = None,
    status: object = "unknown",
    *,
    evidence: str = "persisted",
    observed_at: object = None,
    reason: object = None,
) -> dict[str, Any]:
    status = status if isinstance(status, str) else "unknown"
    reason = reason if isinstance(reason, str) else None
    status = {"inProgress": "in_progress"}.get(status, status)
    if status not in {"completed", "failed", "interrupted", "in_progress"}:
        status = "unknown"
    if evidence == "persisted" and status == "in_progress":
        status, reason = "unknown", "unconfirmed_execution"
    if reason in {"user", "user_requested", "user_stop"} and status == "interrupted":
        reason = "user_stop"
    elif reason not in {
        "retrying",
        "connection_lost",
        "unconfirmed_execution",
        "needs_approval",
        "needs_input",
    }:
        reason = {"completed": "completed", "failed": "turn_failed", "in_progress": "running"}.get(
            str(status), "unknown" if turn_id else "history_unavailable"
        )
    category = {
        "completed": "success",
        "user_stop": "stopped",
        "turn_failed": "failure",
        "retrying": "retrying",
        "running": "running",
        "needs_approval": "waiting",
        "needs_input": "waiting",
        "connection_lost": "observation_lost",
    }.get(str(reason), "interruption" if status == "interrupted" else "unknown")
    if isinstance(observed_at, (int, float)) and not isinstance(observed_at, bool):
        try:
            epoch = observed_at / 1000 if observed_at > 100_000_000_000 else observed_at
            observed_at = datetime.fromtimestamp(epoch, UTC).isoformat()
        except (ValueError, OSError, OverflowError):
            observed_at = None
    return {
        "turn_id": turn_id if isinstance(turn_id, str) else None,
        "status": status,
        "observed_at": observed_at if isinstance(observed_at, str) else None,
        "reason": reason,
        "category": category,
        "evidence": evidence,
        "needs_attention": reason in {"connection_lost", "unconfirmed_execution", "turn_failed"}
        or status == "interrupted"
        and reason != "user_stop",
    }


def persisted_turn(turn: Mapping[str, Any]) -> dict[str, Any]:
    timestamp = next(
        (
            turn[k]
            for k in ("completedAt", "completedAtMs", "updatedAt", "startedAt", "startedAtMs")
            if turn.get(k) is not None
        ),
        None,
    )
    result = observation(
        turn.get("id"), turn.get("status"), observed_at=timestamp, reason=turn.get("reason")
    )
    if result["observed_at"] is None and result["turn_id"] is not None:
        result["observed_at"] = datetime.now(UTC).isoformat()
    return result


def read_rollout_outcome(path: object) -> dict[str, Any] | None:
    if not isinstance(path, str):
        return None
    latest: dict[str, Any] | None = None
    kinds = {
        "task_started": "in_progress",
        "turn_started": "in_progress",
        "task_complete": "completed",
        "task_completed": "completed",
        "task_failed": "failed",
        "turn_complete": "completed",
        "turn_completed": "completed",
        "turn_aborted": "interrupted",
        "turn_interrupted": "interrupted",
        "turn_failed": "failed",
    }
    try:
        with Path(path).open("rb") as stream:
            for line in stream:
                if not line.endswith(b"\n"):
                    continue
                try:
                    record = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(record, dict) or record.get("type") != "event_msg":
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
                    continue
                if payload["type"] not in kinds:
                    continue
                turn_id = payload.get("turn_id", record.get("turn_id"))
                if not isinstance(turn_id, str) or not turn_id:
                    turn_id = payload.get("turnId", record.get("turnId"))
                if not isinstance(turn_id, str) or not turn_id:
                    turn_id = None
                kind = payload["type"]
                if turn_id is None:
                    turn_id = latest["turn_id"] if latest is not None else None
                # A delayed terminal for an older turn must not replace the newest start.
                if (
                    latest
                    and kind not in {"task_started", "turn_started"}
                    and (turn_id != latest["turn_id"])
                ):
                    continue
                latest = observation(
                    turn_id,
                    kinds[kind],
                    observed_at=record.get("timestamp"),
                    reason=payload.get("reason"),
                )
    except (OSError, ValueError):
        return None
    return latest


class TurnOutcomeHints:
    def __init__(self) -> None:
        self.stops: set[tuple[str, str]] = set()
        self.reasons: dict[tuple[str, str], str] = {}

    def observe(self, message: Mapping[str, Any]) -> None:
        params = message.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("threadId"), str):
            return
        turn = params.get("turn")
        turn_id = turn.get("id") if isinstance(turn, dict) else params.get("turnId")
        if not isinstance(turn_id, str):
            return
        key = (params["threadId"], turn_id)
        if message.get("method") in {"turn/started", "turn/completed"}:
            self.reasons.pop(key, None)
        elif message.get("method") == "error":
            self.reasons[key] = "retrying" if params.get("willRetry") is True else "turn_failed"

    def live(self, snapshot: Mapping[str, Any], timestamp: object) -> dict[str, Any]:
        key = (snapshot["thread_id"], snapshot["turn_id"])
        state = snapshot.get("state")
        reason = self.reasons.get(key)
        evidence = "live"
        if reason == "connection_lost":
            state, evidence = "unknown", "inferred"
        elif state == "interrupted" and key in self.stops:
            reason = "user_stop"
        elif state in {"needs_approval", "needs_input"}:
            reason, state = state, "in_progress"
        return observation(
            snapshot["turn_id"], state, evidence=evidence, observed_at=timestamp, reason=reason
        )


class PersistedTurnReader:
    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}

    def cached(self, thread_id: str) -> dict[str, Any] | None:
        cached = self._cache.get(thread_id)
        return dict(cached[1]) if cached else None

    async def read(
        self,
        thread_id: str,
        metadata_reader: Callable[[str], Awaitable[tuple[dict[str, Any], str, str]]],
        legacy_reader: Callable[[str, str], Awaitable[dict[str, Any]]],
        request: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        cached = self._cache.get(thread_id)
        if cached is not None and time.monotonic() - cached[0] < 5:
            return dict(cached[1])
        metadata: dict[str, Any] = {}
        result = observation()
        try:
            response, mode, cwd = await metadata_reader(thread_id)
            metadata = response["thread"]
            if mode == "paginated":
                history = await request(
                    "thread/turns/list",
                    {
                        "threadId": thread_id,
                        "limit": 1,
                        "sortDirection": "desc",
                        "itemsView": "notLoaded",
                    },
                )
                turns = history.get("data")
                turn = turns[0] if isinstance(turns, list) and turns else None
            else:
                history = await legacy_reader(thread_id, cwd)
                turns = history["thread"].get("turns")
                turn = turns[-1] if isinstance(turns, list) and turns else None
            if isinstance(turn, Mapping):
                result = persisted_turn(turn)
        except (RuntimeError, ValueError, OSError):
            pass
        if result["turn_id"] is None:
            # Path comes only from the cwd-validated history metadata.
            rollout_result = await asyncio.to_thread(read_rollout_outcome, metadata.get("path"))
            if rollout_result is not None:
                result = rollout_result
        if result["turn_id"] is None and cached is not None:
            result = cached[1]
        self._cache[thread_id] = (time.monotonic(), result)
        return dict(result)
