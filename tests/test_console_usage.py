from __future__ import annotations

from codex_bridge.console.usage import (
    format_codex_usage,
    format_codex_usage_tooltip,
    parse_codex_usage,
)


def _window(duration: int, used: object, reset: object = None) -> dict[str, object]:
    return {
        "windowDurationMins": duration,
        "usedPercent": used,
        "resetsAt": reset,
    }


def test_codex_usage_classifies_windows_by_duration_not_primary_secondary_order() -> None:
    usage = parse_codex_usage(
        {
            "rateLimitsByLimitId": {
                "codex": {
                    "secondary": _window(10080, 39),
                    "primary": _window(300, 28),
                }
            }
        }
    )

    assert usage.five_hour is not None
    assert usage.five_hour.remaining_percent == 72
    assert usage.weekly is not None
    assert usage.weekly.remaining_percent == 61
    assert format_codex_usage(usage) == "Codex Usage  5h 72% · Week 61%"


def test_codex_usage_prefers_codex_limit_id_and_falls_back_to_rate_limits() -> None:
    usage = parse_codex_usage(
        {
            "rateLimitsByLimitId": {"other": {"primary": _window(300, 1)}},
            "rateLimits": {"primary": _window(300, 15), "secondary": _window(10080, 20)},
        }
    )

    assert usage.five_hour is not None
    assert usage.five_hour.remaining_percent == 85
    assert usage.weekly is not None
    assert usage.weekly.remaining_percent == 80


def test_codex_usage_clamps_used_percent_to_zero_through_one_hundred_remaining() -> None:
    usage = parse_codex_usage(
        {"rateLimits": {"primary": _window(300, 150), "secondary": _window(10080, -20)}}
    )

    assert usage.five_hour is not None
    assert usage.five_hour.remaining_percent == 0
    assert usage.weekly is not None
    assert usage.weekly.remaining_percent == 100


def test_codex_usage_formats_each_missing_window_and_unavailable_state() -> None:
    five_hour_only = parse_codex_usage({"rateLimits": {"primary": _window(300, 12)}})
    weekly_only = parse_codex_usage({"rateLimits": {"secondary": _window(10080, 25)}})
    unavailable = parse_codex_usage({"rateLimits": {"primary": {"usedPercent": 12}}})

    assert format_codex_usage(five_hour_only) == "Codex Usage  5h 88% · Week —"
    assert format_codex_usage(weekly_only) == "Codex Usage  5h — · Week 75%"
    assert format_codex_usage(unavailable) == "Codex Usage  unavailable"


def test_codex_usage_tooltip_includes_remaining_and_local_reset_values() -> None:
    usage = parse_codex_usage(
        {
            "rateLimits": {
                "primary": _window(300, 28, "2030-01-02T03:04:05Z"),
                "secondary": _window(10080, 39, "2030-01-09T03:04:05Z"),
            }
        }
    )

    tooltip = format_codex_usage_tooltip(usage)

    assert "5h: 72% left" in tooltip
    assert "Week: 61% left" in tooltip
    assert "2030-01-02 " in tooltip
    assert "2030-01-09 " in tooltip
