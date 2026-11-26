"""Public-definition adapter for explicitly prepared definitions.sqlite v2."""

from __future__ import annotations

from pathlib import Path

from .contracts import CompiledContract
from .definition_contract import (
    CONTRACT_MISSING,
    compile_definition_contract,
    definition_contract_object,
)
from .definitions import (
    DefinitionInvalid,
    DefinitionPage,
    DefinitionRepositoryUnavailable,
    DefinitionRevision,
    DefinitionValidation,
    WorkflowContractInvalid,
    WorkflowContractRequired,
    WorkflowContractUnavailable,
    WorkflowDefinitionRepository,
)
from .strict_definitions import StrictDefinitionRevision, StrictWorkflowDefinitionStore


class StrictWorkflowDefinitionRepository(WorkflowDefinitionRepository):
    """Expose the existing strict v2 store through the definition repository shape.

    Construction never creates, migrates, or selects a database. The caller must
    explicitly prepare a v2 definitions database before using write operations.
    """

    supports_contracts = True

    def __init__(
        self,
        state_root: Path,
        *,
        store: StrictWorkflowDefinitionStore | None = None,
    ) -> None:
        super().__init__(state_root)
        self._strict_store = store or StrictWorkflowDefinitionStore(state_root)

    def validate(
        self,
        source_yaml: str,
        max_live_codex_jobs: int,
        *,
        contract: object = CONTRACT_MISSING,
        strict_contract: CompiledContract | None = None,
    ) -> DefinitionValidation:
        if contract is not CONTRACT_MISSING:
            if strict_contract is not None:
                raise DefinitionInvalid("workflow validation request is invalid") from None
            strict_contract = compile_definition_contract(contract)
        return super().validate(
            source_yaml,
            max_live_codex_jobs,
            strict_contract=strict_contract,
        )

    def create(
        self,
        *,
        mutation_request_id: str,
        source_yaml: str,
        max_live_codex_jobs: int,
        contract: object = CONTRACT_MISSING,
    ) -> DefinitionRevision:
        if contract is CONTRACT_MISSING:
            saved = self._strict_store.create(
                mutation_request_id=mutation_request_id,
                source_yaml=source_yaml,
                max_live_codex_jobs=max_live_codex_jobs,
            )
        else:
            if contract is None:
                compile_definition_contract(contract)
            try:
                saved = self._strict_store.create(
                    mutation_request_id=mutation_request_id,
                    source_yaml=source_yaml,
                    max_live_codex_jobs=max_live_codex_jobs,
                    contract=contract,
                )
            except DefinitionRepositoryUnavailable:
                raise WorkflowContractUnavailable(
                    "workflow contract storage is unavailable"
                ) from None
        return _definition_revision(saved)

    def update(
        self,
        *,
        definition_id: str,
        expected_revision: int,
        mutation_request_id: str,
        source_yaml: str,
        max_live_codex_jobs: int,
        contract: object = CONTRACT_MISSING,
    ) -> DefinitionRevision:
        if contract is CONTRACT_MISSING:
            saved = self._strict_store.update(
                definition_id=definition_id,
                expected_revision=expected_revision,
                mutation_request_id=mutation_request_id,
                source_yaml=source_yaml,
                max_live_codex_jobs=max_live_codex_jobs,
            )
        elif contract is None:
            current = self._strict_store.get(definition_id, expected_revision)
            if current.is_strict:
                raise WorkflowContractRequired("workflow contract is required") from None
            raise WorkflowContractInvalid("workflow contract is invalid") from None
        else:
            try:
                saved = self._strict_store.update(
                    definition_id=definition_id,
                    expected_revision=expected_revision,
                    mutation_request_id=mutation_request_id,
                    source_yaml=source_yaml,
                    max_live_codex_jobs=max_live_codex_jobs,
                    contract=contract,
                )
            except DefinitionRepositoryUnavailable:
                raise WorkflowContractUnavailable(
                    "workflow contract storage is unavailable"
                ) from None
        return _definition_revision(saved)

    def get(self, definition_id: str, revision: int | None = None) -> DefinitionRevision:
        return _definition_revision(self._strict_store.get(definition_id, revision))

    def revision_for_etag(self, definition_id: str, etag: str) -> int | None:
        return self._strict_store.revision_for_etag(definition_id, etag)

    def list(self, limit: int = 50, cursor: str | None = None) -> DefinitionPage:
        return self._strict_store.list(limit, cursor)


def _definition_revision(saved: StrictDefinitionRevision) -> DefinitionRevision:
    contract = (
        definition_contract_object(saved.contract_json_canonical)
        if saved.contract_json_canonical is not None
        else None
    )
    return DefinitionRevision(
        definition_id=saved.definition_id,
        revision=saved.revision,
        source_yaml=saved.source_yaml,
        source_sha256=saved.source_sha256,
        canonical_sha256=saved.canonical_sha256,
        max_live_codex_jobs=saved.max_live_codex_jobs,
        created_at=saved.created_at,
        etag=saved.etag,
        definition=saved.definition,
        contract_version=(saved.contract.contract_version if saved.contract is not None else None),
        contract_sha256=saved.contract_sha256,
        binding_digest=saved.binding_digest,
        contract=contract,
    )
