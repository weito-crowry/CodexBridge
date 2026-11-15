from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any, cast

from mcp.server import MCPServer

from ..config import BridgeConfig
from .definition_api import _revision_detail_json, _revision_metadata, _summary_json
from .definitions import (
    DefinitionError,
    DefinitionInvalid,
    DefinitionMutationConflict,
    DefinitionNotFound,
    DefinitionPreconditionFailed,
    DefinitionRepositoryUnavailable,
    DefinitionRevision,
    WorkflowDefinitionRepository,
)
from .graph import project_workflow_graph

_MAX_DEFINITION_RESPONSE_BYTES = 256 * 1024


def register_workflow_definition_tools(
    mcp: MCPServer,
    config: BridgeConfig,
    runtime_lookup: Callable[[], object | None],
) -> None:
    """Register definition tools only for their configured MCP permissions."""

    def repository_for_request(
        permission: str,
    ) -> tuple[WorkflowDefinitionRepository | None, dict[str, str] | None]:
        if permission not in config.workflow_mcp_permissions:
            return None, {"error": "workflow_permission_denied"}
        try:
            runtime = runtime_lookup()
            repository = getattr(runtime, "workflow_definitions", None)
        except Exception:
            repository = None
        if repository is None:
            return None, {"error": "workflow_definition_service_unavailable"}
        return cast(WorkflowDefinitionRepository, repository), None

    if "read" in config.workflow_mcp_permissions:

        @mcp.tool()
        async def workflow_definitions_list(
            limit: int = 50, cursor: str | None = None
        ) -> dict[str, Any]:
            """List bounded metadata for saved Workflow definitions."""
            repository, permission_error = repository_for_request("read")
            if permission_error is not None:
                return permission_error
            if type(limit) is not int or not 1 <= limit <= 100:
                return {"error": "invalid_limit"}
            if cursor is not None and not isinstance(cursor, str):
                return {"error": "invalid_cursor"}
            assert repository is not None
            try:
                page = await asyncio.to_thread(repository.list, limit, cursor)
                return _bounded_definition_response(
                    {
                        "definitions": [_summary_json(item) for item in page.items],
                        "limit": limit,
                        "next_cursor": page.next_cursor,
                    }
                )
            except DefinitionError as definition_error:
                return _definition_error(definition_error)
            except Exception:
                return {"error": "internal_error"}

        @mcp.tool()
        async def workflow_definition_get(
            definition_id: str, revision: int | None = None
        ) -> dict[str, Any]:
            """Read one immutable Workflow definition revision, including source YAML."""
            repository, permission_error = repository_for_request("read")
            if permission_error is not None:
                return permission_error
            if not isinstance(definition_id, str):
                return {"error": "invalid_definition_id"}
            if revision is not None and (type(revision) is not int or revision < 1):
                return {"error": "invalid_revision"}
            assert repository is not None
            try:
                saved = await asyncio.to_thread(repository.get, definition_id, revision)
                return _bounded_definition_response(_revision_detail_json(saved))
            except DefinitionError as definition_error:
                return _definition_error(definition_error)
            except Exception:
                return {"error": "internal_error"}

        @mcp.tool()
        async def workflow_graph_get(
            definition_id: str, revision: int | None = None
        ) -> dict[str, Any]:
            """Project a saved Workflow definition into a static graph without executing it."""
            repository, permission_error = repository_for_request("read")
            if permission_error is not None:
                return permission_error
            if not isinstance(definition_id, str):
                return {"error": "invalid_definition_id"}
            if revision is not None and (type(revision) is not int or revision < 1):
                return {"error": "invalid_revision"}
            assert repository is not None
            try:

                def project() -> tuple[DefinitionRevision, dict[str, Any]]:
                    saved = repository.get(definition_id, revision)
                    graph = project_workflow_graph(
                        saved.definition,
                        codex_job_live=True,
                        max_live_codex_jobs=saved.max_live_codex_jobs,
                    )
                    return saved, graph

                saved, graph = await asyncio.to_thread(project)
                return _bounded_definition_response(
                    {
                        "definition_id": saved.definition_id,
                        "revision": saved.revision,
                        "etag": saved.etag,
                        "graph": graph,
                    }
                )
            except DefinitionError as definition_error:
                return _definition_error(definition_error)
            except Exception:
                return {"error": "internal_error"}

    if "write" in config.workflow_mcp_permissions:

        @mcp.tool()
        async def workflow_definition_validate(
            source_yaml: str, max_live_codex_jobs: int = 1
        ) -> dict[str, Any]:
            """Validate source YAML and return safe metadata without saving or executing it."""
            repository, permission_error = repository_for_request("write")
            if permission_error is not None:
                return permission_error
            if not isinstance(source_yaml, str):
                return {"error": "invalid_workflow_definition"}
            if type(max_live_codex_jobs) is not int or max_live_codex_jobs not in {1, 2, 3}:
                return {"error": "invalid_workflow_definition"}
            assert repository is not None
            try:
                validation = await asyncio.to_thread(
                    repository.validate, source_yaml, max_live_codex_jobs
                )
                return _bounded_definition_response(
                    {
                        "valid": True,
                        "source_sha256": validation.source_sha256,
                        "canonical_sha256": validation.canonical_sha256,
                        "max_live_codex_jobs": validation.max_live_codex_jobs,
                    }
                )
            except DefinitionError as definition_error:
                return _definition_error(definition_error)
            except Exception:
                return {"error": "internal_error"}

        @mcp.tool()
        async def workflow_definition_create(
            mutation_request_id: str,
            source_yaml: str,
            max_live_codex_jobs: int = 1,
        ) -> dict[str, Any]:
            """Save a new immutable Workflow definition revision."""
            repository, permission_error = repository_for_request("write")
            if permission_error is not None:
                return permission_error
            if (
                not isinstance(mutation_request_id, str)
                or not isinstance(source_yaml, str)
                or type(max_live_codex_jobs) is not int
                or max_live_codex_jobs not in {1, 2, 3}
            ):
                return {"error": "invalid_workflow_definition"}
            assert repository is not None
            try:
                saved = await asyncio.to_thread(
                    repository.create,
                    mutation_request_id=mutation_request_id,
                    source_yaml=source_yaml,
                    max_live_codex_jobs=max_live_codex_jobs,
                )
                return _bounded_definition_response(_revision_metadata(saved))
            except DefinitionError as definition_error:
                return _definition_error(definition_error)
            except Exception:
                return {"error": "internal_error"}

        @mcp.tool()
        async def workflow_definition_update(
            definition_id: str,
            expected_revision: int,
            mutation_request_id: str,
            source_yaml: str,
            max_live_codex_jobs: int = 1,
        ) -> dict[str, Any]:
            """Compare-and-swap a saved Workflow definition to a new revision."""
            repository, permission_error = repository_for_request("write")
            if permission_error is not None:
                return permission_error
            if (
                not isinstance(definition_id, str)
                or type(expected_revision) is not int
                or expected_revision < 1
                or not isinstance(mutation_request_id, str)
                or not isinstance(source_yaml, str)
                or type(max_live_codex_jobs) is not int
                or max_live_codex_jobs not in {1, 2, 3}
            ):
                return {"error": "invalid_workflow_definition"}
            assert repository is not None
            try:
                saved = await asyncio.to_thread(
                    repository.update,
                    definition_id=definition_id,
                    expected_revision=expected_revision,
                    mutation_request_id=mutation_request_id,
                    source_yaml=source_yaml,
                    max_live_codex_jobs=max_live_codex_jobs,
                )
                return _bounded_definition_response(_revision_metadata(saved))
            except DefinitionError as definition_error:
                return _definition_error(definition_error)
            except Exception:
                return {"error": "internal_error"}


def _definition_error(error: DefinitionError) -> dict[str, str]:
    if isinstance(error, DefinitionInvalid):
        return {"error": "workflow_definition_invalid"}
    if isinstance(error, DefinitionNotFound):
        return {"error": "workflow_definition_not_found"}
    if isinstance(error, DefinitionMutationConflict):
        return {"error": "workflow_definition_mutation_conflict"}
    if isinstance(error, DefinitionPreconditionFailed):
        return {"error": "workflow_definition_precondition_failed"}
    if isinstance(error, DefinitionRepositoryUnavailable):
        return {"error": "workflow_definition_service_unavailable"}
    return {"error": "internal_error"}


def _bounded_definition_response(response: dict[str, Any]) -> dict[str, Any]:
    try:
        encoded = json.dumps(
            response,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        return {"error": "internal_error"}
    if len(encoded) > _MAX_DEFINITION_RESPONSE_BYTES:
        return {"error": "response_too_large"}
    return response
