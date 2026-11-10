from __future__ import annotations

import pytest

from codex_bridge.console import usage_forecast
from codex_bridge.console.usage_history import UsageHistorySample

NOW = 1_800_000_000.0


def samples(points: list[tuple[float, int | None, int | None]]) -> list[UsageHistorySample]:
    return [
        UsageHistorySample(int((NOW + offset) // 60 * 60), NOW + offset, five, week)
        for offset, five, week in points
    ]


def forecast(points, **kwargs):
    return usage_forecast.estimate_exhaustion(samples(points), now_epoch=NOW, **kwargs)


def test_irregular_samples_use_elapsed_time_and_independent_windows():
    points = [(-7200, 50, 80), (-4500, 45, 79), (0, 30, 76)]
    five = forecast(points, window="five_hour")
    weekly = forecast(points, window="weekly")
    assert five.status == "estimated"
    assert five.rate_points_per_hour == 10
    assert five.hours_remaining == 3
    assert five.exhaustion_epoch == NOW + 10800
    assert weekly.rate_points_per_hour == 2
    assert weekly.hours_remaining == 38


@pytest.mark.parametrize("hours, rate", [(1, 2), (3, 6), (6, 8), (12, 6)])
def test_lookback_changes_the_observation_interval(hours, rate):
    points = [
        (-43200, 92, 92),
        (-21600, 68, 68),
        (-10800, 38, 38),
        (-3600, 22, 22),
        (-1200, 21, 21),
        (0, 20, 20),
    ]
    result = forecast(points, window="five_hour", lookback_hours=hours)
    assert result.rate_points_per_hour == rate


@pytest.mark.parametrize(
    "points, reason",
    [
        ([], "insufficient_data"),
        ([(-3600, 20, 80), (0, 19, 79)], "insufficient_data"),
        ([(-120, 20, 80), (-60, 19, 79), (0, 18, 78)], "short_observation"),
        ([(-3600, 20, 80), (-1800, 20, 80), (0, 20, 80)], "flat"),
        ([(-3600, 20, 80), (-1800, 19, 79), (0, None, 78)], "missing_data"),
        ([(-3600, 20, 80), (-1800, 19, 79), (-301, 18, 78)], "stale_data"),
        ([(-3600, 20, 80), (-1800, 19, 79), (0, 30, 78)], "remaining_increased"),
        ([(-3600, 20, 80), (-1800, None, 79), (0, 18, 78)], "missing_data"),
    ],
)
def test_unavailable_forecasts_explain_reason(points, reason):
    result = forecast(points, window="five_hour")
    assert result.status == reason
    assert result.exhaustion_epoch is None


def test_increase_starts_a_new_interval_without_old_consumption():
    result = forecast(
        [(-7200, 90, 90), (-3600, 10, 80), (-1800, 50, 79), (-900, 48, 78), (0, 45, 77)],
        window="five_hour",
    )
    assert result.rate_points_per_hour == 10
    assert result.hours_remaining == 4.5


def test_deduplicates_same_minute_using_latest_sample():
    result = forecast([(-100, 25, 80), (-90, 23, 79), (0, 20, 78)], window="five_hour")
    assert result.status == "insufficient_data"


def test_known_reset_interval_excludes_pre_reset_samples():
    result = forecast(
        [(-3600, 90, 90), (-1800, 80, 80), (0, 70, 70)],
        window="five_hour",
        next_reset_epoch=NOW + 300 * 60 - 600,
    )
    assert result.status == "reset_boundary"


@pytest.mark.parametrize("reset_offset", [10800, 7200])
def test_exhaustion_at_or_after_reset_has_no_exhaustion_date(reset_offset):
    result = forecast(
        [(-7200, 50, 80), (-3600, 40, 78), (0, 30, 76)],
        window="five_hour",
        next_reset_epoch=NOW + reset_offset,
    )
    assert result.status == "reset_before_exhaustion"
    assert result.exhaustion_epoch is None
    assert result.next_reset_epoch == NOW + reset_offset


def test_unknown_reset_still_allows_estimate_and_expired_reset_does_not():
    points = [(-1800, 40, 80), (-900, 30, 78), (0, 20, 76)]
    assert forecast(points, window="five_hour").status == "estimated"
    assert forecast(points, window="five_hour").next_reset_epoch is None
    assert forecast(points, window="five_hour", next_reset_epoch=NOW - 1).status == "reset_boundary"


def test_zero_remaining_is_distinct_from_a_prediction():
    result = forecast([(0, 0, 76)], window="five_hour")
    assert result.status == "empty"
    assert result.remaining_percent == 0
    assert result.exhaustion_epoch is None


def test_old_zero_remaining_does_not_claim_current_exhaustion():
    assert forecast([(-301, 0, 76)], window="five_hour").status == "stale_data"


def test_latest_observation_outside_lookback_is_reported_stale():
    result = forecast([(-43201, 20, 80)], window="five_hour")
    assert result.status == "stale_data"
    assert result.observed_at_epoch == NOW - 43201


def test_presentation_rounds_estimates_and_explains_the_observation_rules():
    from codex_bridge.console.usage_forecast_presentation import format_usage_forecast

    result = forecast([(-3600, 36, 80), (-1800, 30, 79), (0, 23, 78)], window="five_hour")
    text = format_usage_forecast(result, label="5h", lookback_hours=3)
    assert "13.0pt/h" in text
    assert "about 1h 45m" in text
    assert "if the pace over the last 3 hours continues" in text
    assert "Next reset Unknown" in text


def test_zero_observation_before_current_reset_is_not_current_exhaustion():
    result = forecast([(-60, 0, 76)], window="five_hour", next_reset_epoch=NOW + 300 * 60 - 30)
    assert result.status == "reset_boundary"


def test_missing_values_do_not_mix_consumption_across_the_gap():
    result = forecast(
        [(-7200, 90, 90), (-3600, None, 80), (-1800, 50, 79), (-900, 48, 78), (0, 45, 77)],
        window="five_hour",
    )
    assert result.status == "estimated"
    assert result.rate_points_per_hour == 10


def test_forecast_ignores_future_samples():
    result = forecast(
        [(-3600, 40, 80), (-1800, 30, 79), (0, 20, 78), (60, 90, 90)], window="five_hour"
    )
    assert result.remaining_percent == 20
    assert result.hours_remaining == 1
