from __future__ import annotations

import asyncio
import json

import pytest

from codex_bridge.activity import ActivityStore
from codex_bridge.console.sse import SseParser, parse_activity_event
from codex_bridge.ui_api import create_ui_app
from tests.bridge_test_fakes import make_bridge
from tests.test_ui_api import _request, _route, config


@pytest.mark.asyncio
async def test_existing_global_sse_delivers_initial_and_changed_cached_snapshot(tmp_path):
    bridge, app_server, _ = make_bridge(tmp_path)
    activity_store = ActivityStore()
    app = create_ui_app(bridge, activity_store, config(tmp_path))
    response = await _route(app, "/ui-api/events").endpoint(_request("/ui-api/events"))
    first = await asyncio.wait_for(response.body_iterator.__anext__(), 0.2)
    event = parse_activity_event(SseParser().feed(first)[0])
    assert event == {
        "type": "external_threads",
        "state": "unavailable",
        "threads": {},
        "observed_at": None,
    }
    # Cached snapshot delivery has no App Server calls or second scanner.
    assert app_server.methods == []
    await response.body_iterator.aclose()


@pytest.mark.asyncio
async def test_global_sse_initializing_scan_changes_and_reconnect_use_one_scanner(tmp_path):
    from codex_bridge.external_sessions import CodexSessionScanner, ExternalExecutionMonitor

    home = tmp_path / "home"
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    path = sessions / "external.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"cwd": str(tmp_path), "originator": "codex_desktop"},
            }
        )
        + "\n"
        + json.dumps({"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t"}})
        + "\n",
        encoding="utf-8",
    )
    monitor = ExternalExecutionMonitor(
        CodexSessionScanner(allowed_roots=(str(tmp_path),), codex_home=home), interval=0.01
    )
    bridge, app_server, _ = make_bridge(tmp_path)
    bridge.observations.external = monitor
    app = create_ui_app(bridge, ActivityStore(), config(tmp_path))
    route = _route(app, "/ui-api/events")
    response = await route.endpoint(_request("/ui-api/events"))

    async def next_snapshot(response):
        async with asyncio.timeout(1.5):
            while True:
                chunk = await response.body_iterator.__anext__()
                frames = SseParser().feed(chunk)
                if frames:
                    return parse_activity_event(frames[0])

    initial = await next_snapshot(response)
    assert initial["state"] == "initializing" and initial["threads"] == {}
    await monitor.start()
    try:
        started = await next_snapshot(response)
        assert started["state"] == "ready"
        assert set(started["threads"]) == {"external"}
        await response.body_iterator.aclose()
        # A new global stream receives the existing cache immediately, without rescanning.
        response = await route.endpoint(_request("/ui-api/events"))
        restored = await next_snapshot(response)
        assert restored["threads"] == started["threads"]
        with path.open("a", encoding="utf-8") as output:
            output.write(
                json.dumps(
                    {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t"}}
                )
                + "\n"
            )
        ended = await next_snapshot(response)
        assert ended["state"] == "ready" and ended["threads"] == {}
        assert app_server.methods == []
    finally:
        await response.body_iterator.aclose()
        await monitor.close()


def test_console_accepts_snapshot_after_empty_http_and_clears_when_disconnected(tmp_path):
    from tests.console_main_window_test_support import FakeClient, _application
    from tests.test_console_api_result_dispatcher import _window

    _application()
    client = FakeClient()
    window = _window(client)
    generation = window._global_stream_generation
    try:
        window.apply_json_result("threads", {"threads": []})
        window._apply_global_activity(
            generation,
            {
                "type": "external_threads",
                "state": "ready",
                "threads": {
                    "external": {
                        "id": "external",
                        "cwd": str(tmp_path),
                        "originator": "codex_desktop",
                    }
                },
            },
        )
        assert window.thread_pane.thread_count == 1
        assert "external" in window._external_running_threads
        window._apply_global_stream_state(generation, "disconnected")
        assert window._external_running_threads == {}
        assert window.thread_pane.thread_count == 0
        assert window._external_thread_monitor is None
    finally:
        window.close()


def test_sse_snapshot_is_bounded_to_safe_thread_fields():
    payload = {
        "state": "ready",
        "observed_at": None,
        "threads": {"ext": {"id": "ext", "cwd": "C:/allowed", "raw": "secret"}},
    }
    frame = ("event: external_threads\ndata: " + json.dumps(payload) + "\n\n").encode()
    event = parse_activity_event(SseParser().feed(frame)[0])
    assert event["threads"] == {"ext": {"id": "ext", "cwd": "C:/allowed"}}
