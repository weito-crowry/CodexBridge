"""Opt-in strict manifest v2 codec and isolated snapshot verification.

This module is deliberately not connected to the legacy run creation or reader
paths. It prepares and verifies immutable snapshots for the later I5-B reader
integration work.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn

import yaml

from ..paths import AllowedPathPolicy
from .contracts import canonical_json, normalize_input, parse_strict_json, sha256_json
from .strict_binding import (
    StrictWorkflowBindingVerifier,
    StrictWorkflowInputNormalization,
)
from .strict_definitions import StrictDefinitionRevision, StrictWorkflowDefinitionStore
from .strict_requests import (
    StrictWorkflowRequestReservation,
    StrictWorkflowRequestStore,
)
from .strict_variable_mutations import StrictWorkflowVariableMutationStore
from .strict_variables import (
    StrictWorkflowRunBindingInput,
    StrictWorkflowVariableStore,
)

_RUN_ID = re.compile(r"run-[0-9a-f]{32}\Z", re.ASCII)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_MAX_WORKFLOW_BYTES = 64 * 1024
_MAX_INPUT_BYTES = 96 * 1024
_MAX_MANIFEST_BYTES = 32 * 1024
_VARIABLE_STORE_FILENAME = "variables.sqlite"
_MAX_TASKS = 10_000
_ERROR = "strict workflow snapshot needs reconciliation"

_MANIFEST_FIELDS = frozenset(
    {
        "run_format_version",
        "workflow_id",
        "run_id",
        "workflow_source",
        "mode",
        "workspace_root",
        "max_live_codex_jobs",
        "max_tasks",
        "created_at",
        "request_digest",
        "definition_id",
        "definition_revision",
        "definition_sha256",
        "source_sha256",
        "contract_version",
        "contract_sha256",
        "binding_digest",
        "input_sha256",
        "normalized_input_sha256",
        "variable_store_filename",
        "variable_store_schema_version",
    }
)


class StrictWorkflowSnapshotError(RuntimeError):
    """Stable value-free failure for an invalid strict snapshot."""

    reason_code = "NEEDS_RECONCILIATION"

    def __init__(self) -> None:
        super().__init__(_ERROR)


@dataclass(frozen=True, slots=True, repr=False)
class StrictWorkflowManifestV2:
    """Fully specified strict v2 manifest fields; contents stay out of repr."""

    run_format_version: int = field(repr=False)
    workflow_id: str = field(repr=False)
    run_id: str = field(repr=False)
    workflow_source: str = field(repr=False)
    mode: str = field(repr=False)
    workspace_root: str = field(repr=False)
    max_live_codex_jobs: int = field(repr=False)
    max_tasks: int = field(repr=False)
    created_at: str = field(repr=False)
    request_digest: str = field(repr=False)
    definition_id: str = field(repr=False)
    definition_revision: int = field(repr=False)
    definition_sha256: str = field(repr=False)
    source_sha256: str = field(repr=False)
    contract_version: int = field(repr=False)
    contract_sha256: str = field(repr=False)
    binding_digest: str = field(repr=False)
    input_sha256: str = field(repr=False)
    normalized_input_sha256: str = field(repr=False)
    variable_store_filename: str = field(repr=False)
    variable_store_schema_version: int = field(repr=False)

    def __repr__(self) -> str:
        return "StrictWorkflowManifestV2(<verified>)"

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "run_format_version": self.run_format_version,
            "workflow_id": self.workflow_id,
            "run_id": self.run_id,
            "workflow_source": self.workflow_source,
            "mode": self.mode,
            "workspace_root": self.workspace_root,
            "max_live_codex_jobs": self.max_live_codex_jobs,
            "max_tasks": self.max_tasks,
            "created_at": self.created_at,
            "request_digest": self.request_digest,
            "definition_id": self.definition_id,
            "definition_revision": self.definition_revision,
            "definition_sha256": self.definition_sha256,
            "source_sha256": self.source_sha256,
            "contract_version": self.contract_version,
            "contract_sha256": self.contract_sha256,
            "binding_digest": self.binding_digest,
            "input_sha256": self.input_sha256,
            "normalized_input_sha256": self.normalized_input_sha256,
            "variable_store_filename": self.variable_store_filename,
            "variable_store_schema_version": self.variable_store_schema_version,
        }
        _validate_manifest_mapping(value)
        return value


@dataclass(frozen=True, slots=True, repr=False)
class StrictWorkflowSnapshotVerification:
    """Value-free verification result for a prepared or reserved snapshot."""

    manifest: StrictWorkflowManifestV2 = field(repr=False)
    reservation_verified: bool = field(repr=False)
    mutation_count: int = field(repr=False)
    _definition: StrictDefinitionRevision | None = field(default=None, repr=False, compare=False)
    _normalized_input: dict[str, Any] | None = field(default=None, repr=False, compare=False)

    def __repr__(self) -> str:
        return "StrictWorkflowSnapshotVerification(<verified>)"


def strict_request_digest_v2(
    *,
    workflow: object,
    workspace_root: str,
    max_live_codex_jobs: int,
    definition_id: str,
    definition_revision: int,
    source_sha256: str,
    contract_version: int,
    contract_sha256: str,
    binding_digest: str,
    normalized_input_sha256: str,
) -> str:
    """Return the one canonical, domain-separated strict v2 request digest."""
    try:
        if type(workflow) is not dict or not isinstance(workspace_root, str):
            _raise_snapshot_error()
        if not Path(workspace_root).is_absolute() or ".." in Path(workspace_root).parts:
            _raise_snapshot_error()
        _validate_uuid(definition_id)
        if type(definition_revision) is not int or definition_revision < 1:
            _raise_snapshot_error()
        if type(max_live_codex_jobs) is not int or max_live_codex_jobs not in {1, 2, 3}:
            _raise_snapshot_error()
        if type(contract_version) is not int or contract_version != 1:
            _raise_snapshot_error()
        for digest in (
            source_sha256,
            contract_sha256,
            binding_digest,
            normalized_input_sha256,
        ):
            _validate_sha256(digest)
        isolated_workflow = parse_strict_json(canonical_json(workflow))
        if type(isolated_workflow) is not dict:
            _raise_snapshot_error()
        return sha256_json(
            {
                "domain": "codexbridge-workflow-request-v2",
                "workflow": isolated_workflow,
                "workspace_root": workspace_root,
                "max_live_codex_jobs": max_live_codex_jobs,
                "definition_id": definition_id,
                "definition_revision": definition_revision,
                "source_sha256": source_sha256,
                "contract_version": contract_version,
                "contract_sha256": contract_sha256,
                "binding_digest": binding_digest,
                "normalized_input_sha256": normalized_input_sha256,
            }
        )
    except StrictWorkflowSnapshotError:
        raise
    except Exception:
        _raise_snapshot_error()


def serialize_strict_manifest_v2(manifest: StrictWorkflowManifestV2) -> bytes:
    """Serialize a validated manifest using the shared canonical JSON rules."""
    try:
        if not isinstance(manifest, StrictWorkflowManifestV2):
            _raise_snapshot_error()
        payload = canonical_json(manifest.to_dict()).encode("utf-8")
        if len(payload) > _MAX_MANIFEST_BYTES:
            _raise_snapshot_error()
        return payload
    except StrictWorkflowSnapshotError:
        raise
    except Exception:
        _raise_snapshot_error()


def parse_strict_manifest_v2(payload: bytes) -> StrictWorkflowManifestV2:
    """Parse canonical JSON and enforce the complete strict v2 allowlist."""
    try:
        if type(payload) is not bytes or len(payload) > _MAX_MANIFEST_BYTES:
            _raise_snapshot_error()
        parsed = parse_strict_json(payload)
        if type(parsed) is not dict:
            _raise_snapshot_error()
        _validate_manifest_mapping(parsed)
        if canonical_json(parsed).encode("utf-8") != payload:
            _raise_snapshot_error()
        return StrictWorkflowManifestV2(**parsed)
    except StrictWorkflowSnapshotError:
        raise
    except Exception:
        _raise_snapshot_error()


class StrictWorkflowSnapshotWriter:
    """Prepare an isolated v2 snapshot without reserving a request or run."""

    def __init__(self, state_root: Path, allowed_roots: tuple[str, ...]) -> None:
        self._state_root = Path(state_root)
        self._allowed_roots = tuple(allowed_roots)

    def prepare_snapshot(
        self,
        *,
        workflow_id: str,
        run_id: str,
        definition_id: str,
        definition_revision: int,
        workspace_root: str,
        normalized_input: StrictWorkflowInputNormalization,
        created_at: datetime,
    ) -> StrictWorkflowManifestV2:
        """Write workflow.yaml, input.json and manifest.json exclusively.

        The normalized input comes from I3's normalize_for_definition API.
        The initial variables.sqlite RunBinding must already exist; this method
        only verifies it and never reserves a request or creates a database.
        """
        try:
            root = _resolve_state_root(self._state_root)
            _validate_uuid(workflow_id)
            _validate_run_id(run_id)
            _validate_uuid(definition_id)
            if type(definition_revision) is not int or definition_revision < 1:
                _raise_snapshot_error()
            canonical_workspace = _validate_workspace(workspace_root, self._allowed_roots)
            _validate_no_overlap(root, Path(canonical_workspace))
            created_text = _format_utc_timestamp(created_at)

            definitions = StrictWorkflowDefinitionStore(root)
            definition = definitions.get(definition_id, definition_revision)
            if not _is_strict_definition(definition):
                _raise_snapshot_error()
            contract = definition.contract
            contract_sha256 = definition.contract_sha256
            binding_digest = definition.binding_digest
            if contract is None or contract_sha256 is None or binding_digest is None:
                _raise_snapshot_error()
            if (
                definition.definition_id != definition_id
                or definition.revision != definition_revision
            ):
                _raise_snapshot_error()

            verified_input = _copy_normalized_input(
                normalized_input,
                definition_id,
                definition_revision,
            )
            normalized_value = normalize_input(contract, verified_input.normalized_input)
            supplied_input_json = canonical_json(verified_input.normalized_input)
            normalized_input_json = canonical_json(normalized_value)
            normalized_input_sha256 = sha256_json(normalized_value)
            if (
                normalized_input_json != supplied_input_json
                or normalized_input_sha256 != verified_input.normalized_input_sha256
            ):
                _raise_snapshot_error()
            verified_input = StrictWorkflowInputNormalization(
                definition_id=definition_id,
                definition_revision=definition_revision,
                normalized_input=normalized_value,
                normalized_input_sha256=normalized_input_sha256,
            )
            source_bytes = _source_bytes(definition)
            workflow_value = _parse_workflow_source(source_bytes)
            if canonical_json(workflow_value) != canonical_json(definition.definition):
                _raise_snapshot_error()
            input_text = canonical_json(verified_input.normalized_input)
            input_bytes = input_text.encode("utf-8")
            if not input_bytes or len(input_bytes) > _MAX_INPUT_BYTES:
                _raise_snapshot_error()
            input_sha256 = hashlib.sha256(input_bytes).hexdigest()
            if input_sha256 != verified_input.normalized_input_sha256:
                _raise_snapshot_error()

            request_digest = strict_request_digest_v2(
                workflow=workflow_value,
                workspace_root=canonical_workspace,
                max_live_codex_jobs=definition.max_live_codex_jobs,
                definition_id=definition.definition_id,
                definition_revision=definition.revision,
                source_sha256=definition.source_sha256,
                contract_version=contract.contract_version,
                contract_sha256=contract_sha256,
                binding_digest=binding_digest,
                normalized_input_sha256=verified_input.normalized_input_sha256,
            )
            manifest = StrictWorkflowManifestV2(
                run_format_version=2,
                workflow_id=workflow_id,
                run_id=run_id,
                workflow_source="ui-api",
                mode="codex-job-live",
                workspace_root=canonical_workspace,
                max_live_codex_jobs=definition.max_live_codex_jobs,
                max_tasks=_MAX_TASKS,
                created_at=created_text,
                request_digest=request_digest,
                definition_id=definition.definition_id,
                definition_revision=definition.revision,
                definition_sha256=definition.source_sha256,
                source_sha256=definition.source_sha256,
                contract_version=contract.contract_version,
                contract_sha256=contract_sha256,
                binding_digest=binding_digest,
                input_sha256=input_sha256,
                normalized_input_sha256=verified_input.normalized_input_sha256,
                variable_store_filename=_VARIABLE_STORE_FILENAME,
                variable_store_schema_version=1,
            )
            manifest_bytes = serialize_strict_manifest_v2(manifest)
            if not source_bytes or len(source_bytes) > _MAX_WORKFLOW_BYTES:
                _raise_snapshot_error()
            source_sha256 = hashlib.sha256(source_bytes).hexdigest()
            if source_sha256 != definition.source_sha256:
                _raise_snapshot_error()

            run_directory = _resolve_run_directory(root, run_id, create=True)
            _ensure_variable_store_boundary(run_directory)
            _inspect_initial_variable_store(
                run_directory,
                manifest,
                created_at,
            )
            _ensure_snapshot_targets_absent(run_directory)
            _write_exclusive(run_directory / "workflow.yaml", source_bytes)
            _write_exclusive(run_directory / "input.json", input_bytes)
            _write_exclusive(run_directory / "manifest.json", manifest_bytes)
            return manifest
        except StrictWorkflowSnapshotError:
            raise
        except Exception:
            _raise_snapshot_error()


class StrictWorkflowSnapshotVerifier:
    """Read-only v2 snapshot verifier for pre-reservation and reserved runs."""

    def __init__(self, state_root: Path, allowed_roots: tuple[str, ...]) -> None:
        self._state_root = Path(state_root)
        self._allowed_roots = tuple(allowed_roots)

    def verify_snapshot_files(self, run_id: str) -> StrictWorkflowSnapshotVerification:
        """Verify snapshot, pinned revision and an initial empty variable DB."""
        return self.verify_pre_reservation(run_id)

    def verify_pre_reservation(self, run_id: str) -> StrictWorkflowSnapshotVerification:
        """Verify snapshot files and initial RunBinding without request lookup."""
        try:
            root = _resolve_state_root(self._state_root)
            checked = _verify_snapshot_files(root, self._allowed_roots, run_id)
            _ensure_variable_store_boundary(checked.run_directory)
            normalized = StrictWorkflowBindingVerifier(root).normalize_for_definition(
                checked.manifest.definition_id,
                checked.manifest.definition_revision,
                checked.input_value,
            )
            if (
                canonical_json(normalized.normalized_input) != canonical_json(checked.input_value)
                or normalized.normalized_input_sha256 != checked.manifest.normalized_input_sha256
            ):
                _raise_snapshot_error()
            info = _inspect_initial_variable_store(
                checked.run_directory,
                checked.manifest,
                _parse_manifest_timestamp(checked.manifest.created_at),
            )
            if info.frame_count != 1 or info.variable_state_count != 0:
                _raise_snapshot_error()
            return StrictWorkflowSnapshotVerification(
                manifest=checked.manifest,
                reservation_verified=False,
                mutation_count=0,
                _definition=checked.definition,
                _normalized_input=normalized.normalized_input,
            )
        except StrictWorkflowSnapshotError:
            raise
        except Exception:
            _raise_snapshot_error()

    def verify_reserved(self, run_id: str) -> StrictWorkflowSnapshotVerification:
        """Verify the v2 snapshot against I3 reservation and I4 ledger reads."""
        try:
            root = _resolve_state_root(self._state_root)
            _validate_run_id(run_id)
            reservation = StrictWorkflowRequestStore(root).read_binding(run_id)
            if reservation is None:
                _raise_snapshot_error()
            checked = _verify_snapshot_files(root, self._allowed_roots, run_id)
            manifest = checked.manifest
            _check_reservation_matches_manifest(reservation, manifest)
            binding = StrictWorkflowBindingVerifier(root).verify_normalized_input(
                run_id,
                checked.input_value,
                expected_workflow_id=manifest.workflow_id,
                expected_request_digest=manifest.request_digest,
            )
            _check_binding_metadata_matches_manifest(binding, manifest)

            definition = checked.definition
            if not _is_strict_definition(definition):
                _raise_snapshot_error()
            contract = definition.contract
            if contract is None:
                _raise_snapshot_error()
            _ensure_variable_store_boundary(checked.run_directory)
            run_binding = _run_binding_input(manifest)
            ledger = StrictWorkflowVariableMutationStore(
                checked.run_directory,
                run_binding,
                contract,
            ).inspect_ledger()
            if ledger.run_id != run_id:
                _raise_snapshot_error()
            if ledger.mutation_count == 0:
                if ledger.state_count != 0 or ledger.frame_count < 1:
                    _raise_snapshot_error()
                if ledger.frame_count == 1:
                    info = _inspect_initial_variable_store(
                        checked.run_directory,
                        manifest,
                        _parse_manifest_timestamp(manifest.created_at),
                    )
                    if info.frame_count != 1 or info.variable_state_count != 0:
                        _raise_snapshot_error()
            elif ledger.state_count < 1:
                _raise_snapshot_error()
            return StrictWorkflowSnapshotVerification(
                manifest=manifest,
                reservation_verified=True,
                mutation_count=ledger.mutation_count,
                _definition=checked.definition,
                _normalized_input=checked.input_value,
            )
        except StrictWorkflowSnapshotError:
            raise
        except Exception:
            _raise_snapshot_error()


@dataclass(frozen=True, slots=True, repr=False)
class _CheckedSnapshot:
    run_directory: Path = field(repr=False)
    manifest: StrictWorkflowManifestV2 = field(repr=False)
    input_value: dict[str, Any] = field(repr=False)
    definition: StrictDefinitionRevision = field(repr=False)

    def __repr__(self) -> str:
        return "_CheckedSnapshot(<verified>)"


def _verify_snapshot_files(
    state_root: Path,
    allowed_roots: tuple[str, ...],
    run_id: str,
) -> _CheckedSnapshot:
    _validate_run_id(run_id)
    run_directory = _resolve_run_directory(state_root, run_id, create=False)
    manifest_bytes = _read_regular_snapshot_file(
        run_directory, "manifest.json", _MAX_MANIFEST_BYTES
    )
    manifest = parse_strict_manifest_v2(manifest_bytes)
    if manifest.run_id != run_id:
        _raise_snapshot_error()
    workspace = _validate_workspace(manifest.workspace_root, allowed_roots)
    if workspace != manifest.workspace_root:
        _raise_snapshot_error()
    _validate_no_overlap(state_root, Path(workspace))

    workflow_bytes = _read_regular_snapshot_file(
        run_directory, "workflow.yaml", _MAX_WORKFLOW_BYTES
    )
    input_bytes = _read_regular_snapshot_file(run_directory, "input.json", _MAX_INPUT_BYTES)
    workflow_value = _parse_workflow_source(workflow_bytes)
    input_value = parse_strict_json(input_bytes)
    if type(input_value) is not dict or canonical_json(input_value).encode("utf-8") != input_bytes:
        _raise_snapshot_error()
    input_sha256 = hashlib.sha256(input_bytes).hexdigest()
    normalized_input_sha256 = sha256_json(input_value)
    source_sha256 = hashlib.sha256(workflow_bytes).hexdigest()
    if (
        input_sha256 != manifest.input_sha256
        or normalized_input_sha256 != manifest.normalized_input_sha256
        or input_sha256 != normalized_input_sha256
        or source_sha256 != manifest.source_sha256
        or source_sha256 != manifest.definition_sha256
    ):
        _raise_snapshot_error()

    definition = StrictWorkflowDefinitionStore(state_root).get(
        manifest.definition_id,
        manifest.definition_revision,
    )
    if not _is_strict_definition(definition):
        _raise_snapshot_error()
    contract = definition.contract
    contract_sha256 = definition.contract_sha256
    binding_digest = definition.binding_digest
    if contract is None or contract_sha256 is None or binding_digest is None:
        _raise_snapshot_error()
    if (
        definition.definition_id != manifest.definition_id
        or definition.revision != manifest.definition_revision
        or definition.source_sha256 != source_sha256
        or _source_bytes(definition) != workflow_bytes
        or contract.contract_version != manifest.contract_version
        or contract_sha256 != manifest.contract_sha256
        or binding_digest != manifest.binding_digest
        or definition.max_live_codex_jobs != manifest.max_live_codex_jobs
        or canonical_json(definition.definition) != canonical_json(workflow_value)
    ):
        _raise_snapshot_error()

    request_digest = strict_request_digest_v2(
        workflow=workflow_value,
        workspace_root=workspace,
        max_live_codex_jobs=definition.max_live_codex_jobs,
        definition_id=definition.definition_id,
        definition_revision=definition.revision,
        source_sha256=source_sha256,
        contract_version=contract.contract_version,
        contract_sha256=contract_sha256,
        binding_digest=binding_digest,
        normalized_input_sha256=manifest.normalized_input_sha256,
    )
    if request_digest != manifest.request_digest:
        _raise_snapshot_error()
    return _CheckedSnapshot(run_directory, manifest, input_value, definition)


def _copy_normalized_input(
    normalized_input: StrictWorkflowInputNormalization,
    definition_id: str,
    definition_revision: int,
) -> StrictWorkflowInputNormalization:
    if not isinstance(normalized_input, StrictWorkflowInputNormalization):
        _raise_snapshot_error()
    if (
        normalized_input.definition_id != definition_id
        or normalized_input.definition_revision != definition_revision
    ):
        _raise_snapshot_error()
    try:
        canonical = canonical_json(normalized_input.normalized_input)
        isolated = parse_strict_json(canonical)
    except Exception:
        _raise_snapshot_error()
    if (
        type(isolated) is not dict
        or sha256_json(isolated) != normalized_input.normalized_input_sha256
    ):
        _raise_snapshot_error()
    return StrictWorkflowInputNormalization(
        definition_id=definition_id,
        definition_revision=definition_revision,
        normalized_input=isolated,
        normalized_input_sha256=normalized_input.normalized_input_sha256,
    )


def _is_strict_definition(definition: StrictDefinitionRevision) -> bool:
    return (
        definition.is_strict
        and definition.contract is not None
        and definition.contract.contract_version == 1
        and _is_sha256(definition.source_sha256)
        and _is_sha256(definition.contract_sha256)
        and _is_sha256(definition.binding_digest)
        and type(definition.max_live_codex_jobs) is int
        and definition.max_live_codex_jobs in {1, 2, 3}
    )


def _source_bytes(definition: StrictDefinitionRevision) -> bytes:
    if type(definition.source_yaml) is not str:
        _raise_snapshot_error()
    try:
        payload = definition.source_yaml.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        _raise_snapshot_error()
    if not payload or len(payload) > _MAX_WORKFLOW_BYTES:
        _raise_snapshot_error()
    if hashlib.sha256(payload).hexdigest() != definition.source_sha256:
        _raise_snapshot_error()
    return payload


class _StrictWorkflowYamlLoader(yaml.SafeLoader):  # type: ignore[misc]
    pass


def _construct_unique_yaml_mapping(
    loader: _StrictWorkflowYamlLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[str, Any]:
    loader.flatten_mapping(node)
    result: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if type(key) is not str or key in result:
            raise ValueError
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_StrictWorkflowYamlLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_yaml_mapping,
)


def _parse_workflow_source(payload: bytes) -> dict[str, Any]:
    if type(payload) is not bytes or not payload or len(payload) > _MAX_WORKFLOW_BYTES:
        _raise_snapshot_error()
    try:
        text = payload.decode("utf-8", errors="strict")
        parsed = yaml.load(text, Loader=_StrictWorkflowYamlLoader)
        if type(parsed) is not dict:
            _raise_snapshot_error()
        encoded = canonical_json(parsed)
        isolated = parse_strict_json(encoded)
        if type(isolated) is not dict:
            _raise_snapshot_error()
        return isolated
    except StrictWorkflowSnapshotError:
        raise
    except Exception:
        _raise_snapshot_error()


def _validate_manifest_mapping(value: dict[str, Any]) -> None:
    if type(value) is not dict or set(value) != _MANIFEST_FIELDS:
        _raise_snapshot_error()
    if (
        type(value["run_format_version"]) is not int
        or value["run_format_version"] != 2
        or type(value["workflow_id"]) is not str
        or type(value["run_id"]) is not str
        or type(value["workflow_source"]) is not str
        or value["workflow_source"] != "ui-api"
        or type(value["mode"]) is not str
        or value["mode"] != "codex-job-live"
        or type(value["workspace_root"]) is not str
        or not Path(value["workspace_root"]).is_absolute()
        or ".." in Path(value["workspace_root"]).parts
        or type(value["max_live_codex_jobs"]) is not int
        or value["max_live_codex_jobs"] not in {1, 2, 3}
        or type(value["max_tasks"]) is not int
        or value["max_tasks"] != _MAX_TASKS
        or type(value["created_at"]) is not str
        or type(value["request_digest"]) is not str
        or type(value["definition_id"]) is not str
        or type(value["definition_revision"]) is not int
        or value["definition_revision"] < 1
        or type(value["definition_sha256"]) is not str
        or type(value["source_sha256"]) is not str
        or type(value["contract_version"]) is not int
        or value["contract_version"] != 1
        or type(value["contract_sha256"]) is not str
        or type(value["binding_digest"]) is not str
        or type(value["input_sha256"]) is not str
        or type(value["normalized_input_sha256"]) is not str
        or type(value["variable_store_filename"]) is not str
        or value["variable_store_filename"] != _VARIABLE_STORE_FILENAME
        or type(value["variable_store_schema_version"]) is not int
        or value["variable_store_schema_version"] != 1
    ):
        _raise_snapshot_error()
    _validate_uuid(value["workflow_id"])
    _validate_run_id(value["run_id"])
    _validate_uuid(value["definition_id"])
    _parse_manifest_timestamp(value["created_at"])
    for name in (
        "request_digest",
        "definition_sha256",
        "source_sha256",
        "contract_sha256",
        "binding_digest",
        "input_sha256",
        "normalized_input_sha256",
    ):
        _validate_sha256(value[name])
    if value["definition_sha256"] != value["source_sha256"]:
        _raise_snapshot_error()
    if value["input_sha256"] != value["normalized_input_sha256"]:
        _raise_snapshot_error()


def _validate_uuid(value: object) -> None:
    if type(value) is not str:
        _raise_snapshot_error()
    try:
        if str(uuid.UUID(value)) != value:
            _raise_snapshot_error()
    except (ValueError, AttributeError, TypeError):
        _raise_snapshot_error()


def _validate_run_id(value: object) -> None:
    if type(value) is not str or _RUN_ID.fullmatch(value) is None:
        _raise_snapshot_error()


def _validate_sha256(value: object) -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        _raise_snapshot_error()


def _is_sha256(value: object) -> bool:
    return type(value) is str and _SHA256.fullmatch(value) is not None


def _format_utc_timestamp(value: datetime) -> str:
    if type(value) is not datetime:
        _raise_snapshot_error()
    try:
        offset = value.utcoffset()
        if offset is None or offset != timedelta(0):
            _raise_snapshot_error()
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    except StrictWorkflowSnapshotError:
        raise
    except Exception:
        _raise_snapshot_error()


def _parse_manifest_timestamp(value: object) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        _raise_snapshot_error()
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
        if _format_utc_timestamp(parsed) != value:
            _raise_snapshot_error()
        return parsed
    except StrictWorkflowSnapshotError:
        raise
    except Exception:
        _raise_snapshot_error()


def _validate_workspace(value: str, allowed_roots: tuple[str, ...]) -> str:
    if type(value) is not str or not Path(value).is_absolute() or ".." in Path(value).parts:
        _raise_snapshot_error()
    try:
        canonical = AllowedPathPolicy(allowed_roots).validate_cwd(value)
        supplied = os.path.normcase(os.path.abspath(value))
        normalized = os.path.normcase(canonical)
        if supplied != normalized:
            _raise_snapshot_error()
        return canonical
    except StrictWorkflowSnapshotError:
        raise
    except Exception:
        _raise_snapshot_error()


def _resolve_state_root(path: Path) -> Path:
    try:
        if not path.is_absolute() or _is_symlink_or_junction(path):
            _raise_snapshot_error()
        root = path.resolve(strict=True)
        if not root.is_dir():
            _raise_snapshot_error()
        return root
    except StrictWorkflowSnapshotError:
        raise
    except Exception:
        _raise_snapshot_error()


def _resolve_run_directory(state_root: Path, run_id: str, *, create: bool) -> Path:
    _validate_run_id(run_id)
    candidate = state_root / run_id
    if _is_symlink_or_junction(candidate):
        _raise_snapshot_error()
    if create and not candidate.exists():
        try:
            candidate.mkdir()
        except FileExistsError:
            _raise_snapshot_error()
    try:
        if not candidate.exists() or not candidate.is_dir():
            _raise_snapshot_error()
        resolved = candidate.resolve(strict=True)
        if (
            os.path.normcase(os.fspath(resolved.parent)) != os.path.normcase(os.fspath(state_root))
            or resolved.name != run_id
        ):
            _raise_snapshot_error()
        return resolved
    except StrictWorkflowSnapshotError:
        raise
    except Exception:
        _raise_snapshot_error()


def _validate_no_overlap(first: Path, second: Path) -> None:
    try:
        left = os.path.normcase(os.fspath(first.resolve(strict=True)))
        right = os.path.normcase(os.fspath(second.resolve(strict=True)))
        common = os.path.commonpath((left, right))
        if common in {left, right}:
            _raise_snapshot_error()
    except StrictWorkflowSnapshotError:
        raise
    except Exception:
        _raise_snapshot_error()


def _ensure_variable_store_boundary(run_directory: Path) -> None:
    database = run_directory / _VARIABLE_STORE_FILENAME
    for path in (
        database,
        Path(f"{database}-wal"),
        Path(f"{database}-shm"),
        Path(f"{database}-journal"),
    ):
        if _is_symlink_or_junction(path):
            _raise_snapshot_error()
        if path.exists():
            try:
                if not path.is_file() or path.resolve(strict=True).parent != run_directory:
                    _raise_snapshot_error()
            except StrictWorkflowSnapshotError:
                raise
            except Exception:
                _raise_snapshot_error()


def _inspect_initial_variable_store(
    run_directory: Path,
    manifest: StrictWorkflowManifestV2,
    created_at: datetime,
) -> Any:
    binding = _run_binding_input(manifest)
    info = StrictWorkflowVariableStore(run_directory).inspect(
        manifest.run_id,
        manifest.workflow_id,
        expected_definition_id=manifest.definition_id,
        expected_definition_revision=manifest.definition_revision,
        expected_contract_sha256=manifest.contract_sha256,
        expected_binding_digest=manifest.binding_digest,
        expected_normalized_input_sha256=manifest.normalized_input_sha256,
        expected_created_at=created_at,
    )
    if (
        info.user_version != 1
        or info.run_id != binding.run_id
        or info.workflow_id != binding.workflow_id
        or info.definition_id != binding.definition_id
        or info.definition_revision != binding.definition_revision
        or info.contract_sha256 != binding.contract_sha256
        or info.binding_digest != binding.binding_digest
        or info.normalized_input_sha256 != binding.normalized_input_sha256
        or info.run_format_version != 2
        or info.store_schema_version != 1
        or info.status != "INITIALIZED"
        or info.frame_count != 1
        or info.variable_state_count != 0
        or info.variable_mutation_count != 0
    ):
        _raise_snapshot_error()
    return info


def _run_binding_input(manifest: StrictWorkflowManifestV2) -> StrictWorkflowRunBindingInput:
    return StrictWorkflowRunBindingInput(
        run_id=manifest.run_id,
        workflow_id=manifest.workflow_id,
        definition_id=manifest.definition_id,
        definition_revision=manifest.definition_revision,
        contract_sha256=manifest.contract_sha256,
        binding_digest=manifest.binding_digest,
        normalized_input_sha256=manifest.normalized_input_sha256,
        created_at=_parse_manifest_timestamp(manifest.created_at),
        run_format_version=manifest.run_format_version,
        store_schema_version=manifest.variable_store_schema_version,
        status="INITIALIZED",
    )


def _check_reservation_matches_manifest(
    reservation: StrictWorkflowRequestReservation,
    manifest: StrictWorkflowManifestV2,
) -> None:
    if (
        reservation.workflow_id != manifest.workflow_id
        or reservation.run_id != manifest.run_id
        or reservation.request_digest != manifest.request_digest
        or reservation.definition_id != manifest.definition_id
        or reservation.definition_revision != manifest.definition_revision
        or reservation.source_sha256 != manifest.source_sha256
        or reservation.run_format_version != manifest.run_format_version
        or reservation.contract_version != manifest.contract_version
        or reservation.contract_sha256 != manifest.contract_sha256
        or reservation.binding_digest != manifest.binding_digest
        or reservation.normalized_input_sha256 != manifest.normalized_input_sha256
        or _parse_request_timestamp(reservation.created_at)
        != _parse_manifest_timestamp(manifest.created_at)
    ):
        _raise_snapshot_error()


def _check_binding_metadata_matches_manifest(
    binding: Any, manifest: StrictWorkflowManifestV2
) -> None:
    if (
        binding.workflow_id != manifest.workflow_id
        or binding.run_id != manifest.run_id
        or binding.request_digest != manifest.request_digest
        or binding.definition_id != manifest.definition_id
        or binding.definition_revision != manifest.definition_revision
        or binding.source_sha256 != manifest.source_sha256
        or binding.contract_sha256 != manifest.contract_sha256
        or binding.binding_digest != manifest.binding_digest
        or binding.normalized_input_sha256 != manifest.normalized_input_sha256
        or binding.run_format_version != manifest.run_format_version
        or binding.contract_version != manifest.contract_version
        or not binding.normalized_input_snapshot_verified
    ):
        _raise_snapshot_error()


def _parse_request_timestamp(value: str) -> datetime:
    if type(value) is not str:
        _raise_snapshot_error()
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() != timedelta(0):
            _raise_snapshot_error()
        return parsed.astimezone(UTC)
    except StrictWorkflowSnapshotError:
        raise
    except Exception:
        _raise_snapshot_error()


def _read_regular_snapshot_file(run_directory: Path, name: str, limit: int) -> bytes:
    path = run_directory / name
    if path.name != name or _is_symlink_or_junction(path):
        _raise_snapshot_error()
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat()
        if (
            resolved.parent != run_directory
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > limit
        ):
            _raise_snapshot_error()
        payload = resolved.read_bytes()
        if len(payload) > limit:
            _raise_snapshot_error()
        return payload
    except StrictWorkflowSnapshotError:
        raise
    except Exception:
        _raise_snapshot_error()


def _ensure_snapshot_targets_absent(run_directory: Path) -> None:
    for name in ("workflow.yaml", "input.json", "manifest.json"):
        path = run_directory / name
        if _is_symlink_or_junction(path) or path.exists():
            _raise_snapshot_error()


def _write_exclusive(path: Path, payload: bytes) -> None:
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        _raise_snapshot_error()


def _is_symlink_or_junction(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction()) if callable(is_junction) else False
    except OSError:
        return True


def _raise_snapshot_error() -> NoReturn:
    raise StrictWorkflowSnapshotError() from None


__all__ = [
    "StrictWorkflowManifestV2",
    "StrictWorkflowSnapshotError",
    "StrictWorkflowSnapshotVerification",
    "StrictWorkflowSnapshotVerifier",
    "StrictWorkflowSnapshotWriter",
    "parse_strict_manifest_v2",
    "serialize_strict_manifest_v2",
    "strict_request_digest_v2",
]
