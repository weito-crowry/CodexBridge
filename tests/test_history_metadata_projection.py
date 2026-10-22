from __future__ import annotations

import json
from copy import deepcopy

from codex_bridge import history_metadata_projection
from codex_bridge.history_metadata_projection import attach_turn_execution_metadata


def test_attaches_normalized_model_effort_and_access_metadata(tmp_path) -> None:
    rollout_path = tmp_path / "rollout.jsonl"
    records = (
        {
            "type": "turn_context",
            "payload": {
                "turn_id": "turn-1",
                "model": "gpt-5.6-luna",
                "effort": "xhigh",
                "sandbox_policy": {"type": "dangerFullAccess"},
            },
        },
        {
            "type": "turn_context",
            "payload": {
                "turn_id": "turn-1",
                "model": "gpt-5.6-luna",
                "effort": "xhigh",
                "sandbox_policy": {"type": "dangerFullAccess"},
            },
        },
        {
            "type": "turn_context",
            "payload": {
                "turn_id": "turn-1",
                "model": "gpt-5.6-sol",
                "effort": "high",
                "sandbox_policy": {"type": "workspaceWrite"},
            },
        },
    )
    rollout_path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8"
    )
    items = [{"turn_id": "turn-1", "item": {"id": "agent-1", "text": "hello"}}]
    result = {"items": items}

    projected = attach_turn_execution_metadata(result, {"thread": {"path": str(rollout_path)}})

    assert projected["turn_model_metadata"] == {
        "turn-1": {
            "model_candidates": [
                {"model": "gpt-5.6-luna", "reasoning_effort": "xhigh"},
                {"model": "gpt-5.6-sol", "reasoning_effort": "high"},
            ],
            "model_resolution_status": "multiple",
        }
    }
    assert projected["turn_access_metadata"] == {
        "turn-1": {
            "access_candidates": ["danger-full-access", "workspace-write"],
            "access_resolution_status": "multiple",
        }
    }


def test_duplicate_items_share_one_turn_metadata_entry() -> None:
    result = {
        "items": [
            {"turn_id": "turn-1", "item": {"id": "item-1"}},
            {"turn_id": "turn-1", "item": {"id": "item-2"}},
        ]
    }

    attach_turn_execution_metadata(result, {"thread": {"path": object()}})

    assert result["turn_model_metadata"] == {
        "turn-1": {"model_candidates": [], "model_resolution_status": "unavailable"}
    }
    assert result["turn_access_metadata"] == {
        "turn-1": {"access_candidates": [], "access_resolution_status": "unavailable"}
    }


def test_missing_rollout_returns_unavailable_metadata(tmp_path) -> None:
    result = {"items": [{"turn_id": "turn-1", "item": {"id": "item-1"}}]}

    attach_turn_execution_metadata(
        result, {"thread": {"path": str(tmp_path / "missing-rollout.jsonl")}}
    )

    assert result["turn_model_metadata"]["turn-1"] == {
        "model_candidates": [],
        "model_resolution_status": "unavailable",
    }
    assert result["turn_access_metadata"]["turn-1"] == {
        "access_candidates": [],
        "access_resolution_status": "unavailable",
    }


def test_invalid_thread_path_returns_unavailable_metadata() -> None:
    result = {"items": [{"turn_id": "turn-1", "item": {"id": "item-1"}}]}

    attach_turn_execution_metadata(result, {"thread": {"path": object()}})

    assert result["turn_model_metadata"]["turn-1"]["model_resolution_status"] == "unavailable"
    assert result["turn_access_metadata"]["turn-1"]["access_resolution_status"] == "unavailable"


def test_rollout_read_failure_returns_unavailable_metadata(monkeypatch) -> None:
    def fail_read(_path: object) -> dict[str, object]:
        raise OSError("rollout unavailable")

    monkeypatch.setattr(history_metadata_projection, "read_turn_execution_metadata", fail_read)
    result = {"items": [{"turn_id": "turn-1", "item": {"id": "item-1"}}]}

    attach_turn_execution_metadata(result, {"thread": {"path": "rollout.jsonl"}})

    assert result["turn_model_metadata"]["turn-1"]["model_resolution_status"] == "unavailable"
    assert result["turn_access_metadata"]["turn-1"]["access_resolution_status"] == "unavailable"


def test_projection_preserves_original_items() -> None:
    items = [
        {"turn_id": "turn-1", "item": {"id": "item-1", "text": "unchanged"}},
        {"turn_id": "turn-2", "item": {"id": "item-2", "text": "also unchanged"}},
    ]
    original_items = deepcopy(items)
    result = {"items": items}

    projected = attach_turn_execution_metadata(result, {"thread": {"path": object()}})

    assert projected is result
    assert result["items"] == original_items
