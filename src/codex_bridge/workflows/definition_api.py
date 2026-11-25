from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .api import (
    WorkflowPayloadTooLarge,
    _authorization_error,
    _json_payload,
    _snapshot_json,
)
from .definition_contract import CONTRACT_MISSING, compile_definition_contract
from .definitions import (
    DefinitionError,
    DefinitionInvalid,
    DefinitionMutationConflict,
    DefinitionNotFound,
    DefinitionPreconditionFailed,
    DefinitionRepositoryUnavailable,
    DefinitionRevision,
    DefinitionSummary,
    WorkflowContractInvalid,
    WorkflowContractRequired,
    WorkflowContractUnavailable,
    WorkflowDefinitionRepository,
)
from .graph import project_workflow_graph
from .service import (
    WorkflowConcurrencyConflict,
    WorkflowReconciliationRequired,
    WorkflowRequestConflict,
    WorkflowRequestError,
    WorkflowService,
    WorkflowServiceUnavailable,
    WorkflowStartFailed,
)

_MAX_SOURCE_YAML_BYTES = 64 * 1024
_MAX_HTTP_RESPONSE_BYTES = 1024 * 1024
_STRONG_ETAG = re.compile(r'"workflow-definition-v[12]-[0-9a-f]{64}"\Z', re.ASCII)


