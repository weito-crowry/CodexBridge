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
    strict_run_metadata_json,
)

_MAX_BODY_BYTES = 128 * 1024
_MAX_WORKFLOW_YAML_BYTES = 64 * 1024
_MAX_TASK_EVENT_QUERY_LIMIT = 100


class WorkflowPayloadTooLarge(WorkflowRequestError):
    """A request body exceeded the shared Workflow JSON parser limit."""


class WorkflowTaskQueryError(WorkflowRequestError):
    def __init__(self, error_code: str) -> None:
        super().__init__("workflow task query is invalid")
        self.error_code = error_code


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
                pending_response: dict[str, object] = {
                    "workflow_id": result.workflow.workflow_id,
                    "status": status,
                }
                strict_metadata = strict_run_metadata_json(result.workflow)
                if strict_metadata:
                    pending_response["run_id"] = result.workflow.run_id
                    pending_response.update(strict_metadata)
                return JSONResponse(pending_response, status_code=202)
            if status == "needs_reconciliation":
                strict_metadata = strict_run_metadata_json(result.workflow)
                error_code = (
                    "workflow_execution_uncertain"
                    if (
                        strict_metadata.get("reason_code") == "strict_execution_uncertain"
                        and result.workflow.worker_state != "unknown"
                    )
                    else "workflow_needs_reconciliation"
                )
                reconciliation_response: dict[str, object] = {"error": error_code}
                if strict_metadata:
                    reconciliation_response.update(
                        {
                            "workflow_id": result.workflow.workflow_id,
                            "run_id": result.workflow.run_id,
                            "status": status,
                            **strict_metadata,
                        }
                    )
                return JSONResponse(reconciliation_response, status_code=409)
            if status == "failed_to_start":
                strict_metadata = strict_run_metadata_json(result.workflow)
                if strict_metadata:
                    return JSONResponse(
                        {
                            "error": "workflow_failed_to_start",
                            "workflow_id": result.workflow.workflow_id,
                            "run_id": result.workflow.run_id,
                            "status": status,
                            **strict_metadata,
                        },
                        status_code=409,
                    )
                return JSONResponse({"error": "workflow_failed_to_start"}, status_code=409)
            if status == "failed":
                strict_metadata = strict_run_metadata_json(result.workflow)
                if strict_metadata:
                    reason_code = strict_metadata.get("reason_code")
                    strict_error = {
                        "output_contract_violation": "workflow_output_invalid",
                        "resource_limit_exceeded": "workflow_resource_limit",
                    }.get(
                        reason_code if isinstance(reason_code, str) else "",
                        "workflow_failed",
                    )
                    return JSONResponse(
                        {
                            "error": strict_error,
                            "workflow_id": result.workflow.workflow_id,
                            "run_id": result.workflow.run_id,
                            "status": status,
                            **strict_metadata,
                        },
                        status_code=409,
                    )
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

    async def workflow_tasks(request: Request) -> Response:
        forbidden = _authorization_error(request, token, ui_port, authorize)
        if forbidden is not None:
            return forbidden
        try:
            projection = await service.get_workflow_tasks(request.path_params["workflow_id"])
            return JSONResponse(projection)
        except ValueError:
            return JSONResponse({"error": "invalid_workflow_id"}, status_code=400)
        except WorkflowNotFoundError:
            return JSONResponse({"error": "workflow_not_found"}, status_code=404)
        except WorkflowServiceUnavailable:
            return JSONResponse({"error": "workflow_service_unavailable"}, status_code=503)
        except Exception:
            return JSONResponse({"error": "internal_error"}, status_code=500)

    async def workflow_task_events(request: Request) -> Response:
        forbidden = _authorization_error(request, token, ui_port, authorize)
        if forbidden is not None:
            return forbidden
        try:
            after_id, limit = _parse_task_event_query(request)
            projection = await service.get_workflow_task_events(
                request.path_params["workflow_id"],
                after_id=after_id,
                limit=limit,
            )
            return JSONResponse(projection)
        except WorkflowTaskQueryError as error:
            return JSONResponse({"error": error.error_code}, status_code=400)
        except WorkflowRequestError:
            return JSONResponse({"error": "invalid_query"}, status_code=400)
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
        Route(
            "/ui-api/control/workflows/{workflow_id}/tasks",
            workflow_tasks,
            methods=["GET"],
        ),
        Route(
            "/ui-api/control/workflows/{workflow_id}/task-events",
            workflow_task_events,
            methods=["GET"],
        ),
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
        if declared_length < 0:
            raise WorkflowRequestError("content length is invalid")
        if declared_length > _MAX_BODY_BYTES:
            raise WorkflowPayloadTooLarge("request body exceeds 128 KiB")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > _MAX_BODY_BYTES:
            raise WorkflowPayloadTooLarge("request body exceeds 128 KiB")
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


def _parse_task_event_query(request: Request) -> tuple[int, int]:
    items = list(request.query_params.multi_items())
    if any(key not in {"after_id", "limit"} for key, _value in items):
        raise WorkflowTaskQueryError("invalid_query")
    values: dict[str, list[str]] = {"after_id": [], "limit": []}
    for key, value in items:
        values[key].append(value)
    if len(values["after_id"]) > 1:
        raise WorkflowTaskQueryError("invalid_cursor")
    if len(values["limit"]) > 1:
        raise WorkflowTaskQueryError("invalid_limit")
    after_id = 0
    if values["after_id"]:
        raw_after_id = values["after_id"][0]
        if not raw_after_id.isascii() or not raw_after_id.isdigit():
            raise WorkflowTaskQueryError("invalid_cursor")
        try:
            after_id = int(raw_after_id)
        except ValueError:
            raise WorkflowTaskQueryError("invalid_cursor") from None
        if after_id > 2**63 - 1:
            raise WorkflowTaskQueryError("invalid_cursor")
    limit = 100
    if values["limit"]:
        raw_limit = values["limit"][0]
        if not raw_limit.isascii() or not raw_limit.isdigit():
            raise WorkflowTaskQueryError("invalid_limit")
        try:
            limit = int(raw_limit)
        except ValueError:
            raise WorkflowTaskQueryError("invalid_limit") from None
        if not 1 <= limit <= _MAX_TASK_EVENT_QUERY_LIMIT:
            raise WorkflowTaskQueryError("invalid_limit")
    return after_id, limit


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
    result.update(strict_run_metadata_json(snapshot))
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
    result.update(strict_run_metadata_json(snapshot))
    return result
