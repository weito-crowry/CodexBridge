"""Forecast text is separate from calculation statuses for UI translation."""

from __future__ import annotations

from datetime import datetime

from .usage import UsageWindow
from .usage_forecast import ForecastStatus, UsageForecast

_REASON_TEXT = {
    ForecastStatus.INSUFFICIENT_DATA: "データ不足（異なる分の観測が3点必要）",
    ForecastStatus.SHORT_OBSERVATION: "観測時間が短い（15分以上必要）",
    ForecastStatus.MISSING_DATA: "欠損値後のデータ不足",
    ForecastStatus.STALE_DATA: "最新の観測が古い（5分超）",
    ForecastStatus.FLAT: "残量が横ばい",
    ForecastStatus.REMAINING_INCREASED: "残量増加後のデータ不足",
    ForecastStatus.RESET_BOUNDARY: "リセット後のデータ不足",
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
    remaining = "不明" if forecast.remaining_percent is None else f"{forecast.remaining_percent}%"
    observed = (
        "不明"
        if forecast.observed_at_epoch is None
        else datetime.fromtimestamp(forecast.observed_at_epoch).strftime("%Y/%m/%d %H:%M")
    )
    reset = (
        "不明"
        if forecast.next_reset_epoch is None
        else datetime.fromtimestamp(forecast.next_reset_epoch).strftime("%Y/%m/%d %H:%M")
    )
    parts = [f"{label}: 残量 {remaining}（観測 {observed}）"]
    if forecast.rate_points_per_hour is not None:
        rate = forecast.rate_points_per_hour
        rate_text = "0.1未満" if rate < 0.1 else f"{rate:.1f}"
        parts.append(f"直近{lookback_hours}時間の減少 約{rate_text}pt/h")
    if forecast.status == ForecastStatus.EMPTY:
        parts.append("残量0%")
    elif forecast.status == ForecastStatus.RESET_BEFORE_EXHAUSTION:
        parts.append("次回リセットまでの枯渇は予測されない")
    elif forecast.hours_remaining is not None and forecast.exhaustion_epoch is not None:
        # Minute-resolution, integer percentages do not justify precise seconds.
        minutes = max(0, round(forecast.hours_remaining * 60 / 5) * 5)
        parts.append(f"推定残り（観測時点から）約{minutes // 60}時間{minutes % 60:02d}分")
        exhaustion = datetime.fromtimestamp(round(forecast.exhaustion_epoch / 300) * 300)
        parts.append(f"推定枯渇 {exhaustion:%Y/%m/%d %H:%M}頃")
    else:
        parts.append(f"算出不可: {_REASON_TEXT[forecast.status]}")
    parts.append(f"次回リセット {reset}")
    parts.append(f"参考推定: 直近{lookback_hours}時間のペースが継続した場合")
    return " / ".join(parts)
