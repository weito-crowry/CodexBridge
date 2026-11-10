"""Transient quota forecasts from the existing minute history; no Qt or I/O."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Literal

from .usage_history import UsageHistorySample

UsageForecastWindow = Literal["five_hour", "weekly"]
FORECAST_LOOKBACK_HOURS = (1, 3, 6, 12)
MIN_OBSERVATION_SECONDS = 15 * 60
MAX_SAMPLE_AGE_SECONDS = 5 * 60


class ForecastStatus(StrEnum):
    ESTIMATED = "estimated"
    EMPTY = "empty"
    INSUFFICIENT_DATA = "insufficient_data"
    SHORT_OBSERVATION = "short_observation"
    MISSING_DATA = "missing_data"
    STALE_DATA = "stale_data"
    FLAT = "flat"
    REMAINING_INCREASED = "remaining_increased"
    RESET_BOUNDARY = "reset_boundary"
    RESET_BEFORE_EXHAUSTION = "reset_before_exhaustion"


@dataclass(frozen=True, slots=True)
class UsageForecast:
    status: ForecastStatus
    remaining_percent: int | None = None
    observed_at_epoch: float | None = None
    next_reset_epoch: float | None = None
    rate_points_per_hour: float | None = None
    hours_remaining: float | None = None
    exhaustion_epoch: float | None = None


def estimate_exhaustion(
    samples: Iterable[UsageHistorySample],
    *,
    window: UsageForecastWindow,
    now_epoch: float,
    lookback_hours: int = 3,
    next_reset_epoch: float | None = None,
) -> UsageForecast:
    """Use endpoint drop / elapsed hours within the latest uninterrupted interval.

    The DB stores one integer percentage per minute. Require three distinct
    minutes spanning at least 15 minutes; observations older than five polling
    intervals (five minutes) cannot describe the current quota. Missing values,
    increases and known reset boundaries begin a new observation interval.
    """
    if lookback_hours not in FORECAST_LOOKBACK_HOURS:
        raise ValueError("lookback_hours must be 1, 3, 6 or 12")
    start = now_epoch - lookback_hours * 3600
    by_minute: dict[int, UsageHistorySample] = {}
    for sample in sorted(samples, key=lambda item: item.captured_at_epoch):
        if sample.captured_at_epoch <= now_epoch:
            by_minute[int(sample.captured_at_epoch // 60)] = sample
    recent = sorted(by_minute.values(), key=lambda item: item.captured_at_epoch)
    result = UsageForecast(ForecastStatus.INSUFFICIENT_DATA, next_reset_epoch=next_reset_epoch)
    if not recent:
        return result

    def remaining(sample: UsageHistorySample) -> int | None:
        return sample.five_hour_remaining if window == "five_hour" else sample.weekly_remaining

    latest = recent[-1]
    value = remaining(latest)
    result = replace(result, remaining_percent=value, observed_at_epoch=latest.captured_at_epoch)
    if now_epoch - latest.captured_at_epoch > MAX_SAMPLE_AGE_SECONDS:
        return replace(result, status=ForecastStatus.STALE_DATA)
    if value is None:
        return replace(result, status=ForecastStatus.MISSING_DATA)
    duration = 300 * 60 if window == "five_hour" else 10080 * 60
    if next_reset_epoch is not None and (
        next_reset_epoch <= now_epoch or latest.captured_at_epoch < next_reset_epoch - duration
    ):
        return replace(result, status=ForecastStatus.RESET_BOUNDARY)
    if value == 0:
        return replace(result, status=ForecastStatus.EMPTY)

    recent = [item for item in recent if item.captured_at_epoch >= start]
    boundary_reason = ForecastStatus.INSUFFICIENT_DATA
    if next_reset_epoch is not None:
        interval_start = next_reset_epoch - duration
        current = [item for item in recent if item.captured_at_epoch >= interval_start]
        if len(current) < len(recent):
            boundary_reason = ForecastStatus.RESET_BOUNDARY
        recent = current

    interval: list[tuple[float, int]] = []
    for sample in recent:
        value_at_sample = remaining(sample)
        if value_at_sample is None:
            interval = []
            boundary_reason = ForecastStatus.MISSING_DATA
            continue
        if interval and value_at_sample > interval[-1][1]:
            interval = []
            boundary_reason = ForecastStatus.REMAINING_INCREASED
        interval.append((sample.captured_at_epoch, value_at_sample))
    if len(interval) < 3:
        return replace(result, status=boundary_reason)
    span = interval[-1][0] - interval[0][0]
    if span < MIN_OBSERVATION_SECONDS:
        return replace(result, status=ForecastStatus.SHORT_OBSERVATION)
    rate = (interval[0][1] - interval[-1][1]) / (span / 3600)
    if rate <= 0:
        return replace(result, status=ForecastStatus.FLAT)
    hours = value / rate
    exhaustion = latest.captured_at_epoch + hours * 3600
    result = replace(result, rate_points_per_hour=rate)
    if next_reset_epoch is not None and exhaustion >= next_reset_epoch:
        return replace(result, status=ForecastStatus.RESET_BEFORE_EXHAUSTION)
    return replace(
        result,
        status=ForecastStatus.ESTIMATED,
        hours_remaining=hours,
        exhaustion_epoch=exhaustion,
    )