def create_workflow_definition_routes(
    repository: WorkflowDefinitionRepository | None,
    *,
    service: WorkflowService | None = None,
    token: str,
    ui_port: int,
    authorize: Callable[[Request, str | None], bool],
    ready: Callable[[], bool] | None = None,
) -> list[Route]:
    """Create authenticated, read-only/definition-mutation Control routes."""

    def authorize_request(request: Request) -> Response | None:
        return _authorization_error(request, token, ui_port, authorize)

    async def list_definitions(request: Request) -> Response:
        forbidden = authorize_request(request)
        if forbidden is not None:
            return forbidden
        if repository is None:
            return _definition_error(DefinitionRepositoryUnavailable("repository unavailable"))
        try:
            query = _strict_query(request, {"limit", "cursor"})
            limit = _parse_positive_query_integer(query.get("limit", "50"), maximum=100)
            cursor = query.get("cursor")
            page = await asyncio.to_thread(repository.list, limit, cursor)
            return _bounded_json_response(
                {
                    "definitions": [
                        _summary_json(item, include_contract=True) for item in page.items
                    ],
                    "limit": limit,
                    "next_cursor": page.next_cursor,
                }
            )
        except DefinitionError as error:
            return _definition_error(error)
        except Exception:
            return _internal_error()

    async def create_definition(request: Request) -> Response:
        forbidden = authorize_request(request)
        if forbidden is not None:
            return forbidden
        if repository is None:
            return _definition_error(DefinitionRepositoryUnavailable("repository unavailable"))
        try:
            payload = await _request_json(request)
            mutation_request_id, source_yaml, profile, contract_value = _mutation_fields(
                payload, allow_contract=True
            )
            create_kwargs: dict[str, Any] = {}
            if contract_value is not CONTRACT_MISSING:
                if not _supports_contracts(repository):
                    compile_definition_contract(contract_value)
                    raise WorkflowContractUnavailable(
                        "workflow contract storage is unavailable"
                    ) from None
                if contract_value is None:
                    compile_definition_contract(contract_value)
                create_kwargs["contract"] = contract_value
            created = await asyncio.to_thread(
                repository.create,
                mutation_request_id=mutation_request_id,
                source_yaml=source_yaml,
                max_live_codex_jobs=profile,
                **create_kwargs,
            )
            return _bounded_json_response(
                _revision_metadata(created, include_contract=True),
                status_code=201,
                etag=created.etag,
            )
        except WorkflowPayloadTooLarge:
            return _payload_too_large()
        except WorkflowRequestError:
            return _invalid_error()
        except _PayloadTooLarge:
            return _payload_too_large()
        except DefinitionError as error:
            return _definition_error(error)
        except Exception:
            return _internal_error()

    async def validate_definition(request: Request) -> Response:
        forbidden = authorize_request(request)
        if forbidden is not None:
            return forbidden
        if repository is None:
            return _definition_error(DefinitionRepositoryUnavailable("repository unavailable"))
        try:
            payload = await _request_json(request)
            if set(payload) - {"source_yaml", "max_live_codex_jobs", "contract"} or not {
                "source_yaml",
                "max_live_codex_jobs",
            } <= set(payload):
                raise DefinitionInvalid("workflow validation fields are invalid")
            source_yaml, profile = _source_fields(
                payload.get("source_yaml"), payload.get("max_live_codex_jobs")
            )
            validation = await asyncio.to_thread(repository.validate, source_yaml, profile)
            if "contract" in payload:
                compiled = compile_definition_contract(payload["contract"])
                validation = replace(
                    validation,
                    contract_version=compiled.contract_version,
                    contract_sha256=compiled.contract_sha256,
                )
            validation_json = {
                "valid": True,
                "source_sha256": validation.source_sha256,
                "canonical_sha256": validation.canonical_sha256,
                "max_live_codex_jobs": validation.max_live_codex_jobs,
            }
            if validation.contract_version is not None:
                validation_json["contract_version"] = validation.contract_version
                validation_json["contract_sha256"] = validation.contract_sha256
            return _bounded_json_response(validation_json)
        except WorkflowPayloadTooLarge:
            return _payload_too_large()
        except WorkflowRequestError:
            return _invalid_error()
        except _PayloadTooLarge:
            return _payload_too_large()
        except DefinitionError as error:
            return _definition_error(error)
        except Exception:
            return _internal_error()

    async def start_saved_run(request: Request) -> Response:
        forbidden = authorize_request(request)
        if forbidden is not None:
            return forbidden
        if service is None or repository is None:
            return JSONResponse({"error": "workflow_service_unavailable"}, status_code=503)
        try:
            payload = await _request_json(request)
            if set(payload) - {"client_request_id", "revision", "workspace_root", "input"}:
                raise WorkflowRequestError("request contains unsupported fields")
            if not {"client_request_id", "revision", "workspace_root"} <= set(payload):
                raise WorkflowRequestError("request is missing required fields")
            client_request_id = payload["client_request_id"]
            revision = payload["revision"]
            workspace_root = payload["workspace_root"]
            if not isinstance(client_request_id, str):
                raise WorkflowRequestError("client_request_id must be a string")
            if type(revision) is not int or revision < 1:
                raise WorkflowRequestError("revision must be a positive integer")
            if not isinstance(workspace_root, str):
                raise WorkflowRequestError("workspace_root must be a string")
            if ready is None or not ready():
                return JSONResponse({"error": "bridge_not_ready"}, status_code=503)
            snapshot = await service.start_saved_workflow(
                repository=repository,
                definition_id=request.path_params["definition_id"],
                revision=revision,
                client_request_id=client_request_id,
                input_data=payload.get("input", {}),
                workspace_root=workspace_root,
            )
            return JSONResponse(
                {
                    **_snapshot_json(snapshot),
                    "definition_id": request.path_params["definition_id"],
                    "revision": revision,
                },
                status_code=202,
            )
        except WorkflowPayloadTooLarge:
            return _payload_too_large()
        except WorkflowRequestError:
            return _invalid_error()
        except WorkflowRequestConflict:
            return JSONResponse({"error": "request_id_conflict"}, status_code=409)
        except WorkflowConcurrencyConflict:
            return JSONResponse({"error": "workflow_capacity_reached"}, status_code=409)
        except WorkflowStartFailed as error:
            return JSONResponse(
                {
                    "error": "workflow_worker_not_ready",
                    **_snapshot_json(error.snapshot),
                    "definition_id": request.path_params["definition_id"],
                    "revision": revision,
                },
                status_code=503,
            )
        except WorkflowReconciliationRequired as error:
            return JSONResponse(
                {
                    "error": "workflow_needs_reconciliation",
                    **_snapshot_json(error.snapshot),
                    "definition_id": request.path_params["definition_id"],
                    "revision": revision,
                },
                status_code=409,
            )
        except WorkflowServiceUnavailable:
            return JSONResponse({"error": "workflow_service_unavailable"}, status_code=503)
        except DefinitionError as error:
            return _definition_error(error)
        except Exception:
            return _internal_error()

    async def get_definition(request: Request) -> Response:
        forbidden = authorize_request(request)
        if forbidden is not None:
            return forbidden
        if repository is None:
            return _definition_error(DefinitionRepositoryUnavailable("repository unavailable"))
        try:
            _strict_query(request, set())
            revision = await asyncio.to_thread(repository.get, request.path_params["definition_id"])
            return _bounded_json_response(
                _revision_detail_json(revision, include_contract=True), etag=revision.etag
            )
        except DefinitionError as error:
            return _definition_error(error)
        except Exception:
            return _internal_error()

    async def get_revision(request: Request) -> Response:
        forbidden = authorize_request(request)
        if forbidden is not None:
            return forbidden
        if repository is None:
            return _definition_error(DefinitionRepositoryUnavailable("repository unavailable"))
        try:
            _strict_query(request, set())
            revision_number = _parse_path_revision(request.path_params["revision"])
            revision = await asyncio.to_thread(
                repository.get,
                request.path_params["definition_id"],
                revision_number,
            )
            return _bounded_json_response(
                _revision_detail_json(revision, include_contract=True), etag=revision.etag
            )
        except DefinitionError as error:
            return _definition_error(error)
        except Exception:
            return _internal_error()

    async def update_definition(request: Request) -> Response:
        forbidden = authorize_request(request)
        if forbidden is not None:
            return forbidden
        if repository is None:
            return _definition_error(DefinitionRepositoryUnavailable("repository unavailable"))
        if_match_values = request.headers.getlist("if-match")
        if not if_match_values:
            return JSONResponse({"error": "precondition_required"}, status_code=428)
        if len(if_match_values) != 1:
            return _invalid_error()
        etag = if_match_values[0]
        if not _STRONG_ETAG.fullmatch(etag):
            return _invalid_error()
        if etag.startswith('"workflow-definition-v2-') and not _supports_contracts(repository):
            return _definition_error(
                DefinitionPreconditionFailed("workflow definition revision precondition failed")
            )
        try:
            payload = await _request_json(request)
            mutation_request_id, source_yaml, profile, contract_value = _mutation_fields(
                payload, allow_contract=True
            )

            definition_id = request.path_params["definition_id"]

            def update_from_etag() -> DefinitionRevision:
                expected_revision = repository.revision_for_etag(definition_id, etag)
                if expected_revision is None:
                    raise DefinitionPreconditionFailed(
                        "workflow definition revision precondition failed"
                    )
                original = repository.get(definition_id, expected_revision)
                if original.etag != etag:
                    raise DefinitionPreconditionFailed(
                        "workflow definition revision precondition failed"
                    )
                update_kwargs: dict[str, Any] = {}
                if contract_value is not CONTRACT_MISSING:
                    if contract_value is None and _supports_contracts(repository):
                        update_kwargs["contract"] = None
                    else:
                        if not _supports_contracts(repository):
                            compile_definition_contract(contract_value)
                            raise WorkflowContractUnavailable(
                                "workflow contract storage is unavailable"
                            ) from None
                        update_kwargs["contract"] = contract_value
                return repository.update(
                    definition_id=definition_id,
                    expected_revision=expected_revision,
                    mutation_request_id=mutation_request_id,
                    source_yaml=source_yaml,
                    max_live_codex_jobs=profile,
                    **update_kwargs,
                )

            updated = await asyncio.to_thread(update_from_etag)
            return _bounded_json_response(
                _revision_metadata(updated, include_contract=True), etag=updated.etag
            )
        except WorkflowPayloadTooLarge:
            return _payload_too_large()
        except WorkflowRequestError:
            return _invalid_error()
        except _PayloadTooLarge:
            return _payload_too_large()
        except DefinitionError as error:
            return _definition_error(error)
        except Exception:
            return _internal_error()

    async def get_graph(request: Request) -> Response:
        forbidden = authorize_request(request)
        if forbidden is not None:
            return forbidden
        if repository is None:
            return _definition_error(DefinitionRepositoryUnavailable("repository unavailable"))
        try:
            query = _strict_query(request, {"revision"})
            revision_number = (
                _parse_positive_query_integer(query["revision"]) if "revision" in query else None
            )
            definition_id = request.path_params["definition_id"]

            def project() -> tuple[DefinitionRevision, dict[str, Any]]:
                revision = repository.get(definition_id, revision_number)
                graph = project_workflow_graph(
                    revision.definition,
                    codex_job_live=True,
                    max_live_codex_jobs=revision.max_live_codex_jobs,
                )
                return revision, graph

            revision, graph = await asyncio.to_thread(project)
            return _bounded_json_response(
                {
                    "definition_id": revision.definition_id,
                    "revision": revision.revision,
                    "etag": revision.etag,
                    **(_contract_metadata(revision) if _has_contract(revision) else {}),
                    "graph": graph,
                },
                etag=revision.etag,
            )
        except DefinitionError as error:
            return _definition_error(error)
        except Exception:
            return _internal_error()

    # Keep the static validation path ahead of the dynamic definition ID route.
    return [
        Route("/ui-api/control/workflow-definitions", list_definitions, methods=["GET"]),
        Route("/ui-api/control/workflow-definitions", create_definition, methods=["POST"]),
        Route(
            "/ui-api/control/workflow-definitions/validate",
            validate_definition,
            methods=["POST"],
        ),
        Route(
            "/ui-api/control/workflow-definitions/{definition_id}/runs",
            start_saved_run,
            methods=["POST"],
        ),
        Route(
            "/ui-api/control/workflow-definitions/{definition_id}/revisions/{revision}",
            get_revision,
            methods=["GET"],
        ),
        Route(
            "/ui-api/control/workflow-definitions/{definition_id}/graph",
            get_graph,
            methods=["GET"],
        ),
        Route(
            "/ui-api/control/workflow-definitions/{definition_id}",
            update_definition,
            methods=["PUT"],
        ),
        Route(
            "/ui-api/control/workflow-definitions/{definition_id}",
            get_definition,
            methods=["GET"],
        ),
    ]


