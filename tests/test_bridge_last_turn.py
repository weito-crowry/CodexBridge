from __future__ import annotations

import json

import pytest

from tests.bridge_test_fakes import make_bridge

pytest_plugins = ("tests.bridge_test_support",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,reason,attention",
    [
        ("completed", "completed", False),
        ("interrupted", "unknown", True),
        ("failed", "turn_failed", True),
        ("inProgress", "unconfirmed_execution", True),
    ],
)
async def test_unloaded_status_restores_last_turn_without_resuming(
    allowed_dir, status, reason, attention
):
    for _ in range(2):
        bridge, app, store = make_bridge(allowed_dir)
        app.thread_cwds["saved"] = str(allowed_dir)
        app.thread_histories["saved"] = [
            {"id": "last", "status": status, "completedAt": 1700000000}
        ]
        result = await bridge.status("saved")
        assert result["state"] == "not_loaded"
        assert result["turn_id"] is None
        assert result["last_turn"]["turn_id"] == "last"
        assert result["last_turn"]["status"] == ("unknown" if status == "inProgress" else status)
        assert result["last_turn"]["reason"] == reason
        assert result["last_turn"]["evidence"] == "persisted"
        assert result["last_turn"]["observed_at"] is not None
        assert result["needs_attention"] is attention
        assert not store.is_loaded("saved")
        assert set(app.methods) == {"thread/read"}


@pytest.mark.asyncio
async def test_empty_or_unavailable_history_is_unknown_without_alert(allowed_dir):
    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_cwds["empty"] = str(allowed_dir)
    for thread_id in ("empty", "unavailable"):
        result = await bridge.status(thread_id)
        assert result["last_turn"]["status"] == "unknown"
        assert result["needs_attention"] is False


@pytest.mark.asyncio
async def test_live_stop_retry_and_disconnect_are_distinct(allowed_dir):
    bridge, app, store = make_bridge(allowed_dir)
    await bridge.start(str(allowed_dir), "prompt")
    bridge.handle_notification(
        {
            "method": "error",
            "params": {
                "threadId": "native-thread",
                "turnId": "native-turn",
                "willRetry": True,
                "error": {"message": "retry"},
            },
        }
    )
    retry = await bridge.status("native-thread")
    assert retry["last_turn"]["status"] == "in_progress"
    assert retry["last_turn"]["reason"] == "retrying"
    assert retry["needs_attention"] is False
    await bridge.interrupt("native-thread", "native-turn")
    bridge.handle_notification(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "native-thread",
                "turn": {"id": "native-turn", "status": "interrupted"},
            },
        }
    )
    stopped = await bridge.status("native-thread")
    assert stopped["last_turn"]["reason"] == "user_stop"
    assert stopped["needs_attention"] is False
    store.ensure_turn("native-thread", "second")
    bridge.handle_app_server_failure("transport closed")
    disconnected = await bridge.status("native-thread")
    assert disconnected["last_turn"]["status"] == "unknown"
    assert disconnected["last_turn"]["reason"] == "connection_lost"
    assert disconnected["last_turn"]["evidence"] == "inferred"
    assert disconnected["needs_attention"] is True
    bridge.handle_notification(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "native-thread",
                "turn": {"id": "second", "status": "completed"},
            },
        }
    )
    assert (await bridge.status("native-thread"))["last_turn"]["status"] == "completed"


