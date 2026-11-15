from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ..ui_control import _valid_origin
from .service import (
    WorkflowConcurrencyConflict,
    WorkflowNotFoundError,
    WorkflowReconciliationRequired,
    WorkflowRequestConflict,
    WorkflowRequestError,
    WorkflowService,
    WorkflowServiceUnavailable,
    WorkflowSnapshot,
    WorkflowStartFailed,
)

_MAX_BODY_BYTES = 128 * 1024
_MAX_WORKFLOW_YAML_BYTES = 64 * 1024


def create_workflow_routes(
    service: WorkflowService,
    *,
    token: str,
    ui_port: int,
    authorize: Callable[[Request, str | None], bool],
    ready: Callable[[], bool],
) -> list[Route]:
    async def start(request: Request) -> Response:
        forbidden = _authorization_error(request, token, ui_port, authorize)
        if forbidden is not None:
            return forbidden
        try:
            payload = await _json_payload(request)
            if set(payload) - {
                "client_request_id",
                "workflow_yaml",
                "input",
                "workspace_root",
                "max_live_codex_jobs",
            }:
                raise WorkflowRequestError("request contains unsupported fields")
            if not {"client_request_id", "workflow_yaml", "workspace_root"} <= set(payload):
                raise WorkflowRequestError("request is missing required fields")
            client_request_id = payload["client_request_id"]
            workflow_yaml = payload["workflow_yaml"]
            workspace_root = payload["workspace_root"]
            if not isinstance(client_request_id, str):
                raise WorkflowRequestError("client_request_id must be a string")
            if not isinstance(workflow_yaml, str):
                raise WorkflowRequestError("workflow_yaml must be a string")
            try:
                yaml_size = len(workflow_yaml.encode("utf-8"))
            except UnicodeEncodeError:
                raise WorkflowRequestError("workflow_yaml is invalid UTF-8") from None
            if yaml_size > _MAX_WORKFLOW_YAML_BYTES:
                raise WorkflowRequestError("workflow_yaml exceeds 64 KiB")
            if not isinstance(workspace_root, str):
                raise WorkflowRequestError("workspace_root must be an absolute path")
            max_live_codex_jobs = payload.get("max_live_codex_jobs", 1)
            if type(max_live_codex_jobs) is not int or max_live_codex_jobs not in {1, 2, 3}:
                raise WorkflowRequestError("max_live_codex_jobs must be the integer 1, 2, or 3")
            if not ready():
                return JSONResponse({"error": "bridge_not_ready"}, status_code=503)
            snapshot = await service.start_workflow(
                client_request_id=client_request_id,
                workflow_yaml=workflow_yaml,
                input_data=payload.get("input", {}),
                workspace_root=workspace_root,
                max_live_codex_jobs=max_live_codex_jobs,
            )
            return JSONResponse(_snapshot_json(snapshot), status_code=202)
        except WorkflowRequestError:
            return JSONResponse({"error": "invalid_workflow_request"}, status_code=400)
        except WorkflowRequestConflict:
            return JSONResponse({"error": "request_id_conflict"}, status_code=409)
        except WorkflowConcurrencyConflict:
            return JSONResponse({"error": "workflow_capacity_reached"}, status_code=409)
        except WorkflowStartFailed as error:
            return JSONResponse(
                {
                    "error": "workflow_worker_not_ready",
                    **_snapshot_json(error.snapshot),
                },
                status_code=503,
            )
        except WorkflowReconciliationRequired as error:
            return JSONResponse(
                {
                    "error": "workflow_needs_reconciliation",
                    **_snapshot_json(error.snapshot),
                },
                status_code=409,
            )
        except WorkflowServiceUnavailable:
            return JSONResponse({"error": "workflow_service_unavailable"}, status_code=503)
        except Exception:
            return JSONResponse({"error": "internal_error"}, status_code=500)

    async def list_workflows(request: Request) -> Response:
        forbidden = _authorization_error(request, token, ui_port, authorize)
        if forbidden is not None:
            return forbidden
        try:
            limit = _parse_limit(request)
            workflows = await service.list_workflows(limit=limit)
            return JSONResponse(
                {
                    "workflows": [_list_item_json(snapshot) for snapshot in workflows],
                    "limit": limit,
                    "truncated": len(workflows) == limit,
                }
            )
        except WorkflowRequestError:
            return JSONResponse({"error": "invalid_limit"}, status_code=400)
        except WorkflowServiceUnavailable:
            return JSONResponse({"error": "workflow_service_unavailable"}, status_code=503)
        except Exception:
            return JSONResponse({"error": "internal_error"}, status_code=500)

    async def workflow_detail(request: Request) -> Response:
        forbidden = _authorization_error(request, token, ui_port, authorize)
        if forbidden is not None:
            return forbidden
        try:
            snapshot = await service.get_workflow(request.path_params["workflow_id"])
            return JSONResponse(_snapshot_json(snapshot))
        except ValueError:
            return JSONResponse({"error": "invalid_workflow_id"}, status_code=400)
        except WorkflowNotFoundError:
            return JSONResponse({"error": "workflow_not_found"}, status_code=404)
        except WorkflowServiceUnavailable:
            return JSONResponse({"error": "workflow_service_unavailable"}, status_code=503)
        except Exception:
            return JSONResponse({"error": "internal_error"}, status_code=500)

    async def workflow_result(request: Request) -> Response:
        forbidden = _authorization_error(request, token, ui_port, authorize)
        if forbidden is not None:
            return forbidden
        try:
            result = await service.get_result(request.path_params["workflow_id"])
            status = result.workflow.status
            if status == "succeeded" and result.result is not None:
                return JSONResponse(result.result)
            if status in {"queued", "starting", "running"}:
                return JSONResponse(
                    {"workflow_id": result.workflow.workflow_id, "status": status},
                    status_code=202,
                )
            if status == "needs_reconciliation":
                return JSONResponse({"error": "workflow_needs_reconciliation"}, status_code=409)
            if status == "failed_to_start":
                return JSONResponse({"error": "workflow_failed_to_start"}, status_code=409)
            if status == "failed":
                return JSONResponse({"error": "workflow_failed"}, status_code=409)
            return JSONResponse({"error": "workflow_state_unknown"}, status_code=409)
        except ValueError:
            return JSONResponse({"error": "invalid_workflow_id"}, status_code=400)
        except WorkflowNotFoundError:
            return JSONResponse({"error": "workflow_not_found"}, status_code=404)
        except WorkflowServiceUnavailable:
            return JSONResponse({"error": "workflow_service_unavailable"}, status_code=503)
        except Exception:
            return JSONResponse({"error": "internal_error"}, status_code=500)

    return [
        Route("/ui-api/control/workflows", start, methods=["POST"]),
        Route("/ui-api/control/workflows", list_workflows, methods=["GET"]),
        Route("/ui-api/control/workflows/{workflow_id}/result", workflow_result, methods=["GET"]),
        Route("/ui-api/control/workflows/{workflow_id}", workflow_detail, methods=["GET"]),
    ]