async def _request_json(request: Request) -> dict[str, Any]:
    return await _json_payload(request)


def _mutation_fields(
    payload: dict[str, Any], *, allow_contract: bool = False
) -> tuple[str, str, int, object]:
    required = {"mutation_request_id", "source_yaml", "max_live_codex_jobs"}
    allowed = required | ({"contract"} if allow_contract else set())
    if not required <= set(payload) or set(payload) - allowed:
        raise DefinitionInvalid("workflow definition fields are invalid")
    mutation_request_id = payload.get("mutation_request_id")
    if not isinstance(mutation_request_id, str):
        raise DefinitionInvalid("mutation request ID is invalid")
    source_yaml, profile = _source_fields(
        payload.get("source_yaml"), payload.get("max_live_codex_jobs")
    )
    contract = payload["contract"] if "contract" in payload else CONTRACT_MISSING
    return mutation_request_id, source_yaml, profile, contract


def _source_fields(source_yaml: object, profile: object) -> tuple[str, int]:
    if not isinstance(source_yaml, str) or type(profile) is not int or profile not in {1, 2, 3}:
        raise DefinitionInvalid("workflow definition fields are invalid")
    try:
        source_size = len(source_yaml.encode("utf-8", errors="strict"))
    except UnicodeEncodeError:
        raise DefinitionInvalid("workflow source YAML is invalid") from None
    if source_size > _MAX_SOURCE_YAML_BYTES:
        raise _PayloadTooLarge
    return source_yaml, profile


