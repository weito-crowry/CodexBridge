from __future__ import annotations

from collections.abc import Mapping


def _turn_model_metadata_from_payload(payload: object) -> dict[str, Mapping[str, object]]:
    if not isinstance(payload, Mapping):
        return {}
    raw_metadata = payload.get("turn_model_metadata")
    if not isinstance(raw_metadata, Mapping):
        return {}
    result: dict[str, Mapping[str, object]] = {}
    for turn_id, raw_entry in raw_metadata.items():
        if not isinstance(turn_id, str) or not isinstance(raw_entry, Mapping):
            continue
        raw_candidates = raw_entry.get("model_candidates")
        candidates: list[dict[str, object]] = []
        if isinstance(raw_candidates, list):
            for raw_candidate in raw_candidates:
                if not isinstance(raw_candidate, Mapping):
                    continue
                model = raw_candidate.get("model")
                if not isinstance(model, str) or not model:
                    continue
                effort = raw_candidate.get("reasoning_effort")
                candidates.append(
                    {
                        "model": model[:512],
                        "reasoning_effort": effort[:512]
                        if isinstance(effort, str) and effort
                        else None,
                    }
                )
        status = raw_entry.get("model_resolution_status")
        if not isinstance(status, str) or status not in {"resolved", "multiple", "unavailable"}:
            status = (
                "unavailable"
                if not candidates
                else "resolved"
                if len(candidates) == 1
                else "multiple"
            )
        result[turn_id] = {
            "model_candidates": candidates,
            "model_resolution_status": status,
        }
    return result
