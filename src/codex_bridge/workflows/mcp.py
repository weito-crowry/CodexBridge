from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Protocol, cast

from mcp.server import MCPServer

from ..config import BridgeConfig
from .api import _list_item_json, _snapshot_json
from .service import (
    WorkflowNotFoundError,
    WorkflowRequestError,
    WorkflowResultSnapshot,
    WorkflowServiceUnavailable,
    WorkflowSnapshot,
    strict_run_metadata_json,
)

_MAX_MCP_RESPONSE_BYTES = 64 * 1024
_MAX_WORKFLOW_LIST_LIMIT = 100


class WorkflowServiceLike(Protocol):
    async def list_workflows(self, *, limit: int = 50) -> list[WorkflowSnapshot]: ...

    async def get_workflow(self, workflow_id: str) -> WorkflowSnapshot: ...

    async def get_result(self, workflow_id: str) -> WorkflowResultSnapshot: ...


def register_workflow_tools(
    mcp: MCPServer,
    config: BridgeConfig,
    runtime_lookup: Callable[[], object | None],
) -> None:
    """Register the read-only tools for existing Workflow runs."""

    def service_for_request() -> WorkflowServiceLike | None:
        try:
            runtime = runtime_lookup()
            service = getattr(runtime, "workflow_service", None)
            return None if service is None else cast(WorkflowServiceLike, service)
        except Exception:
            return None

    def authorized_service() -> tuple[WorkflowServiceLike | None, dict[str, str] | None]:
        if "read" not in config.workflow_mcp_permissions:
            return None, {"error": "workflow_permission_denied"}
        service = service_for_request()
        if service is None:
            return None, {"error": "workflow_service_unavailable"}
        return service, None

    @mcp.tool()
    async def workflow_runs_list(limit: int = 50) -> dict[str, Any]:
        """List bounded public status fields for existing Workflow runs."""
        service, error = authorized_service()
        if error is not None:
            return error
        if type(limit) is not int or not 1 <= limit <= _MAX_WORKFLOW_LIST_LIMIT:
            return {"error": "invalid_limit"}
        assert service is not None
        try:
            workflows = await service.list_workflows(limit=limit)
            response = {
                "workflows": [_list_item_json(snapshot) for snapshot in workflows],
                "limit": limit,
                "truncated": len(workflows) == limit,
            }
            return _bounded_response(response)
        except WorkflowRequestError:
            return {"error": "invalid_limit"}
        except WorkflowServiceUnavailable:
            return {"error": "workflow_service_unavailable"}
        except Exception:
            return {"error": "internal_error"}

    @mcp.tool()
    async def workflow_run_get(workflow_id: str) -> dict[str, Any]:
        """Read the public status snapshot for one existing Workflow run."""
        service, error = authorized_service()
        if error is not None:
            return error
        if not isinstance(workflow_id, str):
            return {"error": "invalid_workflow_id"}
        assert service is not None
        try:
            snapshot = await service.get_workflow(workflow_id)
            return _bounded_response(_snapshot_json(snapshot))
        except ValueError:
            return {"error": "invalid_workflow_id"}
        except WorkflowNotFoundError:
            return {"error": "workflow_not_found"}
        except WorkflowServiceUnavailable:
            return {"error": "workflow_service_unavailable"}
        except Exception:
            return {"error": "internal_error"}

    @mcp.tool()
    async def workflow_run_result(workflow_id: str) -> dict[str, Any]:
        """Read one verified Workflow result explicitly, limited to 64 KiB of JSON."""
        service, error = authorized_service()
        if error is not None:
            return error
        if not isinstance(workflow_id, str):
            return {"error": "invalid_workflow_id"}
        assert service is not None
        try:
            result_snapshot = await service.get_result(workflow_id)
            snapshot = result_snapshot.workflow
            status = snapshot.status
            if status == "succeeded" and result_snapshot.result is not None:
                return _bounded_response(result_snapshot.result, oversized_error="result_too_large")
            if status in {"queued", "starting", "running"}:
                error_code = "workflow_not_completed"
            elif status == "needs_reconciliation":
                error_code = "workflow_needs_reconciliation"
            elif status == "failed_to_start":
                error_code = "workflow_failed_to_start"
            elif status == "failed":
                error_code = "workflow_failed"
            else:
                error_code = "workflow_state_unknown"
            response: dict[str, Any] = {
                "error": error_code,
                "workflow_id": snapshot.workflow_id,
                "status": status,
            }
            strict_metadata = strict_run_metadata_json(snapshot)
            if strict_metadata:
                response["run_id"] = snapshot.run_id
                response.update(strict_metadata)
            return _bounded_response(response)
        except ValueError:
            return {"error": "invalid_workflow_id"}
        except WorkflowNotFoundError:
            return {"error": "workflow_not_found"}
        except WorkflowServiceUnavailable:
            return {"error": "workflow_service_unavailable"}
        except Exception:
            return {"error": "internal_error"}


def _bounded_response(
    response: dict[str, Any], *, oversized_error: str = "response_too_large"
) -> dict[str, Any]:
    try:
        encoded = json.dumps(
            response,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        return {"error": "internal_error"}
    if len(encoded) > _MAX_MCP_RESPONSE_BYTES:
        return {"error": oversized_error}
    return response