def _strict_query(request: Request, allowed: set[str]) -> dict[str, str]:
    items = request.query_params.multi_items()
    keys = [key for key, _ in items]
    if any(key not in allowed for key in keys) or len(keys) != len(set(keys)):
        raise DefinitionInvalid("workflow definition query is invalid")
    return dict(items)


def _parse_positive_query_integer(value: str, *, maximum: int | None = None) -> int:
    if not value or len(value) > 19 or not value.isascii() or not value.isdecimal():
        raise DefinitionInvalid("workflow definition integer is invalid")
    parsed = int(value)
    if parsed < 1 or (maximum is not None and parsed > maximum):
        raise DefinitionInvalid("workflow definition integer is invalid")
    if str(parsed) != value:
        raise DefinitionInvalid("workflow definition integer is invalid")
    return parsed


def _parse_path_revision(value: str) -> int:
    return _parse_positive_query_integer(value)


def _summary_json(summary: DefinitionSummary, *, include_contract: bool = False) -> dict[str, Any]:
    result = {
        "definition_id": summary.definition_id,
        "latest_revision": summary.latest_revision,
        "document_name": summary.document_name,
        "source_sha256": summary.source_sha256,
        "canonical_sha256": summary.canonical_sha256,
        "max_live_codex_jobs": summary.max_live_codex_jobs,
        "created_at": summary.created_at,
        "updated_at": summary.updated_at,
        "etag": summary.etag,
    }
    if include_contract and summary.contract_version is not None:
        result.update(
            {
                "contract_version": summary.contract_version,
                "contract_sha256": summary.contract_sha256,
                "binding_digest": summary.binding_digest,
            }
        )
    return result


