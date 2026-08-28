from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

from codex_bridge.activity import ActivityStore
from codex_bridge.app_server import AppServerClient
from codex_bridge.bridge import Bridge
from codex_bridge.config import BridgeConfig
from codex_bridge.logging_utils import configure_logging
from codex_bridge.paths import AllowedPathPolicy
from codex_bridge.state import StateStore
from codex_bridge.ui_api import create_ui_app
from codex_bridge.ui_server import LocalUiServer


async def _wait_until_terminal(bridge: Bridge, thread_id: str, turn_id: str) -> dict[str, object]:
    for _ in range(8):
        result = await bridge.wait(thread_id, turn_id, 15.0)
        if result["state"] != "in_progress":
            return result
    return await bridge.wait(thread_id, turn_id, 0.0)


async def _resolve_safe_file_approval(
    bridge: Bridge, result: dict[str, object], thread_id: str, turn_id: str
) -> dict[str, object]:
    if result["state"] != "needs_approval":
        return result
    pending = result.get("pending_request")
    if not isinstance(pending, dict) or pending.get("method") != "item/fileChange/requestApproval":
        print("approval: unsupported request left unresolved", file=sys.stderr)
        return result
    request_id = pending.get("request_id")
    if not isinstance(request_id, (int, str)):
        print("approval: malformed request left unresolved", file=sys.stderr)
        return result
    print("approval: accepting the temporary-workspace file change")
    await bridge.approve(request_id, "accept")
    return await _wait_until_terminal(bridge, thread_id, turn_id)


async def _get_json(port: int, path: str) -> dict[str, object]:
    def request() -> dict[str, object]:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as response:
            payload = response.read()
            if response.status != 200:
                raise RuntimeError(f"GET {path} returned HTTP {response.status}")
            result = json.loads(payload.decode("utf-8"))
            if not isinstance(result, dict):
                raise RuntimeError(f"GET {path} returned a non-object response")
            return result

    return await asyncio.to_thread(request)


async def _read_sse_event(port: int) -> str:
    def request() -> str:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/ui-api/events", timeout=15
        ) as response:
            chunks: list[bytes] = []
            while len(b"".join(chunks)) < 64 * 1024:
                line = response.readline()
                if not line:
                    break
                chunks.append(line)
                if line == b"\n" and chunks:
                    break
            return b"".join(chunks).decode("utf-8", errors="replace")

    return await asyncio.to_thread(request)


