from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .rollout_metadata import (
    TurnAccessMetadata,
    TurnModelMetadata,
    read_turn_execution_metadata,
    unavailable_access_metadata,
    unavailable_metadata,
)


def attach_turn_execution_metadata(
    result: dict[str, object], thread_response: Mapping[str, Any]
) -> dict[str, object]:
    thread = thread_response.get("thread")
    rollout_path = thread.get("path") if isinstance(thread, Mapping) else None
    try:
        resolved = read_turn_execution_metadata(rollout_path)
    except Exception:
        resolved = {}

    metadata: dict[str, TurnModelMetadata] = {}
    access_metadata: dict[str, TurnAccessMetadata] = {}
    items = result.get("items")
    if isinstance(items, list):
        for entry in items:
            if not isinstance(entry, Mapping):
                continue
            turn_id = entry.get("turn_id")
            if not isinstance(turn_id, str) or turn_id in metadata:
                continue
            turn_metadata = resolved.get(turn_id)
            metadata[turn_id] = (
                {
                    "model_candidates": turn_metadata["model_candidates"],
                    "model_resolution_status": turn_metadata["model_resolution_status"],
                }
                if turn_metadata is not None
                else unavailable_metadata()
            )
            access_metadata[turn_id] = (
                {
                    "access_candidates": turn_metadata["access_candidates"],
                    "access_resolution_status": turn_metadata["access_resolution_status"],
                }
                if turn_metadata is not None
                else unavailable_access_metadata()
            )
    result["turn_model_metadata"] = metadata
    result["turn_access_metadata"] = access_metadata
    return result
