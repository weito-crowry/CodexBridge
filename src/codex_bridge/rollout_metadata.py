from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Literal, TypedDict

ModelResolutionStatus = Literal["resolved", "multiple", "unavailable"]
AccessResolutionStatus = Literal["resolved", "multiple", "partial", "unavailable"]


class ModelCandidate(TypedDict):
    model: str
    reasoning_effort: str | None


class TurnModelMetadata(TypedDict):
    model_candidates: list[ModelCandidate]
    model_resolution_status: ModelResolutionStatus


class TurnAccessMetadata(TypedDict):
    access_candidates: list[str]
    access_resolution_status: AccessResolutionStatus


class TurnExecutionMetadata(TurnModelMetadata, TurnAccessMetadata):
    pass


_SANDBOX_TYPE_ALIASES = {
    "dangerFullAccess": "danger-full-access",
    "readOnly": "read-only",
    "workspaceWrite": "workspace-write",
    "externalSandbox": "external-sandbox",
}
_KNOWN_SANDBOX_TYPES = frozenset(
    {
        *_SANDBOX_TYPE_ALIASES.values(),
        "danger-full-access",
        "read-only",
        "workspace-write",
        "external-sandbox",
    }
)


def unavailable_metadata() -> TurnModelMetadata:
    return {
        "model_candidates": [],
        "model_resolution_status": "unavailable",
    }


def unavailable_access_metadata() -> TurnAccessMetadata:
    return {
        "access_candidates": [],
        "access_resolution_status": "unavailable",
    }


def read_turn_execution_metadata(path: object) -> dict[str, TurnExecutionMetadata]:
    """Read the small, normalized set of per-turn values used by the Console."""
    if not isinstance(path, (str, os.PathLike)):
        return {}
    try:
        file_path = os.fspath(path)
        if isinstance(file_path, bytes):
            return {}
        return _read_jsonl(file_path)
    except (OSError, TypeError, ValueError):
        return {}


def read_turn_model_metadata(path: object) -> dict[str, TurnModelMetadata]:
    """Read only the existing model/effort contract from a rollout JSONL file."""
    return {
        turn_id: {
            "model_candidates": metadata["model_candidates"],
            "model_resolution_status": metadata["model_resolution_status"],
        }
        for turn_id, metadata in read_turn_execution_metadata(path).items()
    }


def _read_jsonl(path: str) -> dict[str, TurnExecutionMetadata]:
    model_pairs_by_turn: dict[str, list[ModelCandidate]] = {}
    seen_models_by_turn: dict[str, set[tuple[str, str | None]]] = {}
    access_candidates_by_turn: dict[str, list[str]] = {}
    seen_access_by_turn: dict[str, set[str]] = {}
    incomplete_access_turns: set[str] = set()
    try:
        with open(path, "rb") as stream:
            for raw_line in stream:
                if b'"turn_context"' not in raw_line:
                    continue
                try:
                    record = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(record, Mapping) or record.get("type") != "turn_context":
                    continue
                payload = record.get("payload")
                if not isinstance(payload, Mapping):
                    continue
                turn_id = payload.get("turn_id")
                if not isinstance(turn_id, str) or not turn_id.strip():
                    continue

                model_candidates = model_pairs_by_turn.setdefault(turn_id, [])
                seen_models = seen_models_by_turn.setdefault(turn_id, set())
                access_candidates = access_candidates_by_turn.setdefault(turn_id, [])
                seen_access = seen_access_by_turn.setdefault(turn_id, set())

                model = payload.get("model")
                if isinstance(model, str) and model.strip():
                    effort = payload.get("effort")
                    reasoning_effort = (
                        effort if isinstance(effort, str) and effort.strip() else None
                    )
                    pair = (model, reasoning_effort)
                    if pair not in seen_models:
                        seen_models.add(pair)
                        model_candidates.append(
                            {"model": model, "reasoning_effort": reasoning_effort}
                        )

                access = _normalize_sandbox_policy(payload.get("sandbox_policy"))
                if access is None:
                    incomplete_access_turns.add(turn_id)
                elif access not in seen_access:
                    seen_access.add(access)
                    access_candidates.append(access)

    except (OSError, UnicodeError):
        return {}

    return {
        turn_id: {
            "model_candidates": model_candidates,
            "model_resolution_status": _model_status(model_candidates),
            "access_candidates": access_candidates_by_turn[turn_id],
            "access_resolution_status": _access_status(
                access_candidates_by_turn[turn_id], turn_id in incomplete_access_turns
            ),
        }
        for turn_id, model_candidates in model_pairs_by_turn.items()
    }


def _normalize_sandbox_policy(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    sandbox_type = value.get("type")
    if not isinstance(sandbox_type, str):
        return None
    normalized = _SANDBOX_TYPE_ALIASES.get(sandbox_type, sandbox_type)
    return normalized if normalized in _KNOWN_SANDBOX_TYPES else None


def _model_status(candidates: list[ModelCandidate]) -> ModelResolutionStatus:
    if not candidates:
        return "unavailable"
    return "resolved" if len(candidates) == 1 else "multiple"


def _access_status(candidates: list[str], incomplete: bool) -> AccessResolutionStatus:
    if not candidates:
        return "unavailable"
    if incomplete:
        return "partial"
    return "resolved" if len(candidates) == 1 else "multiple"