async def _run_smoke() -> int:
    configure_logging()
    executable = os.environ.get("CODEX_BRIDGE_CODEX_EXECUTABLE", "codex")
    with tempfile.TemporaryDirectory(prefix="codexbridge-smoke-") as temporary:
        root = Path(temporary)
        target = root / "codexbridge_smoke.txt"
        policy = AllowedPathPolicy((str(root),))

        first_app = AppServerClient(executable)
        activity_store = ActivityStore()
        first_bridge = Bridge(first_app, StateStore(), policy, activity_store=activity_store)
        first_app.set_handlers(
            on_notification=first_bridge.handle_notification,
            on_server_request=first_bridge.handle_server_request,
        )
        ui_config = BridgeConfig(
            host="127.0.0.1",
            port=8000,
            ui_port=8001,
            allowed_roots=(str(root),),
            allowed_hosts=(),
            allowed_origins=(),
            codex_executable=executable,
            wait_default_seconds=18.0,
            wait_max_seconds=30.0,
            shutdown_grace_seconds=3.0,
        )
        ui_server = LocalUiServer(create_ui_app(first_bridge, activity_store, ui_config), 8001)
        await first_app.start()
        try:
            await ui_server.start()
            health = await _get_json(8001, "/healthz")
            ui_status = await _get_json(8001, "/ui-api/status")
            print(f"ui health: {health.get('status')}")
            print(f"ui status: {ui_status.get('app_server')}:{ui_status.get('ui_port')}")
            if health != {"status": "ok"} or ui_status.get("app_server") != "ready":
                print("ui startup check failed", file=sys.stderr)
                return 1
            started = await first_bridge.start(
                str(root),
                (
                    "Create codexbridge_smoke.txt in the current workspace with exactly "
                    "SMOKE-ONE on one line. Do not modify anything else. "
                    "Do not run shell or terminal commands or inspect the file afterward; "
                    "stop after creation."
                ),
            )
            thread_id = str(started["thread_id"])
            turn_id = str(started["turn_id"])
            running_status = await first_bridge.status(thread_id, turn_id)
            print(
                f"status: {running_status['state']} "
                f"activities={len(running_status['recent_activities'])}"
            )
            if running_status["turn_id"] != turn_id:
                print("status failed: selected turn does not match start", file=sys.stderr)
                return 1
            first_result = await _wait_until_terminal(first_bridge, thread_id, turn_id)
            first_result = await _resolve_safe_file_approval(
                first_bridge, first_result, thread_id, turn_id
            )
            print(f"start: {first_result['state']}")
            if first_result["state"] == "needs_approval":
                print(
                    "approval: not safely reproducible; rerun with an explicit user decision",
                    file=sys.stderr,
                )
                return 2
            if first_result["state"] != "completed" or not target.exists():
                print(f"start failed: {first_result}", file=sys.stderr)
                return 1
            thread_page = await _get_json(8001, "/ui-api/threads")
            detail = await _get_json(8001, f"/ui-api/threads/{thread_id}")
            turns = await _get_json(8001, f"/ui-api/threads/{thread_id}/turns")
            items = await _get_json(8001, f"/ui-api/threads/{thread_id}/items")
            api_status = await _get_json(8001, f"/ui-api/threads/{thread_id}/status")
            listed_ids = {
                thread.get("id")
                for thread in thread_page.get("threads", [])
                if isinstance(thread, dict)
            }
            print(
                "ui dogfood: "
                f"list={'ok' if thread_id in listed_ids else 'missing'}, "
                f"detail={detail.get('thread', {}).get('id')}, "
                f"turns={turns.get('history_mode')}, items={items.get('history_mode')}, "
                f"status={api_status.get('state')}"
            )
            if thread_id not in listed_ids or detail.get("thread", {}).get("id") != thread_id:
                print("ui history dogfood failed", file=sys.stderr)
                return 1
            completed_status = await first_bridge.status(thread_id, turn_id)
            print(
                f"status completed: {completed_status['state']} "
                f"latest={completed_status['latest_activity']['type']}"
            )

            continued = await first_bridge.continue_thread(
                thread_id,
                (
                    "Confirm that the previous file-creation task completed. Do not use "
                    "shell, filesystem, network, or other tools; reply exactly CONTINUE-OK."
                ),
            )
            second_result = await _wait_until_terminal(
                thread_id=thread_id, turn_id=str(continued["turn_id"]), bridge=first_bridge
            )
            print(f"continue: {second_result['state']}")
            if second_result["state"] != "completed":
                print(f"continue failed: {second_result}", file=sys.stderr)
                return 1

            sse_task = asyncio.create_task(_read_sse_event(8001))
            await asyncio.sleep(0.3)
            sse_started = await first_bridge.continue_thread(
                thread_id,
                "Reply exactly SSE-OK without using tools and then finish.",
            )
            sse_payload = await asyncio.wait_for(sse_task, timeout=15.0)
            sse_result = await _wait_until_terminal(
                first_bridge, thread_id, str(sse_started["turn_id"])
            )
            print(f"sse: {'activity' if 'event: activity' in sse_payload else 'no activity'}")
            if "event: activity" not in sse_payload or sse_result["state"] != "completed":
                print("sse dogfood failed", file=sys.stderr)
                return 1
        finally:
            await ui_server.shutdown(1.0)
            await first_app.shutdown()

        second_app = AppServerClient(executable)
        second_bridge = Bridge(second_app, StateStore(), policy)
        second_app.set_handlers(
            on_notification=second_bridge.handle_notification,
            on_server_request=second_bridge.handle_server_request,
        )
        await second_app.start()
        try:
            resumed = await second_bridge.continue_thread(
                thread_id,
                (
                    "Confirm that the previous smoke task is complete. Do not use shell, "
                    "filesystem, network, or other tools; reply exactly RESUME-OK."
                ),
            )
            resumed_result = await _wait_until_terminal(
                second_bridge, thread_id, str(resumed["turn_id"])
            )
            print(f"resume: {resumed_result['state']}")
            if resumed_result["state"] != "completed":
                print(f"resume failed: {resumed_result}", file=sys.stderr)
                return 1

            steer_started = await second_bridge.continue_thread(
                thread_id,
                (
                    "Keep this turn active briefly without using shell, filesystem, network, "
                    "or other tools; wait for further input."
                ),
            )
            try:
                steered = await second_bridge.steer(
                    thread_id,
                    str(steer_started["turn_id"]),
                    "Stop now and reply exactly STEER-OK without using any tools.",
                )
                print(f"steer: accepted state={steered['state']}")
            except Exception as exc:
                print(f"steer: not reproducible ({type(exc).__name__})")
            steer_result = await _wait_until_terminal(
                second_bridge, thread_id, str(steer_started["turn_id"])
            )
            print(f"steer terminal: {steer_result['state']}")
            return 0 if steer_result["state"] in {"completed", "interrupted"} else 1
        finally:
            await second_app.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser(description="Explicit real Codex App Server smoke test.")
    parser.parse_args()
    try:
        return asyncio.run(_run_smoke())
    except KeyboardInterrupt:
        logging.getLogger(__name__).error("smoke interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
