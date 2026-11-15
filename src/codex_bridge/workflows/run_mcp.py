from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from mcp.server import MCPServer

from ..config import BridgeConfig
from ..ui_api import _app_server_ready
from .definitions import (
    DefinitionError,
    DefinitionInvalid,
    DefinitionNotFound,
    DefinitionRepositoryUnavailable,
    WorkflowDefinitionRepository,
)
from .service import (
    WorkflowConcurrencyConflict,
    WorkflowReconciliationRequired,
    WorkflowRequestConflict,
    WorkflowRequestError,
    WorkflowService,
    WorkflowServiceUnavailable,
    WorkflowSnapshot,
    WorkflowStartFailed,
)

_MAX_RUN_START_RESPONSE_BYTES = 16 * 1024


def register_workflow_run_start_tool(
    mcp: MCPServer,
    config: BridgeConfig,
    runtime_lookup: Callable[[], object | None],
) -> None:
    """Register the native saved Workflow execution tool for execute permission."""

    def components() -> tuple[WorkflowService | None, WorkflowDefinitionRepository | None, bool]:
        if "execute" not in config.workflow_mcp_permissions:
            return None, None, False
        try:
            runtime = runtime_lookup()
            service = getattr(runtime, "workflow_service", None)
            repository = getattr(runtime, "workflow_definitions", None)
            bridge = getattr(runtime, "bridge", None)
        except Exception:
            return None, None, False
        return service, repository, bridge is not None and _app_server_ready(bridge)

    @mcp.tool()
    async def workflow_run_start(
        definition_id: str,
        revision: int,
        client_request_id: str,
        workspace_root: str,
        input: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Start one fixed revision of a saved Workflow definition."""
        if "execute" not in config.workflow_mcp_permissions:
            return {"error": "workflow_permission_denied"}
        service, repository, ready = components()
        if service is None or repository is None:
            return {"error": "workflow_service_unavailable"}
        if not ready:
            return {"error": "bridge_not_ready"}
        if (
            not isinstance(definition_id, str)
            or type(revision) is not int
            or revision < 1
            or not isinstance(client_request_id, str)
            or not isinstance(workspace_root, str)
            or (input is not None and not isinstance(input, dict))
        ):
            return {"error": "invalid_workflow_request"}
        try:
            snapshot = await service.start_saved_workflow(
                repository=repository,
                definition_id=definition_id,
                revision=revision,
                client_request_id=client_request_id,
                workspace_root=workspace_root,
                input_data={} if input is None else input,
            )
            return _bounded_run_response(
                _run_response(snapshot, definition_id=definition_id, revision=revision)
            )
        except DefinitionInvalid:
            return {"error": "invalid_workflow_request"}
        except DefinitionNotFound:
            return {"error": "workflow_definition_not_found"}
        except DefinitionRepositoryUnavailable:
            return {"error": "workflow_definition_service_unavailable"}
        except WorkflowRequestError:
            return {"error": "invalid_workflow_request"}
        except WorkflowRequestConflict:
            return {"error": "request_id_conflict"}
        except WorkflowConcurrencyConflict:
            return {"error": "workflow_capacity_reached"}
        except WorkflowStartFailed as error:
            return _bounded_run_response(
                {
                    "error": "workflow_worker_not_ready",
                    **_run_response(error.snapshot, definition_id=definition_id, revision=revision),
                }
            )
        except WorkflowReconciliationRequired as error:
            return _bounded_run_response(
                {
                    "error": "workflow_needs_reconciliation",
                    **_run_response(error.snapshot, definition_id=definition_id, revision=revision),
                }
            )
        except WorkflowServiceUnavailable:
            return {"error": "workflow_service_unavailable"}
        except DefinitionError:
            return {"error": "internal_error"}
        except Exception:
            return {"error": "internal_error"}


def _run_response(
    snapshot: WorkflowSnapshot, *, definition_id: str, revision: int
) -> dict[str, Any]:
    return {
        "workflow_id": snapshot.workflow_id,
        "run_id": snapshot.run_id,
        "status": snapshot.status,
        "definition_id": definition_id,
        "revision": revision,
    }


def _bounded_run_response(response: dict[str, Any]) -> dict[str, Any]:
    try:
        encoded = json.dumps(
            response,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        return {"error": "internal_error"}
    if len(encoded) > _MAX_RUN_START_RESPONSE_BYTES:
        return {"error": "response_too_large"}
    return response
