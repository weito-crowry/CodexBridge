from __future__ import annotations

import asyncio
import json

import pytest

from tests.bridge_test_fakes import make_bridge


@pytest.mark.asyncio
async def test_list_projects_live_and_cached_outcomes_without_history_reads(tmp_path):
    bridge, app, store = make_bridge(tmp_path)
    for thread_id in ("managed", "saved", "external"):
        app.thread_cwds[thread_id] = str(tmp_path)
        app.thread_list.append({"id": thread_id, "cwd": str(tmp_path), "name": thread_id})
    app.thread_histories["saved"] = [{"id": "done", "status": "completed"}]
    await bridge.status("saved")
    store.mark_loaded("managed", str(tmp_path))
    store.ensure_turn("managed", "live")
    app.methods.clear()
    result = await bridge.threads()
    rows = {row["id"]: row for row in result["threads"]}
    assert rows["managed"]["current_execution"]["state"] == "in_progress"
    assert rows["managed"]["current_execution"]["evidence"] == "live"
    assert rows["saved"]["last_turn"]["status"] == "completed"
    assert rows["external"]["current_execution"]["state"] == "unknown"
    assert rows["external"]["last_turn"]["status"] == "unknown"
    assert app.methods == ["thread/list"]


@pytest.mark.asyncio
async def test_shared_scanner_estimates_remain_separate_and_allowed_root_filtered(tmp_path):
    from codex_bridge.external_sessions import CodexSessionScanner, ExternalExecutionMonitor

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    home = tmp_path / "home"
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    for thread_id, cwd, originator in (
        ("external", allowed, "codex_desktop"),
        ("managed", allowed, "codex_desktop"),
        ("outside", outside, "codex_desktop"),
        ("bridge-created", allowed, "codexbridge"),
    ):
        records = [
            {"type": "session_meta", "payload": {"cwd": str(cwd), "originator": originator}},
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn"}},
        ]
        (sessions / f"{thread_id}.jsonl").write_text(
            "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
        )
    monitor = ExternalExecutionMonitor(
        CodexSessionScanner(allowed_roots=(str(allowed),), codex_home=home), interval=0.01
    )
    bridge, app, store = make_bridge(allowed)
    bridge.observations.external = monitor
    app.thread_cwds["external"] = str(allowed)
    assert monitor.snapshot()["state"] == "initializing"
    await monitor.start()
    try:
        async with asyncio.timeout(3):
            while monitor.snapshot()["state"] == "initializing":
                await asyncio.sleep(0.005)
        status = await bridge.status("external")
        assert status["state"] == "not_loaded"
        assert status["current_execution"]["state"] == "estimated_running"
        assert status["current_execution"]["evidence"] == "inferred"
        assert status["last_turn"]["status"] == "unknown"
        store.mark_loaded("managed", str(allowed))
        store.ensure_turn("managed", "live")
        result = await bridge.threads()
        external = {row["id"]: row for row in result["external_threads"]}
        assert set(external) == {"external"}  # live managed state takes priority
        assert result["external_execution"]["observed_at"] is not None
        assert "turn/start" not in app.methods
        # Fresh scan evidence never upgrades the unmatched-start heuristic to live certainty.
        from codex_bridge import external_sessions

        before = external_sessions.time.monotonic()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(external_sessions.time, "monotonic", lambda: before + 10)
            stale = await bridge.threads()
            assert stale["external_execution"]["state"] == "stale"
            assert stale["external_threads"] == []
            assert bridge.observations.current("external")["state"] == "unknown"
    finally:
        await monitor.close()
    assert monitor.snapshot()["state"] == "unavailable"
    assert (await bridge.status("external"))["current_execution"]["state"] == "unknown"


def test_external_thread_ids_keep_remote_target_affinity():
    from codex_bridge.execution_targets import ExecutionTargetRouter
    from tests.test_execution_targets import LOCAL, NOTEBOOK, FakeBridge

    router = ExecutionTargetRouter((LOCAL, NOTEBOOK), FakeBridge)
    result = router._rewrite_result(
        NOTEBOOK, {"threads": [], "external_threads": [{"id": "desktop"}]}
    )
    assert result["external_threads"][0]["id"] == "notebook::desktop"
    assert result["external_threads"][0]["target_id"] == "notebook"
