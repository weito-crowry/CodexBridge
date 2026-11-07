from __future__ import annotations

from collections.abc import Mapping

from .history_stream import TERMINAL_TURN_STATES
from .history_timeline import ACTIVE_TURN_STATES


def observe_runtime_thread_status(thread: Mapping[str, object]) -> tuple[bool, str | None]:
    status = thread.get("runtime_status")
    if not isinstance(status, Mapping):
        return False, None
    status_type = status.get("type")
    if not isinstance(status_type, str):
        return False, None
    if status_type in {"notLoaded", "idle", "systemError"}:
        return True, None
    if status_type != "active":
        return False, None
    active_flags = status.get("active_flags")
    if not isinstance(active_flags, list) or any(
        not isinstance(flag, str) for flag in active_flags
    ):
        return False, None
    if not active_flags:
        return True, "in_progress"
    if active_flags == ["waitingOnApproval"]:
        return True, "needs_approval"
    if active_flags == ["waitingOnUserInput"]:
        return True, "needs_input"
    return True, "waiting"


def classify_activity_thread_status(payload: object) -> tuple[str, str] | None:
    if not isinstance(payload, Mapping):
        return None
    thread_id = payload.get("thread_id")
    activity_type = payload.get("type")
    status = payload.get("status")
    if not isinstance(thread_id, str) or not isinstance(activity_type, str):
        return None
    if activity_type == "approval_requested" and status == "requested":
        return thread_id, "needs_approval"
    if activity_type == "user_input_requested" and status == "requested":
        return thread_id, "needs_input"
    if not isinstance(status, str):
        return None
    if activity_type == "turn_started" and status in ACTIVE_TURN_STATES:
        return thread_id, status
    if activity_type in {"turn_completed", "turn_failed", "turn_interrupted", "error"} and (
        status in TERMINAL_TURN_STATES
    ):
        return thread_id, status
    return None