@pytest.mark.asyncio
async def test_rollout_recovers_terminal_if_history_read_unavailable(allowed_dir):
    bridge, app, _ = make_bridge(allowed_dir)
    path = allowed_dir / "rollout.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(record)
            for record in [
                {
                    "type": "event_msg",
                    "timestamp": "2026-11-05T11:00:00Z",
                    "payload": {"type": "task_started", "turn_id": "last"},
                },
                {
                    "type": "event_msg",
                    "timestamp": "2026-11-05T11:01:00Z",
                    "payload": {
                        "type": "turn_aborted",
                        "turn_id": "last",
                        "reason": "user_requested",
                    },
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    app.thread_cwds["saved"] = str(allowed_dir)
    app.thread_paths["saved"] = str(path)
    result = await bridge.status("saved")
    assert result["last_turn"]["status"] == "interrupted"
    assert result["last_turn"]["reason"] == "user_stop"
    assert result["needs_attention"] is False


@pytest.mark.asyncio
async def test_paginated_read_uses_latest_without_loading_items(allowed_dir):
    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_cwds["external"] = str(allowed_dir)
    app.thread_history_modes["external"] = "paginated"
    app.turns_response = {"data": [{"id": "new", "status": "interrupted"}]}
    result = await bridge.status("external")
    assert result["last_turn"]["status"] == "interrupted"
    assert app.calls[-1] == (
        "thread/turns/list",
        {"threadId": "external", "limit": 1, "sortDirection": "desc", "itemsView": "notLoaded"},
    )


@pytest.mark.asyncio
async def test_approval_wait_and_selected_old_turn_do_not_replace_latest(allowed_dir):
    from codex_bridge.models import PendingRequest

    bridge, _, store = make_bridge(allowed_dir)
    store.ensure_turn("t", "old")
    store.set_terminal("t", "old", "completed")
    store.ensure_turn("t", "new")
    store.put_pending_request(
        PendingRequest(
            request_id="a",
            method="item/commandExecution/requestApproval",
            thread_id="t",
            turn_id="new",
            item_id="i",
            summary="Approve",
        )
    )
    result = await bridge.status("t", "old")
    assert result["state"] == "completed"
    assert result["last_turn"]["turn_id"] == "new"
    assert result["last_turn"]["reason"] == "needs_approval"
    assert result["needs_attention"] is False


@pytest.mark.asyncio
async def test_persisted_read_cache_avoids_repeated_reads_and_empty_flicker(
    allowed_dir, monkeypatch
):
    import codex_bridge.bridge as module

    clock = [10.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_cwds["t"] = str(allowed_dir)
    app.thread_histories["t"] = [{"id": "last", "status": "interrupted"}]
    first = await bridge.status("t")
    reads = len(app.calls)
    assert (await bridge.status("t"))["last_turn"] == first["last_turn"]
    assert len(app.calls) == reads
    clock[0] = 20
    app.thread_histories["t"] = []
    assert (await bridge.status("t"))["last_turn"] == first["last_turn"]


@pytest.mark.asyncio
async def test_malformed_persisted_status_is_unknown(allowed_dir):
    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_cwds["t"] = str(allowed_dir)
    app.thread_histories["t"] = [{"id": "bad", "status": {"unexpected": True}, "reason": []}]
    result = await bridge.status("t")
    assert result["last_turn"]["status"] == "unknown"


@pytest.mark.asyncio
async def test_status_route_exposes_persisted_result_without_turn_write(allowed_dir):
    from codex_bridge.activity import ActivityStore
    from codex_bridge.ui_api import create_ui_app
    from tests.test_ui_api import _json_response, _request, _route, config

    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_cwds["t"] = str(allowed_dir)
    app.thread_histories["t"] = [{"id": "last", "status": "interrupted"}]
    ui = create_ui_app(bridge, ActivityStore(), config(allowed_dir))
    response = await _route(ui, "/ui-api/threads/{thread_id}/status").endpoint(
        _request("/ui-api/threads/t/status", path_params={"thread_id": "t"})
    )
    payload = await _json_response(response)
    assert payload["state"] == "not_loaded"
    assert payload["last_turn"]["status"] == "interrupted"
    assert payload["needs_attention"] is True
    assert set(app.methods) == {"thread/read"}


@pytest.mark.asyncio
async def test_known_persisted_turn_has_observation_time_without_source_timestamp(allowed_dir):
    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_cwds["t"] = str(allowed_dir)
    app.thread_histories["t"] = [{"id": "last", "status": "completed"}]
    result = await bridge.status("t")
    assert result["last_turn"]["observed_at"] is not None


@pytest.mark.asyncio
async def test_unknown_selected_turn_keeps_last_live_outcome(allowed_dir):
    bridge, _, store = make_bridge(allowed_dir)
    store.ensure_turn("t", "last")
    store.set_terminal("t", "last", "completed")
    result = await bridge.status("t", "missing")
    assert result["state"] == "not_loaded"
    assert result["last_turn"]["turn_id"] == "last"
    assert result["last_turn"]["status"] == "completed"
