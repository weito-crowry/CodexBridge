from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

_MAX_MODELS = 500
_MAX_REASONING_EFFORTS = 32
_MAX_DESCRIPTION_LENGTH = 2_000


def _non_empty_string(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _valid_model(entry: object) -> dict[str, Any] | None:
    if not isinstance(entry, Mapping):
        return None
    if _non_empty_string(entry.get("id")) is None:
        return None
    model = _non_empty_string(entry.get("model"))
    display_name = entry.get("displayName")
    hidden = entry.get("hidden")
    efforts = entry.get("supportedReasoningEfforts")
    default_effort = _non_empty_string(entry.get("defaultReasoningEffort"))
    if (
        model is None
        or not isinstance(display_name, str)
        or type(hidden) is not bool
        or hidden
        or not isinstance(efforts, list)
        or len(efforts) > _MAX_REASONING_EFFORTS
        or default_effort is None
    ):
        return None

    normalized_efforts: list[dict[str, str | None]] = []
    effort_ids: set[str] = set()
    for effort in efforts:
        if not isinstance(effort, Mapping):
            return None
        effort_id = _non_empty_string(effort.get("reasoningEffort"))
        description = effort.get("description")
        if effort_id is None or (description is not None and not isinstance(description, str)):
            return None
        if effort_id in effort_ids:
            return None
        effort_ids.add(effort_id)
        normalized_efforts.append(
            {
                "id": effort_id,
                "description": (
                    description[:_MAX_DESCRIPTION_LENGTH] if isinstance(description, str) else None
                ),
            }
        )
    if default_effort not in effort_ids:
        return None
    description = entry.get("description")
    if description is not None and not isinstance(description, str):
        return None
    return {
        "model": model,
        "display_name": display_name[:_MAX_DESCRIPTION_LENGTH],
        "description": (
            description[:_MAX_DESCRIPTION_LENGTH] if isinstance(description, str) else None
        ),
        "reasoning_efforts": normalized_efforts,
        "default_reasoning_effort": default_effort,
        "_is_default": entry.get("isDefault") is True,
    }


def normalize_capabilities(
    pages: Sequence[Mapping[str, Any]], config_response: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate App Server model entries and project safe setup defaults."""
    models: list[dict[str, Any]] = []
    seen_models: set[str] = set()
    for page in pages:
        entries = page.get("data")
        if not isinstance(entries, list):
            raise ValueError("model capabilities are unavailable")
        for entry in entries:
            normalized = _valid_model(entry)
            if normalized is None:
                continue
            model = normalized["model"]
            if model in seen_models:
                continue
            seen_models.add(model)
            models.append(normalized)
            if len(models) > _MAX_MODELS:
                raise ValueError("model capabilities are unavailable")

    if not models:
        raise ValueError("model capabilities are unavailable")

    config = config_response.get("config")
    if not isinstance(config, Mapping):
        config = {}
    configured_model = _non_empty_string(config.get("model"))
    model_names = {entry["model"] for entry in models}
    if configured_model in model_names:
        selected_model = configured_model
    else:
        default_models = [entry["model"] for entry in models if entry["_is_default"]]
        selected_model = default_models[0] if len(default_models) == 1 else None

    selected = next((entry for entry in models if entry["model"] == selected_model), None)
    configured_effort = _non_empty_string(config.get("model_reasoning_effort"))
    supported = (
        {entry["id"] for entry in selected["reasoning_efforts"]} if selected is not None else set()
    )
    default_effort = (
        configured_effort
        if configured_effort in supported
        else selected["default_reasoning_effort"]
        if selected is not None
        else None
    )

    public_models = [
        {key: value for key, value in entry.items() if key != "_is_default"} for entry in models
    ]
    return {
        "models": public_models,
        "defaults": {"model": selected_model, "reasoning_effort": default_effort},
    }


def project_public_capabilities(value: Mapping[str, Any]) -> dict[str, Any]:
    """Revalidate and bound the public capability shape received from a remote node."""
    raw_models = value.get("models")
    raw_defaults = value.get("defaults")
    if not isinstance(raw_models, list) or not isinstance(raw_defaults, Mapping):
        raise ValueError("model capabilities are unavailable")

    models: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_models:
        if not isinstance(raw, Mapping):
            continue
        model = _non_empty_string(raw.get("model"))
        display_name = raw.get("display_name")
        description = raw.get("description")
        raw_efforts = raw.get("reasoning_efforts")
        default_effort = _non_empty_string(raw.get("default_reasoning_effort"))
        if (
            model is None
            or model in seen
            or not isinstance(display_name, str)
            or (description is not None and not isinstance(description, str))
            or not isinstance(raw_efforts, list)
            or len(raw_efforts) > _MAX_REASONING_EFFORTS
            or default_effort is None
        ):
            continue
        efforts: list[dict[str, str | None]] = []
        ids: set[str] = set()
        valid = True
        for raw_effort in raw_efforts:
            if not isinstance(raw_effort, Mapping):
                valid = False
                break
            effort_id = _non_empty_string(raw_effort.get("id"))
            effort_description = raw_effort.get("description")
            if (
                effort_id is None
                or effort_id in ids
                or (effort_description is not None and not isinstance(effort_description, str))
            ):
                valid = False
                break
            ids.add(effort_id)
            efforts.append(
                {
                    "id": effort_id,
                    "description": (
                        effort_description[:_MAX_DESCRIPTION_LENGTH]
                        if isinstance(effort_description, str)
                        else None
                    ),
                }
            )
        if not valid or default_effort not in ids:
            continue
        seen.add(model)
        models.append(
            {
                "model": model,
                "display_name": display_name[:_MAX_DESCRIPTION_LENGTH],
                "description": (
                    description[:_MAX_DESCRIPTION_LENGTH] if isinstance(description, str) else None
                ),
                "reasoning_efforts": efforts,
                "default_reasoning_effort": default_effort,
            }
        )
        if len(models) > _MAX_MODELS:
            raise ValueError("model capabilities are unavailable")

    if not models:
        raise ValueError("model capabilities are unavailable")
    default_model = _non_empty_string(raw_defaults.get("model"))
    selected = next((entry for entry in models if entry["model"] == default_model), None)
    default_effort = _non_empty_string(raw_defaults.get("reasoning_effort"))
    supported = (
        {entry["id"] for entry in selected["reasoning_efforts"]} if selected is not None else set()
    )
    return {
        "models": models,
        "defaults": {
            "model": selected["model"] if selected is not None else None,
            "reasoning_effort": default_effort if default_effort in supported else None,
        },
    }
