from __future__ import annotations

import asyncio
import socket
from typing import Any

import pytest
import uvicorn

from codex_bridge.config import BridgeConfig, ExecutionTargetConfig
from codex_bridge.execution_targets import ExecutionTargetRouter
from codex_bridge.server import create_app


class DogfoodBridge:
    async def start(self, cwd: str, prompt: str) -> dict[str, Any]:
        return {
            "ok": True,
            "cwd": cwd,
            "accepted_prompt": prompt,
            "thread_id": "native-dogfood-thread",
            "turn_id": "native-dogfood-turn",
        }

    async def continue_thread(self, thread_id: str, prompt: str) -> dict[str, Any]:
        return {"ok": True, "thread_id": thread_id, "prompt": prompt}

    async def wait(
        self, thread_id: str, turn_id: str, timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        return {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "state": "completed",
            "pending_request": None,
        }

    async def steer(self, thread_id: str, turn_id: str, prompt: str) -> dict[str, Any]:
        return {"thread_id": thread_id, "turn_id": turn_id, "prompt": prompt}

    async def approve(self, request_id: int | str, decision: str) -> dict[str, Any]:
        return {"request_id": request_id, "decision": decision}

    async def answer_user_input(
        self, request_id: int | str, answers: dict[str, list[str]]
    ) -> dict[str, Any]:
        return {"request_id": request_id, "answers": answers}

    async def interrupt(self, thread_id: str, turn_id: str) -> dict[str, Any]:
        return {"thread_id": thread_id, "turn_id": turn_id, "state": "interrupted"}

    async def threads(
        self,
        thread_id: str | None = None,
        *,
        include_history: bool = False,
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        if thread_id is not None:
            return {"thread": {"id": thread_id}}
        return {"threads": [], "next_cursor": None}

    async def status(
        self, thread_id: str, turn_id: str | None = None, activity_limit: int = 20
    ) -> dict[str, Any]:
        return {"thread_id": thread_id, "turn_id": turn_id, "state": "completed"}


class DogfoodRuntime:
    def __init__(self) -> None:
        self.bridge = DogfoodBridge()

    async def start(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None


@pytest.mark.asyncio
async def test_local_gateway_routes_to_real_single_target_mcp_node(tmp_path) -> None:
    remote_config = BridgeConfig(
        host="127.0.0.1",
        port=8000,
        ui_port=8001,
        allowed_roots=(str(tmp_path),),
        allowed_hosts=(),
        allowed_origins=(),
        codex_executable="codex",
        wait_default_seconds=18.0,
        wait_max_seconds=30.0,
        shutdown_grace_seconds=3.0,
    )
    remote_app = create_app(remote_config, runtime_factory=lambda _: DogfoodRuntime())

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener_port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            remote_app,
            host="127.0.0.1",
            port=listener_port,
            lifespan="on",
            log_level="critical",
            access_log=False,
        )
    )
    serving = asyncio.create_task(server.serve(sockets=[listener]))

    async def wait_until_started() -> None:
        while not server.started:
            if serving.done():
                await serving
                raise RuntimeError("remote dogfood MCP node exited before startup")
            await asyncio.sleep(0.01)

    try:
        await asyncio.wait_for(wait_until_started(), timeout=5)
        remote_target = ExecutionTargetConfig(
            "notebook",
            "Notebook PC",
            "remote",
            f"http://127.0.0.1:{listener_port}/mcp",
        )
        local_bridge = DogfoodBridge()
        gateway = ExecutionTargetRouter(
            (remote_config.targets[0], remote_target),
            lambda: local_bridge,
        )
        await gateway.start()

        targets = await gateway.target_list()
        started = await gateway.codex_start(
            str(tmp_path / "remote-path-does-not-need-local-validation"),
            "dogfood prompt",
            "notebook",
        )
        waited = await gateway.codex_wait(started["thread_id"], started["turn_id"], 5.0)
        status = await gateway.codex_status(started["thread_id"], started["turn_id"])

        assert targets["selection_required"] is True
        assert targets["targets"][1]["available"] is True
        assert started["thread_id"] == "notebook::native-dogfood-thread"
        assert started["cwd"] == str(tmp_path / "remote-path-does-not-need-local-validation")
        assert waited["thread_id"] == started["thread_id"]
        assert status["thread_id"] == started["thread_id"]
        await gateway.close()
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, timeout=5)
        listener.close()
