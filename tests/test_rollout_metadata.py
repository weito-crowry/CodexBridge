from __future__ import annotations

import json
from pathlib import Path

from codex_bridge.rollout_metadata import read_turn_model_metadata


def _write_jsonl(path: Path, *records: object, tail: bytes = b"") -> None:
    path.write_bytes(
        b"".join(
            json.dumps(record, ensure_ascii=False).encode("utf-8") + b"\n" for record in records
        )
        + tail
    )


def _turn_context(turn_id: str, model: object = "gpt-5.6-luna", effort: object = "xhigh") -> dict:
    payload: dict[str, object] = {"turn_id": turn_id, "model": model, "effort": effort}
    return {"type": "turn_context", "payload": payload}


def test_one_candidate_is_resolved(tmp_path: Path) -> None:
    path = tmp_path / "rollout.jsonl"
    _write_jsonl(path, _turn_context("turn-1"))

    assert read_turn_model_metadata(path) == {
        "turn-1": {
            "model_candidates": [{"model": "gpt-5.6-luna", "reasoning_effort": "xhigh"}],
            "model_resolution_status": "resolved",
        }
    }


def test_duplicate_model_effort_pairs_are_deduplicated_in_first_seen_order(tmp_path: Path) -> None:
    path = tmp_path / "rollout.jsonl"
    _write_jsonl(
        path,
        _turn_context("turn-1", "gpt-5.6-luna", "xhigh"),
        _turn_context("turn-1", "gpt-5.6-sol", "high"),
        _turn_context("turn-1", "gpt-5.6-luna", "xhigh"),
        _turn_context("turn-1", "gpt-5.6-sol", "high"),
    )

    assert read_turn_model_metadata(path)["turn-1"] == {
        "model_candidates": [
            {"model": "gpt-5.6-luna", "reasoning_effort": "xhigh"},
            {"model": "gpt-5.6-sol", "reasoning_effort": "high"},
        ],
        "model_resolution_status": "multiple",
    }


def test_same_model_with_different_effort_is_multiple(tmp_path: Path) -> None:
    path = tmp_path / "rollout.jsonl"
    _write_jsonl(
        path,
        _turn_context("turn-1", "gpt-5.6-luna", "xhigh"),
        _turn_context("turn-1", "gpt-5.6-luna", "high"),
    )

    assert read_turn_model_metadata(path)["turn-1"]["model_resolution_status"] == "multiple"


def test_missing_effort_is_a_resolved_pair_with_null_effort(tmp_path: Path) -> None:
    path = tmp_path / "rollout.jsonl"
    _write_jsonl(path, _turn_context("turn-1", effort=None))

    assert read_turn_model_metadata(path)["turn-1"] == {
        "model_candidates": [{"model": "gpt-5.6-luna", "reasoning_effort": None}],
        "model_resolution_status": "resolved",
    }


def test_missing_or_invalid_model_does_not_create_a_candidate(tmp_path: Path) -> None:
    path = tmp_path / "rollout.jsonl"
    _write_jsonl(
        path,
        _turn_context("turn-empty", model=""),
        _turn_context("turn-invalid", model=42),
        _turn_context("turn-valid", model="gpt-5.6-luna", effort=42),
    )

    result = read_turn_model_metadata(path)

    assert result["turn-empty"] == {
        "model_candidates": [],
        "model_resolution_status": "unavailable",
    }
    assert result["turn-invalid"] == {
        "model_candidates": [],
        "model_resolution_status": "unavailable",
    }
    assert result["turn-valid"] == {
        "model_candidates": [{"model": "gpt-5.6-luna", "reasoning_effort": None}],
        "model_resolution_status": "resolved",
    }


def test_non_turn_events_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "rollout.jsonl"
    _write_jsonl(
        path,
        {"type": "event_msg", "payload": {"model": "not-a-candidate"}},
        {"type": "response_item", "payload": {"turn_id": "turn-1", "model": "not-a-candidate"}},
    )

    assert read_turn_model_metadata(path) == {}


def test_malformed_lines_and_incomplete_final_line_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "rollout.jsonl"
    _write_jsonl(path, _turn_context("turn-1"), tail=b'{"type":"turn_context"')
    with path.open("ab") as stream:
        stream.write(b"\n{malformed json}\n")

    assert list(read_turn_model_metadata(path)) == ["turn-1"]


def test_unknown_turn_context_fields_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "rollout.jsonl"
    record = _turn_context("turn-1")
    record["payload"]["future_field"] = {"private": "value"}  # type: ignore[index]
    _write_jsonl(path, record)

    assert read_turn_model_metadata(path)["turn-1"]["model_resolution_status"] == "resolved"


def test_missing_or_unreadable_path_returns_empty_result(tmp_path: Path) -> None:
    assert read_turn_model_metadata(tmp_path / "missing.jsonl") == {}
    assert read_turn_model_metadata(tmp_path) == {}
    assert read_turn_model_metadata(None) == {}


def test_invalid_utf8_line_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "rollout.jsonl"
    path.write_bytes(json.dumps(_turn_context("turn-1")).encode("utf-8") + b"\n\xff\xfe\n")

    assert list(read_turn_model_metadata(path)) == ["turn-1"]
