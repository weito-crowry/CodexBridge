from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Protocol, cast

from mcp.server import MCPServer

from ..config import BridgeConfig
from .service import (
    WorkflowNotFoundError,
    WorkflowRequestError,
    WorkflowServiceUnavailable,
)

_MAX_TASK_RESPONSE_BYTES = 256 * 1024
_MAX_EVENT_LIMIT = 100


class WorkflowTaskServiceLike(Protocol):
    async def get_workflow_tasks(self, workflow_id: str) -> dict[str, Any]: ...

    async def get_workflow_task_events(
        self,
        workflow_id: str,
        *,
        after_id: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]: ...


def register_workflow_task_tools(
    mcp: MCPServer,
    config: BridgeConfig,
    runtime_lookup: Callable[[], object | None],
) -> None:
    """Register read-only task observation tools for existing Workflow runs."""

    def authorized_service() -> tuple[WorkflowTaskServiceLike | None, dict[str, str] | None]:
        if "read" not in config.workflow_mcp_permissions:
            return None, {"error": "workflow_permission_denied"}
        try:
            runtime = runtime_lookup()
            service = getattr(runtime, "workflow_service", None)
            if not callable(getattr(service, "get_workflow_tasks", None)) or not callable(
                getattr(service, "get_workflow_task_events", None)
            ):
                return None, {"error": "workflow_service_unavailable"}
            return cast(WorkflowTaskServiceLike, service), None
        except Exception:
            return None, {"error": "workflow_service_unavailable"}

    @mcp.tool()
    async def workflow_run_tasks(workflow_id: str) -> dict[str, Any]:
        """Read a bounded, redacted static graph and observed task state."""
        service, error = authorized_service()
        if error is not None:
            return error
        if not isinstance(workflow_id, str):
            return {"error": "invalid_workflow_id"}
        assert service is not None
        try:
            return _bounded_response(await service.get_workflow_tasks(workflow_id))
        except ValueError:
            return {"error": "invalid_workflow_id"}
        except WorkflowNotFoundError:
            return {"error": "workflow_not_found"}
        except WorkflowServiceUnavailable:
            return {"error": "workflow_service_unavailable"}
        except Exception:
            return {"error": "internal_error"}

    @mcp.tool()
    async def workflow_run_events(
        workflow_id: str,
        after_id: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Read a bounded page of safe Workflow task events after an exclusive cursor."""
        service, error = authorized_service()
        if error is not None:
            return error
        if not isinstance(workflow_id, str):
            return {"error": "invalid_workflow_id"}
        if type(after_id) is not int or not 0 <= after_id <= 2**63 - 1:
            return {"error": "invalid_cursor"}
        if type(limit) is not int or not 1 <= limit <= _MAX_EVENT_LIMIT:
            return {"error": "invalid_limit"}
        assert service is not None
        try:
            response = await service.get_workflow_task_events(
                workflow_id,
                after_id=after_id,
                limit=limit,
            )
            return _bounded_response(response)
        except WorkflowRequestError:
            return {"error": "invalid_cursor"}
        except ValueError:
            return {"error": "invalid_workflow_id"}
        except WorkflowNotFoundError:
            return {"error": "workflow_not_found"}
        except WorkflowServiceUnavailable:
            return {"error": "workflow_service_unavailable"}
        except Exception:
            return {"error": "internal_error"}


def _bounded_response(response: dict[str, Any]) -> dict[str, Any]:
    try:
        encoded = json.dumps(
            response,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        return {"error": "internal_error"}
    if len(encoded) > _MAX_TASK_RESPONSE_BYTES:
        return {"error": "response_too_large"}
    return response
