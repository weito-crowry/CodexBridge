"""Forecast text is separate from calculation statuses for UI translation."""

from __future__ import annotations

from datetime import datetime

from .i18n import tr
from .usage import UsageWindow
from .usage_forecast import ForecastStatus, UsageForecast

_REASON_TEXT = {
    ForecastStatus.INSUFFICIENT_DATA: (
        "Insufficient data (3 observations in different minutes required)"
    ),
    ForecastStatus.SHORT_OBSERVATION: "Short observation period (at least 15 minutes required)",
    ForecastStatus.MISSING_DATA: "Insufficient data after a missing value",
    ForecastStatus.STALE_DATA: "Latest observation is stale (over 5 minutes)",
    ForecastStatus.FLAT: "Remaining quota is flat",
    ForecastStatus.REMAINING_INCREASED: "Insufficient data after a quota increase",
    ForecastStatus.RESET_BOUNDARY: "Insufficient data after reset",
}


def reset_epoch(window: UsageWindow | None) -> float | None:
    """Read only the normalized local timestamp produced by usage._reset_label."""
    if window is None:
        return None
    try:
        return datetime.strptime(window.reset_label, "%Y-%m-%d %H:%M:%S").timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def format_usage_forecast(forecast: UsageForecast, *, label: str, lookback_hours: int) -> str:
    remaining = (
        tr("Unknown") if forecast.remaining_percent is None else f"{forecast.remaining_percent}%"
    )
    observed = (
        tr("Unknown")
        if forecast.observed_at_epoch is None
        else datetime.fromtimestamp(forecast.observed_at_epoch).strftime("%Y/%m/%d %H:%M")
    )
    reset = (
        tr("Unknown")
        if forecast.next_reset_epoch is None
        else datetime.fromtimestamp(forecast.next_reset_epoch).strftime("%Y/%m/%d %H:%M")
    )
    parts = [tr("{p0}: remaining {p1} (observed {p2})", p0=label, p1=remaining, p2=observed)]
    if forecast.rate_points_per_hour is not None:
        rate = forecast.rate_points_per_hour
        rate_text = tr("below 0.1") if rate < 0.1 else f"{rate:.1f}"
        parts.append(
            tr("Decrease over the last {p0} hours: about {p1}pt/h", p0=lookback_hours, p1=rate_text)
        )
    if forecast.status == ForecastStatus.EMPTY:
        parts.append(tr("0% remaining"))
    elif forecast.status == ForecastStatus.RESET_BEFORE_EXHAUSTION:
        parts.append(tr("No exhaustion predicted before the next reset"))
    elif forecast.hours_remaining is not None and forecast.exhaustion_epoch is not None:
        # Minute-resolution, integer percentages do not justify precise seconds.
        minutes = max(0, round(forecast.hours_remaining * 60 / 5) * 5)
        parts.append(
            tr(
                "Estimated time remaining from observation: about {p0}h {p1:02d}m",
                p0=minutes // 60,
                p1=minutes % 60,
            )
        )
        exhaustion = datetime.fromtimestamp(round(forecast.exhaustion_epoch / 300) * 300)
        parts.append(tr("Estimated exhaustion around {p0:%Y/%m/%d %H:%M}", p0=exhaustion))
    else:
        parts.append(tr("Unavailable: {p0}", p0=tr(_REASON_TEXT[forecast.status])))
    parts.append(tr("Next reset {p0}", p0=reset))
    parts.append(
        tr("Reference estimate: if the pace over the last {p0} hours continues", p0=lookback_hours)
    )
    return " / ".join(parts)
