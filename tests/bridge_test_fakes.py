from __future__ import annotations

from typing import Any

from codex_bridge.activity import ActivityStore
from codex_bridge.bridge import Bridge
from codex_bridge.paths import AllowedPathPolicy
from codex_bridge.state import StateStore


class FakeAppServer:
    def __init__(self) -> None:
        self.methods: list[str] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses: list[tuple[int | str, dict[str, Any]]] = []
        self.rejections: list[tuple[int | str, int, str]] = []
        self.thread_cwds: dict[str, str] = {}
        self.thread_histories: dict[str, list[dict[str, Any]]] = {}
        self.thread_history_modes: dict[str, str] = {}
        self.thread_paths: dict[str, str] = {}
        self.thread_list: list[dict[str, Any]] = []
        self.turns_response: dict[str, Any] = {"data": []}
        self.items_response: dict[str, Any] = {"data": []}
        self.rate_limits_response: dict[str, Any] = {
            "rateLimits": {
                "primary": {"windowDurationMins": 300, "usedPercent": 28},
                "secondary": {"windowDurationMins": 10080, "usedPercent": 39},
            }
        }
        self.model_pages: dict[str | None, dict[str, Any]] = {}
        self.config_response: dict[str, Any] = {"config": {}}
        self.thread_start_settings: dict[str, Any] = {}
        self.thread_start_error: Exception | None = None
        self.thread_resume_settings: dict[str, Any] = {}

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.methods.append(method)
        self.calls.append((method, params))
        if method == "thread/start":
            if self.thread_start_error is not None:
                raise self.thread_start_error
            response = {
                "thread": {
                    "id": "native-thread",
                    "modelProvider": "openai",
                    "model": "gpt-5",
                    "reasoningEffort": "high",
                    "cliVersion": "0.1.2",
                },
            }
            for key in ("approvalPolicy", "approvalsReviewer"):
                if key in params:
                    response[key] = params[key]
            response.update(self.thread_start_settings)
            return response
        if method == "model/list":
            return self.model_pages.get(params.get("cursor"), {"data": []})
        if method == "config/read":
            return self.config_response
        if method == "thread/resume":
            return {
                "thread": {
                    "id": params["threadId"],
                    "modelProvider": "openai",
                    "model": "gpt-5",
                    "reasoningEffort": "medium",
                    "cliVersion": "0.1.2",
                },
                "cwd": self.thread_cwds[params["threadId"]],
                **self.thread_resume_settings,
            }
        if method == "turn/start":
            return {"turn": {"id": "native-turn", "status": "inProgress"}}
        if method == "turn/steer":
            return {"turnId": params["expectedTurnId"]}
        if method == "turn/interrupt":
            return {}
        if method == "thread/name/set":
            return {"thread": {"id": params["threadId"], "name": params["name"]}}
        if method == "thread/list":
            return {"data": self.thread_list}
        if method == "thread/read":
            thread: dict[str, Any] = {
                "id": params["threadId"],
                "turns": [],
                "raw": "do not expose",
                "chain_of_thought": "do not expose",
            }
            if params["threadId"] in self.thread_cwds:
                thread["cwd"] = self.thread_cwds[params["threadId"]]
            if params.get("includeTurns") and params["threadId"] in self.thread_histories:
                thread["turns"] = self.thread_histories[params["threadId"]]
            if params["threadId"] in self.thread_history_modes:
                thread["historyMode"] = self.thread_history_modes[params["threadId"]]
            if params["threadId"] in self.thread_paths:
                thread["path"] = self.thread_paths[params["threadId"]]
            return {"thread": thread}
        if method == "thread/turns/list":
            return self.turns_response
        if method == "thread/items/list":
            return self.items_response
        if method == "account/rateLimits/read":
            return self.rate_limits_response
        raise AssertionError(f"unexpected method {method}")

    async def respond(self, request_id: int | str, result: dict[str, Any]) -> None:
        self.responses.append((request_id, result))

    async def reject(self, request_id: int | str, code: int, message: str) -> None:
        self.rejections.append((request_id, code, message))


def make_bridge(allowed_dir) -> tuple[Bridge, FakeAppServer, StateStore]:
    app = FakeAppServer()
    store = StateStore()
    bridge = Bridge(app, store, AllowedPathPolicy((str(allowed_dir),)))
    return bridge, app, store


def make_activity_bridge(allowed_dir) -> tuple[Bridge, FakeAppServer, StateStore, ActivityStore]:
    app = FakeAppServer()
    store = StateStore()
    activities = ActivityStore()
    bridge = Bridge(
        app,
        store,
        AllowedPathPolicy((str(allowed_dir),)),
        activity_store=activities,
    )
    return bridge, app, store, activities
