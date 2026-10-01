from __future__ import annotations

from typing import Any

import pytest

from codex_bridge.setup_capabilities import normalize_capabilities, project_public_capabilities


def model(
    model_name: str,
    *,
    default: bool = False,
    efforts: tuple[str, ...] = ("low", "high"),
    default_effort: str = "low",
    hidden: bool = False,
) -> dict[str, Any]:
    return {
        "id": f"id:{model_name}",
        "model": model_name,
        "displayName": f"Display {model_name}",
        "description": f"About {model_name}",
        "hidden": hidden,
        "isDefault": default,
        "supportedReasoningEfforts": [
            {"reasoningEffort": effort, "description": f"{effort} detail"} for effort in efforts
        ],
        "defaultReasoningEffort": default_effort,
    }


def test_normalize_capabilities_uses_effective_config_when_supported() -> None:
    result = normalize_capabilities(
        [{"data": [model("model-a", default=True), model("model-b")]}],
        {"config": {"model": "model-b", "model_reasoning_effort": "high"}},
    )

    assert result["defaults"] == {"model": "model-b", "reasoning_effort": "high"}
    assert result["models"][1] == {
        "model": "model-b",
        "display_name": "Display model-b",
        "description": "About model-b",
        "reasoning_efforts": [
            {"id": "low", "description": "low detail"},
            {"id": "high", "description": "high detail"},
        ],
        "default_reasoning_effort": "low",
    }
    assert "id:model-b" not in str(result)


def test_normalize_capabilities_skips_hidden_and_malformed_models() -> None:
    malformed = model("bad")
    malformed["defaultReasoningEffort"] = "unsupported"
    malformed_effort = model("bad-effort")
    malformed_effort["supportedReasoningEfforts"] = [{"description": "missing id"}]
    result = normalize_capabilities(
        [{"data": [model("hidden", hidden=True), malformed, malformed_effort, model("visible")]}],
        {"config": {}},
    )

    assert [entry["model"] for entry in result["models"]] == ["visible"]


def test_normalize_capabilities_uses_unique_default_or_null_for_ambiguous() -> None:
    result = normalize_capabilities(
        [{"data": [model("first", default=True), model("second", default=True)]}],
        {"config": {"model": "stale"}},
    )

    assert result["defaults"] == {"model": None, "reasoning_effort": None}


def test_normalize_capabilities_falls_back_when_config_effort_is_unsupported() -> None:
    result = normalize_capabilities(
        [{"data": [model("model-a", default=True, default_effort="high")]}],
        {"config": {"model_reasoning_effort": "unsupported"}},
    )

    assert result["defaults"] == {"model": "model-a", "reasoning_effort": "high"}


def test_normalize_capabilities_rejects_unusable_catalog_and_pagination_cycle() -> None:
    with pytest.raises(ValueError, match="unavailable"):
        normalize_capabilities([{"data": [model("hidden", hidden=True)]}], {"config": {}})


def test_project_public_capabilities_revalidates_remote_catalog_and_drops_extra_fields() -> None:
    result = project_public_capabilities(
        {
            "models": [
                {
                    "model": "model-a",
                    "display_name": "Model A",
                    "description": "Description",
                    "reasoning_efforts": [{"id": "high", "description": "More"}],
                    "default_reasoning_effort": "high",
                    "url": "https://private.invalid",
                },
                {"model": "broken"},
            ],
            "defaults": {"model": "model-a", "reasoning_effort": "high"},
        }
    )

    assert result == {
        "models": [
            {
                "model": "model-a",
                "display_name": "Model A",
                "description": "Description",
                "reasoning_efforts": [{"id": "high", "description": "More"}],
                "default_reasoning_effort": "high",
            }
        ],
        "defaults": {"model": "model-a", "reasoning_effort": "high"},
    }
