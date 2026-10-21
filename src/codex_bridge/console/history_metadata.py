from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

_MAX_TOOLTIP_TURNS = 8
_MAX_TOOLTIP_CHARS = 1200
_MAX_TOOLTIP_VALUE_CHARS = 96
_MAX_METADATA_VALUE_CHARS = 512


@dataclass(frozen=True, slots=True)
class HistoryModelSummary:
    model: str | None
    effort: str | None
    model_tooltip: str
    effort_tooltip: str


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


def summarize_turn_model_metadata(
    turn_ids: Iterable[str], turn_model_metadata: Mapping[str, object]
) -> HistoryModelSummary:
    """Summarize metadata only for turns represented in the loaded timeline."""
    parsed = _turn_model_metadata_from_payload({"turn_model_metadata": turn_model_metadata})
    ordered_turn_ids = tuple(dict.fromkeys(turn_id for turn_id in turn_ids if turn_id))
    records: list[tuple[str, str, tuple[tuple[str, str | None], ...]]] = []
    models: set[str] = set()
    efforts: set[str] = set()
    partial = False

    for turn_id in ordered_turn_ids:
        entry = parsed.get(turn_id)
        status = entry.get("model_resolution_status") if entry is not None else "unavailable"
        status = status if isinstance(status, str) else "unavailable"
        raw_candidates = entry.get("model_candidates") if entry is not None else None
        candidates: list[tuple[str, str | None]] = []
        if isinstance(raw_candidates, list) and status != "unavailable":
            for raw_candidate in raw_candidates:
                if not isinstance(raw_candidate, Mapping):
                    continue
                model = _single_line(raw_candidate.get("model"))
                if model is None:
                    continue
                effort = _single_line(raw_candidate.get("reasoning_effort"))
                candidates.append((model, effort))
                models.add(model)
                if effort is not None:
                    efforts.add(effort)
                else:
                    partial = True
        if not candidates:
            partial = True
        records.append((turn_id, status, tuple(candidates)))

    model = _single_observed_value(models)
    effort = _single_observed_value(efforts)
    return HistoryModelSummary(
        model=model,
        effort=effort,
        model_tooltip=_history_tooltip(records, partial=partial),
        effort_tooltip=_history_tooltip(records, partial=partial),
    )


def _single_observed_value(values: set[str]) -> str | None:
    if len(values) > 1:
        return "Multiple"
    return next(iter(values), None)


def _single_line(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())[:_MAX_METADATA_VALUE_CHARS]
    return cleaned or None


def _history_tooltip(
    records: list[tuple[str, str, tuple[tuple[str, str | None], ...]]], *, partial: bool
) -> str:
    lines = ["読み込んだ履歴から観測した情報です。現在の設定とは限りません。"]
    if partial:
        lines.append("一部のturnまたはeffort情報を取得できませんでした。")
    shown_records = list(reversed(records[-_MAX_TOOLTIP_TURNS:]))
    for turn_id, status, candidates in shown_records:
        safe_turn_id = _single_line(turn_id) or "Unknown turn"
        safe_turn_id = _tooltip_value(safe_turn_id)
        if candidates:
            values = "; ".join(
                _format_tooltip_candidate(model, effort) for model, effort in candidates[:6]
            )
            if len(candidates) > 6:
                values += "; …"
            lines.append(f"{safe_turn_id} [{status}]: {values}")
        else:
            lines.append(f"{safe_turn_id} [{status}]")
    omitted = len(records) - len(shown_records)
    if omitted:
        lines.append(f"ほか{omitted}件のturnを省略しました。")
    tooltip = "\n".join(lines)
    if len(tooltip) > _MAX_TOOLTIP_CHARS:
        tooltip = tooltip[: _MAX_TOOLTIP_CHARS - 12].rstrip() + "\n…省略…"
    return tooltip


def _tooltip_value(value: str) -> str:
    if len(value) <= _MAX_TOOLTIP_VALUE_CHARS:
        return value
    return value[: _MAX_TOOLTIP_VALUE_CHARS - 1] + "…"


def _format_tooltip_candidate(model: str, effort: str | None) -> str:
    effort_text = _tooltip_value(effort) if effort is not None else "effort: Unknown"
    return f"{_tooltip_value(model)} ({effort_text})"
