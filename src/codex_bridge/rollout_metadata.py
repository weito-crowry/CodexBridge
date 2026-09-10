from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Literal, TypedDict

ModelResolutionStatus = Literal["resolved", "multiple", "unavailable"]


class ModelCandidate(TypedDict):
    model: str
    reasoning_effort: str | None


class TurnModelMetadata(TypedDict):
    model_candidates: list[ModelCandidate]
    model_resolution_status: ModelResolutionStatus


def unavailable_metadata() -> TurnModelMetadata:
    return {
        "model_candidates": [],
        "model_resolution_status": "unavailable",
    }


def read_turn_model_metadata(path: object) -> dict[str, TurnModelMetadata]:
    """Read only turn execution metadata from a rollout JSONL file.

    Rollout files are internal, evolving data. Every read or record-level parse
    failure is intentionally treated as missing metadata so callers can keep
    displaying App Server history.
    """
    if not isinstance(path, (str, os.PathLike)):
        return {}
    try:
        file_path = os.fspath(path)
        if isinstance(file_path, bytes):
            return {}
        return _read_jsonl(file_path)
    except (OSError, TypeError, ValueError):
        return {}


def _read_jsonl(path: str) -> dict[str, TurnModelMetadata]:
    pairs_by_turn: dict[str, list[ModelCandidate]] = {}
    seen_by_turn: dict[str, set[tuple[str, str | None]]] = {}
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
                pairs = pairs_by_turn.setdefault(turn_id, [])
                seen = seen_by_turn.setdefault(turn_id, set())
                model = payload.get("model")
                if not isinstance(model, str) or not model.strip():
                    continue
                effort = payload.get("effort")
                reasoning_effort = effort if isinstance(effort, str) and effort.strip() else None
                pair = (model, reasoning_effort)
                if pair in seen:
                    continue
                seen.add(pair)
                pairs.append({"model": model, "reasoning_effort": reasoning_effort})
    except (OSError, UnicodeError):
        return {}

    return {
        turn_id: {
            "model_candidates": candidates,
            "model_resolution_status": (
                "unavailable"
                if not candidates
                else "resolved"
                if len(candidates) == 1
                else "multiple"
            ),
        }
        for turn_id, candidates in pairs_by_turn.items()
    }
