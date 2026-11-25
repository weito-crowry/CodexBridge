from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from typing import Any, cast

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from pydantic import Field

from ..config import BridgeConfig
from .definition_api import _revision_detail_json, _revision_metadata, _summary_json
from .definition_contract import (
    CONTRACT_MISSING,
    compile_definition_contract,
    compiled_contract_for_revision,
    validate_definition_source,
)
from .definitions import (
    DefinitionError,
    DefinitionInvalid,
    DefinitionMutationConflict,
    DefinitionNotFound,
    DefinitionPreconditionFailed,
    DefinitionRepositoryUnavailable,
    DefinitionRevision,
    WorkflowContractInvalid,
    WorkflowContractRequired,
    WorkflowContractUnavailable,
    WorkflowDefinitionRepository,
)
from .graph import project_workflow_graph

_MAX_DEFINITION_RESPONSE_BYTES = 256 * 1024
_CONTRACT_ARGUMENT_FIELD: Any = Field(default_factory=lambda: CONTRACT_MISSING)


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
            ctx: Context, limit: int = 50, cursor: str | None = None
        ) -> dict[str, Any]:
            """List bounded metadata for saved Workflow definitions."""
            repository, permission_error = repository_for_request("read")
            if permission_error is not None:
                return permission_error
            if _has_unexpected_arguments(ctx, {"limit", "cursor"}):
                return {"error": "workflow_definition_invalid"}
            if type(limit) is not int or not 1 <= limit <= 100:
                return {"error": "invalid_limit"}
            if cursor is not None and not isinstance(cursor, str):
                return {"error": "invalid_cursor"}
            assert repository is not None
            try:
                page = await asyncio.to_thread(repository.list, limit, cursor)
                return _bounded_definition_response(
                    {
                        "definitions": [
                            _summary_json(item, include_contract=True) for item in page.items
                        ],
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
            ctx: Context, definition_id: str, revision: int | None = None
        ) -> dict[str, Any]:
            """Read one immutable Workflow definition revision, including source YAML."""
            repository, permission_error = repository_for_request("read")
            if permission_error is not None:
                return permission_error
            if _has_unexpected_arguments(ctx, {"definition_id", "revision"}):
                return {"error": "workflow_definition_invalid"}
            if not isinstance(definition_id, str):
                return {"error": "invalid_definition_id"}
            if revision is not None and (type(revision) is not int or revision < 1):
                return {"error": "invalid_revision"}
            assert repository is not None
            try:
                saved = await asyncio.to_thread(repository.get, definition_id, revision)
                return _bounded_definition_response(
                    _revision_detail_json(saved, include_contract=True)
                )
            except DefinitionError as definition_error:
                return _definition_error(definition_error)
            except Exception:
                return {"error": "internal_error"}

        @mcp.tool()
        async def workflow_graph_get(
            ctx: Context, definition_id: str, revision: int | None = None
        ) -> dict[str, Any]:
            """Project a saved Workflow definition into a static graph without executing it."""
            repository, permission_error = repository_for_request("read")
            if permission_error is not None:
                return permission_error
            if _has_unexpected_arguments(ctx, {"definition_id", "revision"}):
                return {"error": "workflow_definition_invalid"}
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
                        strict_contract=compiled_contract_for_revision(saved),
                    )
                    return saved, graph

                saved, graph = await asyncio.to_thread(project)
                response = {
                    "definition_id": saved.definition_id,
                    "revision": saved.revision,
                    "etag": saved.etag,
                    "graph": graph,
                }
                if saved.contract_version is not None:
                    response.update(
                        {
                            key: value
                            for key, value in _revision_metadata(
                                saved, include_contract=True
                            ).items()
                            if key in {"contract_version", "contract_sha256", "binding_digest"}
                        }
                    )
                return _bounded_definition_response(response)
            except DefinitionError as definition_error:
                return _definition_error(definition_error)
            except Exception:
                return {"error": "internal_error"}

    if "write" in config.workflow_mcp_permissions:

        @mcp.tool()
        async def workflow_definition_validate(
            ctx: Context,
            source_yaml: str,
            max_live_codex_jobs: int = 1,
            contract: dict[str, Any] | None = _CONTRACT_ARGUMENT_FIELD,
        ) -> dict[str, Any]:
            """Validate source YAML and return safe metadata without saving or executing it."""
            repository, permission_error = repository_for_request("write")
            if permission_error is not None:
                return permission_error
            if _has_unexpected_arguments(ctx, {"source_yaml", "max_live_codex_jobs", "contract"}):
                return {"error": "workflow_definition_invalid"}
            if not isinstance(source_yaml, str):
                return {"error": "invalid_workflow_definition"}
            if type(max_live_codex_jobs) is not int or max_live_codex_jobs not in {1, 2, 3}:
                return {"error": "invalid_workflow_definition"}
            assert repository is not None
            try:
                validation = await asyncio.to_thread(
                    validate_definition_source,
                    repository,
                    source_yaml,
                    max_live_codex_jobs,
                    contract=contract,
                )
                result = {
                    "valid": True,
                    "source_sha256": validation.source_sha256,
                    "canonical_sha256": validation.canonical_sha256,
                    "max_live_codex_jobs": validation.max_live_codex_jobs,
                }
                if validation.contract_version is not None:
                    result["contract_version"] = validation.contract_version
                    result["contract_sha256"] = validation.contract_sha256
                return _bounded_definition_response(result)
            except DefinitionError as definition_error:
                return _definition_error(definition_error)
            except Exception:
                return {"error": "internal_error"}

        @mcp.tool()
        async def workflow_definition_create(
            ctx: Context,
            mutation_request_id: str,
            source_yaml: str,
            max_live_codex_jobs: int = 1,
            contract: dict[str, Any] | None = _CONTRACT_ARGUMENT_FIELD,
        ) -> dict[str, Any]:
            """Save a new immutable Workflow definition revision."""
            repository, permission_error = repository_for_request("write")
            if permission_error is not None:
                return permission_error
            if _has_unexpected_arguments(
                ctx, {"mutation_request_id", "source_yaml", "max_live_codex_jobs", "contract"}
            ):
                return {"error": "workflow_definition_invalid"}
            if (
                not isinstance(mutation_request_id, str)
                or not isinstance(source_yaml, str)
                or type(max_live_codex_jobs) is not int
                or max_live_codex_jobs not in {1, 2, 3}
            ):
                return {"error": "invalid_workflow_definition"}
            assert repository is not None
            try:
                create_kwargs: dict[str, Any] = {}
                if contract is not CONTRACT_MISSING:
                    if contract is None:
                        compile_definition_contract(contract)
                    if not bool(getattr(repository, "supports_contracts", False)):
                        compile_definition_contract(contract)
                        raise WorkflowContractUnavailable(
                            "workflow contract storage is unavailable"
                        ) from None
                    create_kwargs["contract"] = contract
                saved = await asyncio.to_thread(
                    repository.create,
                    mutation_request_id=mutation_request_id,
                    source_yaml=source_yaml,
                    max_live_codex_jobs=max_live_codex_jobs,
                    **create_kwargs,
                )
                return _bounded_definition_response(
                    _revision_metadata(saved, include_contract=True)
                )
            except DefinitionError as definition_error:
                return _definition_error(definition_error)
            except Exception:
                return {"error": "internal_error"}

        @mcp.tool()
        async def workflow_definition_update(
            ctx: Context,
            definition_id: str,
            expected_revision: int,
            mutation_request_id: str,
            source_yaml: str,
            max_live_codex_jobs: int = 1,
            contract: dict[str, Any] | None = _CONTRACT_ARGUMENT_FIELD,
        ) -> dict[str, Any]:
            """Compare-and-swap a saved Workflow definition to a new revision."""
            repository, permission_error = repository_for_request("write")
            if permission_error is not None:
                return permission_error
            if _has_unexpected_arguments(
                ctx,
                {
                    "definition_id",
                    "expected_revision",
                    "mutation_request_id",
                    "source_yaml",
                    "max_live_codex_jobs",
                    "contract",
                },
            ):
                return {"error": "workflow_definition_invalid"}
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
                update_kwargs: dict[str, Any] = {}
                if contract is not CONTRACT_MISSING:
                    if contract is None and not bool(
                        getattr(repository, "supports_contracts", False)
                    ):
                        compile_definition_contract(contract)
                    elif contract is not None and not bool(
                        getattr(repository, "supports_contracts", False)
                    ):
                        compile_definition_contract(contract)
                        raise WorkflowContractUnavailable(
                            "workflow contract storage is unavailable"
                        ) from None
                    update_kwargs["contract"] = contract
                saved = await asyncio.to_thread(
                    repository.update,
                    definition_id=definition_id,
                    expected_revision=expected_revision,
                    mutation_request_id=mutation_request_id,
                    source_yaml=source_yaml,
                    max_live_codex_jobs=max_live_codex_jobs,
                    **update_kwargs,
                )
                return _bounded_definition_response(
                    _revision_metadata(saved, include_contract=True)
                )
            except DefinitionError as definition_error:
                return _definition_error(definition_error)
            except Exception:
                return {"error": "internal_error"}


def _definition_error(error: DefinitionError) -> dict[str, str]:
    if isinstance(error, WorkflowContractInvalid):
        return {"error": "workflow_contract_invalid"}
    if isinstance(error, WorkflowContractRequired):
        return {"error": "workflow_contract_required"}
    if isinstance(error, WorkflowContractUnavailable):
        return {"error": "workflow_contract_unavailable"}
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


def _has_unexpected_arguments(ctx: Context, allowed: set[str]) -> bool:
    try:
        params = ctx.request_context.params
    except Exception:
        return False
    if not isinstance(params, Mapping):
        return False
    arguments = params.get("arguments")
    if not isinstance(arguments, Mapping):
        return False
    return any(not isinstance(key, str) or key not in allowed for key in arguments)


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
