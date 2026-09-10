from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import TypeGuard


@dataclass(frozen=True, slots=True)
class UsageWindow:
    duration_minutes: int
    remaining_percent: int
    reset_label: str


@dataclass(frozen=True, slots=True)
class CodexUsage:
    five_hour: UsageWindow | None = None
    weekly: UsageWindow | None = None


def _window_mappings(value: object, *, depth: int = 0) -> Iterator[Mapping[str, object]]:
    if depth > 8:
        return
    if isinstance(value, Mapping):
        if "windowDurationMins" in value and "usedPercent" in value:
            yield value
        for child in value.values():
            yield from _window_mappings(child, depth=depth + 1)
    elif isinstance(value, list):
        for child in value:
            yield from _window_mappings(child, depth=depth + 1)


def _is_number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _remaining_percent(value: object) -> int | None:
    if not _is_number(value) or not isfinite(float(value)):
        return None
    return int(round(max(0.0, min(100.0, 100.0 - float(value)))))


def _reset_label(value: object) -> str:
    if _is_number(value):
        timestamp = float(value)
        if timestamp > 100_000_000_000:
            timestamp /= 1_000
        try:
            return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
        except (OverflowError, OSError, ValueError):
            return "unavailable"
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value[:64]
        return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    return "unavailable"


def _make_window(value: Mapping[str, object]) -> UsageWindow | None:
    duration = value.get("windowDurationMins")
    remaining = _remaining_percent(value.get("usedPercent"))
    if not isinstance(duration, int) or isinstance(duration, bool) or remaining is None:
        return None
    if duration not in {300, 10080}:
        return None
    return UsageWindow(duration, remaining, _reset_label(value.get("resetsAt")))


def parse_codex_usage(payload: object) -> CodexUsage:
    if not isinstance(payload, Mapping):
        return CodexUsage()

    by_limit_id = payload.get("rateLimitsByLimitId")
    codex_limits = by_limit_id.get("codex") if isinstance(by_limit_id, Mapping) else None
    if isinstance(codex_limits, (Mapping, list)):
        source: object = codex_limits
    else:
        source = payload.get("rateLimits")
        if not isinstance(source, (Mapping, list)):
            source = payload

    windows: dict[int, UsageWindow] = {}
    for raw_window in _window_mappings(source):
        window = _make_window(raw_window)
        if window is not None and window.duration_minutes not in windows:
            windows[window.duration_minutes] = window
    return CodexUsage(windows.get(300), windows.get(10080))


def _window_text(window: UsageWindow | None) -> str:
    return f"{window.remaining_percent}%" if window is not None else "—"


def format_codex_usage(usage: CodexUsage) -> str:
    if usage.five_hour is None and usage.weekly is None:
        return "Codex Usage  unavailable"
    return f"Codex Usage  5h {_window_text(usage.five_hour)} · Week {_window_text(usage.weekly)}"


def format_codex_usage_tooltip(usage: CodexUsage) -> str:
    if usage.five_hour is None and usage.weekly is None:
        return "Codex Usage unavailable"
    details: list[str] = []
    for label, window in (("5h", usage.five_hour), ("Week", usage.weekly)):
        if window is None:
            details.append(f"{label}: unavailable")
        else:
            details.append(
                f"{label}: {window.remaining_percent}% left · reset {window.reset_label}"
            )
    return "\n".join(details)


def format_codex_usage_detail(usage: CodexUsage) -> str:
    if usage.five_hour is None and usage.weekly is None:
        return "Unavailable"
    return format_codex_usage_tooltip(usage)


def usage_level(usage: CodexUsage) -> str:
    remaining = [
        window.remaining_percent for window in (usage.five_hour, usage.weekly) if window is not None
    ]
    if not remaining:
        return "normal"
    minimum = min(remaining)
    if minimum < 10:
        return "error"
    if minimum <= 20:
        return "warning"
    return "normal"
