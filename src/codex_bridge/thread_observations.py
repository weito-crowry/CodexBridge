"""Public projections of cached execution observations, without history I/O."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .activity import ActivityStore
from .external_sessions import ExternalExecutionMonitor
from .state import StateStore
from .turn_outcome import PersistedTurnReader, TurnOutcomeHints, observation

ACTIVE = {"in_progress", "needs_approval", "needs_input"}


class ThreadObservations:
    def __init__(
        self,
        state: StateStore,
        activities: ActivityStore,
        outcomes: TurnOutcomeHints,
        persisted: PersistedTurnReader,
    ) -> None:
        self._state = state
        self._activities = activities
        self._outcomes = outcomes
        self._persisted = persisted
        self.external: ExternalExecutionMonitor | None = None

    def external_snapshot(self) -> dict[str, Any]:
        return (
            self.external.snapshot()
            if self.external
            else {
                "state": "unavailable",
                "observed_at": None,
                "source": "rollout_unmatched_start",
                "threads": {},
            }
        )

    def current(self, thread_id: str, external: dict[str, Any] | None = None) -> dict[str, Any]:
        latest = self._state.active_turn_for_thread(thread_id) or self._state.latest_known_turn(
            thread_id
        )
        snapshot = self._state.snapshot_if_known(thread_id, latest) if latest else None
        if snapshot and snapshot["state"] in ACTIVE:
            activity = self._activities.latest(thread_id, latest)
            outcome = self._outcomes.live(snapshot, activity.timestamp if activity else None)
            if outcome["evidence"] == "live":
                return {
                    "state": snapshot["state"],
                    "evidence": "live",
                    "source": "bridge",
                    "observed_at": outcome["observed_at"],
                    "reason": "managed_turn",
                }
        external = self.external_snapshot() if external is None else external
        estimated = thread_id in external["threads"]
        return {
            "state": "estimated_running" if estimated else "unknown",
            "evidence": "inferred",
            "source": external["source"],
            "observed_at": external["observed_at"],
            "reason": "unmatched_start_not_liveness" if estimated else "execution_unconfirmed",
        }

    def attach(self, row: dict[str, Any], external: dict[str, Any] | None = None) -> dict[str, Any]:
        thread_id = row.get("id", row.get("thread_id"))
        if not isinstance(thread_id, str):
            return row
        row["current_execution"] = self.current(thread_id, external)
        if "last_turn" not in row:
            last_id = self._state.latest_known_turn(thread_id)
            snapshot = self._state.snapshot_if_known(thread_id, last_id) if last_id else None
            activity = self._activities.latest(thread_id, last_id) if last_id else None
            row["last_turn"] = (
                self._outcomes.live(snapshot, activity.timestamp if activity else None)
                if snapshot
                else self._persisted.cached(thread_id) or observation()
            )
            row["needs_attention"] = row["last_turn"]["needs_attention"]
        return row

    def attach_list(self, result: dict[str, Any]) -> dict[str, Any]:
        snapshot = self.external_snapshot()
        for row in result["threads"]:
            self.attach(row, snapshot)
        result["external_execution"] = {
            key: value for key, value in snapshot.items() if key != "threads"
        }
        result["external_threads"] = [
            self.attach(row, snapshot)
            for thread_id, row in snapshot["threads"].items()
            if self.current(thread_id, snapshot)["evidence"] != "live"
        ]
        return result

    async def status(
        self,
        result: dict[str, Any],
        thread_id: str,
        metadata_reader: Callable[[str], Awaitable[tuple[dict[str, Any], str, str]]],
        legacy_reader: Callable[[str, str], Awaitable[dict[str, Any]]],
        request: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        # A persisted read may race with the first live notification; live still wins.
        if self._state.latest_known_turn(thread_id) is None:
            result["last_turn"] = await self._persisted.read(
                thread_id, metadata_reader, legacy_reader, request
            )
        last_id = self._state.latest_known_turn(thread_id)
        if last_id is not None:
            snapshot = self._state.snapshot_if_known(thread_id, last_id)
            activity = self._activities.latest(thread_id, last_id)
            if snapshot is not None:
                result["last_turn"] = self._outcomes.live(
                    snapshot, activity.timestamp if activity is not None else None
                )
        result["needs_attention"] = result["last_turn"]["needs_attention"]
        return self.attach(result)
