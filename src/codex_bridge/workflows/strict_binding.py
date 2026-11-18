"""Read-only verification across isolated strict workflow stores.

This opt-in adapter joins a strict request reservation to its pinned immutable
definition revision. It does not admit or start workflow runs.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contracts import canonical_json, normalize_input, sha256_json
from .strict_definitions import StrictDefinitionRevision, StrictWorkflowDefinitionStore
from .strict_requests import (
    StrictWorkflowRequestReservation,
    StrictWorkflowRequestStore,
)

_ERROR = "strict workflow binding verification failed"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)


class StrictWorkflowBindingError(RuntimeError):
    """Stable, value-free error for a failed cross-database verification."""

    def __init__(self) -> None:
        super().__init__(_ERROR)


@dataclass(frozen=True, slots=True, repr=False)
class StrictWorkflowBindingMetadata:
    """Verified identities and hashes, without contract or input contents."""

    workflow_id: str = field(repr=False)
    run_id: str = field(repr=False)
    request_digest: str = field(repr=False)
    definition_id: str = field(repr=False)
    definition_revision: int = field(repr=False)
    source_sha256: str = field(repr=False)
    contract_sha256: str = field(repr=False)
    binding_digest: str = field(repr=False)
    normalized_input_sha256: str = field(repr=False)
    run_format_version: int = field(repr=False)
    contract_version: int = field(repr=False)
    normalized_input_snapshot_verified: bool = field(repr=False)

    def __repr__(self) -> str:
        return "StrictWorkflowBindingMetadata(<verified>)"


@dataclass(frozen=True, slots=True, repr=False)
class StrictWorkflowInputNormalization:
    """Isolated normalized input and its digest; input is omitted from repr."""

    definition_id: str = field(repr=False)
    definition_revision: int = field(repr=False)
    normalized_input: dict[str, Any] = field(repr=False)
    normalized_input_sha256: str = field(repr=False)

    def __repr__(self) -> str:
        return "StrictWorkflowInputNormalization(<redacted>)"


class StrictWorkflowBindingVerifier:
    """Opt-in, read-only verifier for definitions.sqlite and requests.sqlite."""

    def __init__(self, state_root: Path) -> None:
        self._requests = StrictWorkflowRequestStore(state_root)
        self._definitions = StrictWorkflowDefinitionStore(state_root)

    def verify(
        self,
        run_id: str,
        *,
        expected_workflow_id: str | None = None,
        expected_request_digest: str | None = None,
    ) -> StrictWorkflowBindingMetadata:
        """Verify the request against its exact pinned strict revision.

        Caller-provided expected values are comparisons only. They are not an
        independent proof of the identity supplied by either database.
        """
        self._validate_expectations(expected_workflow_id, expected_request_digest)
        try:
            request, definition = self._read_pair(run_id)
            self._check_pair(request, definition)
            if expected_workflow_id is not None and request.workflow_id != expected_workflow_id:
                raise StrictWorkflowBindingError()
            if (
                expected_request_digest is not None
                and request.request_digest != expected_request_digest
            ):
                raise StrictWorkflowBindingError()
            self._confirm_stable(run_id, request, definition)
            return self._metadata(request, normalized_input_snapshot_verified=False)
        except StrictWorkflowBindingError:
            raise
        except Exception:
            raise StrictWorkflowBindingError() from None

    def normalize_for_definition(
        self,
        definition_id: str,
        revision: int,
        raw_input: object,
    ) -> StrictWorkflowInputNormalization:
        """Normalize raw JSON input once against an exact strict revision."""
        try:
            definition = self._definitions.get(definition_id, revision)
            contract = definition.contract
            if not definition.is_strict or contract is None:
                raise StrictWorkflowBindingError()
            normalized = normalize_input(contract, raw_input)
            # The I1 normalizer already returns a JSON-isolated copy. Reparse
            # canonical JSON here to keep the returned object detached as well.
            isolated = json.loads(canonical_json(normalized))
            if type(isolated) is not dict:
                raise StrictWorkflowBindingError()
            digest = sha256_json(isolated)
            self._confirm_definition_stable(definition)
            return StrictWorkflowInputNormalization(
                definition_id=definition.definition_id,
                definition_revision=definition.revision,
                normalized_input=isolated,
                normalized_input_sha256=digest,
            )
        except StrictWorkflowBindingError:
            raise
        except Exception:
            raise StrictWorkflowBindingError() from None

    def verify_normalized_input(
        self,
        run_id: str,
        snapshot: object,
        *,
        expected_workflow_id: str | None = None,
        expected_request_digest: str | None = None,
    ) -> StrictWorkflowBindingMetadata:
        """Verify an explicitly supplied post-normalization input snapshot.

        A snapshot is accepted only when it is already complete: applying the
        pinned contract's defaults must leave its canonical JSON unchanged.
        This validates the supplied snapshot against the request hash; it does
        not establish where that snapshot came from or authenticate a manifest.
        """
        self._validate_expectations(expected_workflow_id, expected_request_digest)
        try:
            request, definition = self._read_pair(run_id)
            self._check_pair(request, definition)
            if expected_workflow_id is not None and request.workflow_id != expected_workflow_id:
                raise StrictWorkflowBindingError()
            if (
                expected_request_digest is not None
                and request.request_digest != expected_request_digest
            ):
                raise StrictWorkflowBindingError()
            contract = definition.contract
            if contract is None:
                raise StrictWorkflowBindingError()
            snapshot_json = canonical_json(snapshot)
            isolated_snapshot = json.loads(snapshot_json)
            if type(isolated_snapshot) is not dict:
                raise StrictWorkflowBindingError()
            normalized = normalize_input(contract, isolated_snapshot)
            if canonical_json(normalized) != snapshot_json:
                raise StrictWorkflowBindingError()
            if sha256_json(isolated_snapshot) != request.normalized_input_sha256:
                raise StrictWorkflowBindingError()
            self._confirm_stable(run_id, request, definition)
            return self._metadata(request, normalized_input_snapshot_verified=True)
        except StrictWorkflowBindingError:
            raise
        except Exception:
            raise StrictWorkflowBindingError() from None

    def _read_pair(
        self, run_id: str
    ) -> tuple[StrictWorkflowRequestReservation, StrictDefinitionRevision]:
        request = self._requests.read_binding(run_id)
        if request is None:
            raise StrictWorkflowBindingError()
        definition = self._definitions.get(request.definition_id, request.definition_revision)
        return request, definition

    @staticmethod
    def _check_pair(
        request: StrictWorkflowRequestReservation,
        definition: StrictDefinitionRevision,
    ) -> None:
        if (
            not definition.is_strict
            or definition.contract is None
            or definition.contract.contract_version != 1
            or request.run_format_version != 2
            or request.contract_version != 1
            or request.definition_id != definition.definition_id
            or request.definition_revision != definition.revision
            or request.source_sha256 != definition.source_sha256
            or request.contract_sha256 != definition.contract_sha256
            or request.binding_digest != definition.binding_digest
            or not isinstance(request.request_digest, str)
            or _SHA256.fullmatch(request.request_digest) is None
            or not isinstance(request.normalized_input_sha256, str)
            or _SHA256.fullmatch(request.normalized_input_sha256) is None
        ):
            raise StrictWorkflowBindingError()

    def _confirm_stable(
        self,
        run_id: str,
        request: StrictWorkflowRequestReservation,
        definition: StrictDefinitionRevision,
    ) -> None:
        current_request, current_definition = self._read_pair(run_id)
        if current_request != request or current_definition != definition:
            raise StrictWorkflowBindingError()

    def _confirm_definition_stable(self, definition: StrictDefinitionRevision) -> None:
        current = self._definitions.get(definition.definition_id, definition.revision)
        if current != definition:
            raise StrictWorkflowBindingError()

    @staticmethod
    def _metadata(
        request: StrictWorkflowRequestReservation,
        *,
        normalized_input_snapshot_verified: bool,
    ) -> StrictWorkflowBindingMetadata:
        return StrictWorkflowBindingMetadata(
            workflow_id=request.workflow_id,
            run_id=request.run_id,
            request_digest=request.request_digest,
            definition_id=request.definition_id,
            definition_revision=request.definition_revision,
            source_sha256=request.source_sha256,
            contract_sha256=request.contract_sha256,
            binding_digest=request.binding_digest,
            normalized_input_sha256=request.normalized_input_sha256,
            run_format_version=request.run_format_version,
            contract_version=request.contract_version,
            normalized_input_snapshot_verified=normalized_input_snapshot_verified,
        )

    @staticmethod
    def _validate_expectations(
        expected_workflow_id: str | None,
        expected_request_digest: str | None,
    ) -> None:
        if expected_workflow_id is not None:
            if not isinstance(expected_workflow_id, str):
                raise StrictWorkflowBindingError()
            try:
                if str(uuid.UUID(expected_workflow_id)) != expected_workflow_id:
                    raise StrictWorkflowBindingError()
            except (ValueError, AttributeError):
                raise StrictWorkflowBindingError() from None
        if expected_request_digest is not None and (
            not isinstance(expected_request_digest, str)
            or _SHA256.fullmatch(expected_request_digest) is None
        ):
            raise StrictWorkflowBindingError()


__all__ = [
    "StrictWorkflowBindingError",
    "StrictWorkflowBindingMetadata",
    "StrictWorkflowBindingVerifier",
    "StrictWorkflowInputNormalization",
]