def _authorization_error(
    request: Request,
    token: str,
    ui_port: int,
    authorize: Callable[[Request, str | None], bool],
) -> JSONResponse | None:
    if not _valid_origin(request, ui_port) or not authorize(request, token):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return None


async def _json_payload(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise WorkflowRequestError("content type must be application/json")
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            raise WorkflowRequestError("content length is invalid") from None
        if declared_length < 0 or declared_length > _MAX_BODY_BYTES:
            raise WorkflowRequestError("request body exceeds 128 KiB")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > _MAX_BODY_BYTES:
            raise WorkflowRequestError("request body exceeds 128 KiB")
    try:
        value = json.loads(
            body,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_unique_object,
        )
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, RecursionError):
        raise WorkflowRequestError("request body is invalid JSON") from None
    if not isinstance(value, dict):
        raise WorkflowRequestError("request body must be an object")
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _parse_limit(request: Request) -> int:
    values = request.query_params.getlist("limit")
    if not values:
        return 50
    if len(values) != 1 or not values[0].isascii() or not values[0].isdigit():
        raise WorkflowRequestError("limit is invalid")
    limit = int(values[0])
    if not 1 <= limit <= 100:
        raise WorkflowRequestError("limit is invalid")
    return limit


def _snapshot_json(snapshot: WorkflowSnapshot) -> dict[str, object]:
    result: dict[str, object] = {
        "workflow_id": snapshot.workflow_id,
        "run_id": snapshot.run_id,
        "status": snapshot.status,
        "created_at": snapshot.created_at,
        "updated_at": snapshot.updated_at,
        "worker_state": snapshot.worker_state,
        "terminal": snapshot.terminal,
    }
    if snapshot.job_id is not None:
        result["job_id"] = snapshot.job_id
    if snapshot.thread_id is not None:
        result["thread_id"] = snapshot.thread_id
    if snapshot.max_live_codex_jobs in {2, 3}:
        result["max_live_codex_jobs"] = snapshot.max_live_codex_jobs
        result["jobs"] = [
            {
                "job_id": job.job_id,
                "task_identity": job.task_identity,
                "state": job.state,
                "thread_id": job.thread_id,
                "turn_id": job.turn_id,
            }
            for job in snapshot.jobs
        ]
    return result


def _list_item_json(snapshot: WorkflowSnapshot) -> dict[str, object]:
    result: dict[str, object] = {
        "workflow_id": snapshot.workflow_id,
        "status": snapshot.status,
        "created_at": snapshot.created_at,
        "updated_at": snapshot.updated_at,
    }
    if snapshot.job_id is not None:
        result["job_id"] = snapshot.job_id
    if snapshot.thread_id is not None:
        result["thread_id"] = snapshot.thread_id
    return result