def _revision_metadata(
    revision: DefinitionRevision, *, include_contract: bool = False
) -> dict[str, Any]:
    result = {
        "definition_id": revision.definition_id,
        "revision": revision.revision,
        "source_sha256": revision.source_sha256,
        "canonical_sha256": revision.canonical_sha256,
        "max_live_codex_jobs": revision.max_live_codex_jobs,
        "created_at": revision.created_at,
        "etag": revision.etag,
    }
    if include_contract and _has_contract(revision):
        result.update(_contract_metadata(revision))
    return result


def _revision_detail_json(
    revision: DefinitionRevision, *, include_contract: bool = False
) -> dict[str, Any]:
    document = revision.definition.get("document")
    document_name = document.get("name") if isinstance(document, dict) else None
    result = {
        **_revision_metadata(revision, include_contract=include_contract),
        "document_name": document_name,
        "source_yaml": revision.source_yaml,
    }
    if include_contract and _has_contract(revision):
        result["contract"] = revision.contract
    return result


def _contract_metadata(revision: DefinitionRevision) -> dict[str, Any]:
    return {
        "contract_version": revision.contract_version,
        "contract_sha256": revision.contract_sha256,
        "binding_digest": revision.binding_digest,
    }


def _has_contract(revision: DefinitionRevision) -> bool:
    return revision.contract_version is not None


def _supports_contracts(repository: WorkflowDefinitionRepository) -> bool:
    return bool(getattr(repository, "supports_contracts", False))


def _bounded_json_response(
    payload: dict[str, Any], *, status_code: int = 200, etag: str | None = None
) -> JSONResponse:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        return _internal_error()
    if len(encoded) > _MAX_HTTP_RESPONSE_BYTES:
        return _payload_too_large(response=True)
    headers = {"ETag": etag} if etag is not None else None
    return JSONResponse(payload, status_code=status_code, headers=headers)


def _definition_error(error: DefinitionError) -> JSONResponse:
    if isinstance(error, WorkflowContractInvalid):
        return JSONResponse({"error": "workflow_contract_invalid"}, status_code=400)
    if isinstance(error, WorkflowContractRequired):
        return JSONResponse({"error": "workflow_contract_required"}, status_code=400)
    if isinstance(error, WorkflowContractUnavailable):
        return JSONResponse({"error": "workflow_contract_unavailable"}, status_code=503)
    if isinstance(error, DefinitionInvalid):
        return JSONResponse({"error": "invalid"}, status_code=400)
    if isinstance(error, DefinitionNotFound):
        return JSONResponse({"error": "not_found"}, status_code=404)
    if isinstance(error, DefinitionMutationConflict):
        return JSONResponse({"error": "mutation_conflict"}, status_code=409)
    if isinstance(error, DefinitionPreconditionFailed):
        return JSONResponse({"error": "precondition_failed"}, status_code=412)
    if isinstance(error, DefinitionRepositoryUnavailable):
        return JSONResponse({"error": "repository_unavailable"}, status_code=503)
    return _internal_error()


def _invalid_error() -> JSONResponse:
    return JSONResponse({"error": "invalid"}, status_code=400)


def _payload_too_large(*, response: bool = False) -> JSONResponse:
    return JSONResponse(
        {"error": "response_too_large" if response else "payload_too_large"},
        status_code=413,
    )


def _internal_error() -> JSONResponse:
    return JSONResponse({"error": "internal"}, status_code=500)


class _PayloadTooLarge(Exception):
    pass
