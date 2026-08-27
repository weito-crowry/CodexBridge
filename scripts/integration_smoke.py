from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import tempfile
from pathlib import Path

from codex_bridge.app_server import AppServerClient
from codex_bridge.bridge import Bridge
from codex_bridge.logging_utils import configure_logging
from codex_bridge.paths import AllowedPathPolicy
from codex_bridge.state import StateStore


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


async def _run_smoke() -> int:
    configure_logging()
    with tempfile.TemporaryDirectory(prefix="codexbridge-smoke-") as temporary:
        root = Path(temporary)
        target = root / "codexbridge_smoke.txt"
        policy = AllowedPathPolicy((str(root),))

        first_app = AppServerClient("codex")
        first_bridge = Bridge(first_app, StateStore(), policy)
        first_app.set_handlers(
            on_notification=first_bridge.handle_notification,
            on_server_request=first_bridge.handle_server_request,
        )
        await first_app.start()
        try:
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
        finally:
            await first_app.shutdown()

        second_app = AppServerClient("codex")
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
