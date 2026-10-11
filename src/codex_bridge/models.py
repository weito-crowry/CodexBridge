from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

NormalizedState = Literal[
    "in_progress",
    "needs_approval",
    "needs_input",
    "completed",
    "interrupted",
    "failed",
]
RequestId = int | str
ApprovalDecision = Literal["accept", "acceptForSession", "decline", "cancel"]
PermissionGrantScope = Literal["turn", "session"]
SandboxMode = Literal["inherit", "danger-full-access"]
ApprovalsReviewer = Literal["auto_review", "user"]
SANDBOX_MODES = frozenset({"inherit", "danger-full-access"})
APPROVALS_REVIEWERS = frozenset({"auto_review", "user"})


def validate_sandbox_mode(value: str | None) -> SandboxMode | None:
    if value is None:
        return None
    if value not in SANDBOX_MODES:
        raise ValueError("unsupported sandbox_mode")
    return value  # type: ignore[return-value]


def validate_approvals_reviewer(value: str) -> ApprovalsReviewer:
    if value not in APPROVALS_REVIEWERS:
        raise ValueError("unsupported approvals_reviewer")
    return value  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class UserInputQuestion:
    header: str
    id: str
    question: str


@dataclass(frozen=True, slots=True)
class UserInputAnswer:
    answers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PermissionRequestDetails:
    requested_permissions: dict[str, Any]
    cwd: str
    environment_id: str | None
    reason: str | None
    allowed_scopes: tuple[PermissionGrantScope, ...] = ("turn", "session")


@dataclass(frozen=True, slots=True)
class PendingRequest:
    request_id: RequestId
    method: str
    thread_id: str
    turn_id: str
    item_id: str | None = None
    questions: tuple[UserInputQuestion, ...] = ()
    summary: str | None = None
    permission: PermissionRequestDetails | None = None


@dataclass(slots=True)
class TurnState:
    thread_id: str
    turn_id: str
    state: NormalizedState = "in_progress"
    latest_agent_message: str = ""
    current_diff: str = ""
    pending_request_id: RequestId | None = None
    error: str | None = None
    generation: int = 0
    recent_events: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ThreadState:
    thread_id: str
    turns: dict[str, TurnState] = field(default_factory=dict)
    loaded: bool = False
    validated_cwd: str | None = None
    thread_metadata: dict[str, str | None] = field(
        default_factory=lambda: {
            "model_provider": None,
            "model": None,
            "reasoning_effort": None,
            "cli_version": None,
            "sandbox_mode": None,
            "approval_policy": None,
            "approvals_reviewer": None,
        }
    )
