from __future__ import annotations

from typing import Any

from codex_bridge.console import history_metadata


def _summarize(turn_ids: list[str], metadata: dict[str, Any]) -> Any:
    summarize = getattr(history_metadata, "summarize_turn_model_metadata", None)
    assert callable(summarize), "history summary helper has not been implemented"
    return summarize(turn_ids, metadata)


def _entry(model: str, effort: str | None, *, status: str = "resolved") -> dict[str, object]:
    return {
        "model_candidates": [{"model": model, "reasoning_effort": effort}],
        "model_resolution_status": status,
    }


def test_single_resolved_turn_is_shown_with_history_source() -> None:
    summary = _summarize(["turn-1"], {"turn-1": _entry("gpt-5", "high")})

    assert summary.model == "gpt-5"
    assert summary.effort == "high"
    assert "読み込んだ履歴" in summary.model_tooltip
    assert "turn-1" in summary.model_tooltip


def test_multiple_turns_with_the_same_observed_values_keep_one_value() -> None:
    summary = _summarize(
        ["turn-1", "turn-2"],
        {"turn-1": _entry("gpt-5", "high"), "turn-2": _entry("gpt-5", "high")},
    )

    assert summary.model == "gpt-5"
    assert summary.effort == "high"
    assert "turn-1" in summary.effort_tooltip
    assert "turn-2" in summary.effort_tooltip


def test_distinct_observed_models_are_reported_as_multiple() -> None:
    summary = _summarize(
        ["turn-1", "turn-2"],
        {"turn-1": _entry("gpt-5", "high"), "turn-2": _entry("gpt-5.1", "high")},
    )

    assert summary.model == "Multiple"
    assert summary.effort == "high"
    assert "gpt-5" in summary.model_tooltip
    assert "gpt-5.1" in summary.model_tooltip


def test_distinct_long_model_values_are_not_collapsed_by_tooltip_truncation() -> None:
    prefix = "model-" + "x" * 100
    summary = _summarize(
        ["turn-1", "turn-2"],
        {"turn-1": _entry(prefix + "a", "high"), "turn-2": _entry(prefix + "b", "high")},
    )

    assert summary.model == "Multiple"
    assert len(summary.model_tooltip) <= 1200


def test_same_model_with_different_efforts_keeps_model_and_reports_effort_multiple() -> None:
    summary = _summarize(
        ["turn-1"],
        {
            "turn-1": {
                "model_candidates": [
                    {"model": "gpt-5", "reasoning_effort": "low"},
                    {"model": "gpt-5", "reasoning_effort": "high"},
                ],
                "model_resolution_status": "multiple",
            }
        },
    )

    assert summary.model == "gpt-5"
    assert summary.effort == "Multiple"
    assert "multiple" in summary.model_tooltip
    assert "low" in summary.effort_tooltip
    assert "high" in summary.effort_tooltip


def test_missing_effort_does_not_invent_a_value() -> None:
    summary = _summarize(["turn-1"], {"turn-1": _entry("gpt-5", None)})

    assert summary.model == "gpt-5"
    assert summary.effort is None
    assert "turn-1" in summary.effort_tooltip


def test_unavailable_turns_are_partial_and_metadata_without_a_loaded_turn_is_ignored() -> None:
    summary = _summarize(
        ["unavailable", "null", "invalid", "known"],
        {
            "unavailable": {
                "model_candidates": [],
                "model_resolution_status": "unavailable",
            },
            "null": None,
            "invalid": {"model_candidates": "not-a-list"},
            "known": _entry("gpt-5", "high"),
            "orphan": _entry("gpt-6", "low"),
        },
    )

    assert summary.model == "gpt-5"
    assert summary.effort == "high"
    assert "一部" in summary.model_tooltip
    assert "unavailable" in summary.model_tooltip
    assert "orphan" not in summary.model_tooltip
    assert "gpt-6" not in summary.model_tooltip


def test_no_matching_timeline_turn_has_no_displayable_metadata() -> None:
    summary = _summarize(["loaded-turn"], {"orphan": _entry("gpt-5", "high")})

    assert summary.model is None
    assert summary.effort is None


def test_tooltips_include_history_source_and_are_truncated() -> None:
    turn_ids = [f"turn-{index:02d}-" + "x" * 100 for index in range(20)]
    metadata = {
        turn_id: _entry(f"model-{index:02d}-" + "m" * 100, "high")
        for index, turn_id in enumerate(turn_ids)
    }

    summary = _summarize(turn_ids, metadata)

    assert len(summary.model_tooltip) <= 1200
    assert len(summary.effort_tooltip) <= 1200
    assert "読み込んだ履歴" in summary.model_tooltip
    assert "省略" in summary.model_tooltip
