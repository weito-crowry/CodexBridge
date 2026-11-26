"""Atomic mutation ledger for opt-in run-local strict workflow variables.

This module attaches only to an explicitly initialized I4-A variables.sqlite.
It does not initialize or migrate databases and is not connected to DBOS or a
workflow runtime. State rows and mutation ledger rows commit together; a later
DBOS checkpoint remains a separate transaction boundary.
"""

from __future__ import annotations

import copy
import re
import sqlite3
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import strict_variable_frames as _frames
from .contracts import CompiledContract, canonical_json, parse_strict_json, sha256_json
from .strict_variable_logic import (
    MISSING,
    StrictVariableLogicError,
    StrictVariableLogicResourceLimit,
    VariableCandidate,
    VariableDeclaration,
    calculate_variable_candidate,
    resolve_variable_declaration,
    resolve_variable_declarations,
)
from .strict_variable_logic import (
    _snapshot_value as _snapshot_operation_payload,
)
from .strict_variables import (
    _BUSY_TIMEOUT_MS,
    StrictWorkflowRunBindingInput,
    StrictWorkflowVariableConflict,
    StrictWorkflowVariableError,
    StrictWorkflowVariableInvalidMetadata,
    StrictWorkflowVariableStorageUnavailable,
    StrictWorkflowVariableStore,
    _check_integrity,
    _check_schema,
    _configure_write_connection,
    _format_utc_timestamp,
    _read_connection,
    _rollback,
    _validate_binding,
)

_MAX_MUTATIONS = 10_000
_MAX_STATE_BYTES = 2 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_EXECUTION_KEY = re.compile(r"mutation-[0-9a-f]{64}\Z", re.ASCII)
_OPERATIONS = frozenset(
    {
        "initialize",
        "assign",
        "increment",
        "decrement",
        "append",
        "object_set",
        "object_delete",
        "variable_delete",
    }
)
_UNAVAILABLE = "strict workflow variable storage is unavailable"
_NEEDS_RECONCILIATION = "strict workflow variable mutation needs reconciliation"
_RESOURCE_LIMIT = "strict workflow variable resource limit exceeded"
_FAULT_INJECTOR = Callable[[str], None]


class StrictWorkflowVariableNeedsReconciliation(StrictWorkflowVariableConflict):
    """A deterministic execution key was reused with a different request."""

    code = "CONFLICT/NEEDS_RECONCILIATION"

    def __init__(self) -> None:
        StrictWorkflowVariableError.__init__(self, _NEEDS_RECONCILIATION)


class StrictWorkflowVariableResourceLimit(StrictWorkflowVariableError):
    """A proposed state or ledger update exceeds the I4-C data budget."""

    def __init__(self) -> None:
        super().__init__(_RESOURCE_LIMIT)


@dataclass(frozen=True, slots=True, repr=False)
class MutationContext:
    """Value-free identity supplied by the future strict runtime adapter."""

    node_path: str = field(repr=False)
    iteration_vector: tuple[int, ...] | list[int] = field(repr=False)
    frame_id: str = field(repr=False)
    mutation_ordinal: int = field(repr=False)
    subflow_call_context: tuple[str, ...] | list[str] | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        return "MutationContext(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class StrictWorkflowVariableState:
    """Verified current state; value access returns a detached JSON copy."""

    run_id: str = field(repr=False)
    frame_id: str = field(repr=False)
    declaration_id: str = field(repr=False)
    name: str = field(repr=False)
    exists: bool = field(repr=False)
    present: bool = field(repr=False)
    version: int = field(repr=False)
    value_digest: str | None = field(repr=False)
    _value_json: str | None = field(repr=False)

    @property
    def value(self) -> Any:
        if not self.exists or not self.present or self._value_json is None:
            return MISSING
        return copy.deepcopy(parse_strict_json(self._value_json))

    def __repr__(self) -> str:
        return "StrictWorkflowVariableState(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class StrictWorkflowVariableMutation:
    """Verified historical mutation result with redacted value storage."""

    execution_key: str = field(repr=False)
    payload_digest: str = field(repr=False)
    run_id: str = field(repr=False)
    frame_id: str = field(repr=False)
    declaration_id: str = field(repr=False)
    mutation_ordinal: int = field(repr=False)
    operation: str = field(repr=False)
    before_version: int = field(repr=False)
    after_version: int = field(repr=False)
    before_present: bool = field(repr=False)
    after_present: bool = field(repr=False)
    before_digest: str | None = field(repr=False)
    after_digest: str = field(repr=False)
    committed_at: str = field(repr=False)
    replayed: bool = field(repr=False)
    _after_value_json: str | None = field(repr=False)

    @property
    def after_value(self) -> Any:
        if not self.after_present or self._after_value_json is None:
            return MISSING
        return copy.deepcopy(parse_strict_json(self._after_value_json))

    def __repr__(self) -> str:
        return "StrictWorkflowVariableMutation(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class StrictWorkflowVariableLedgerInfo:
    run_id: str = field(repr=False)
    frame_count: int = field(repr=False)
    state_count: int = field(repr=False)
    mutation_count: int = field(repr=False)
    total_current_value_bytes: int = field(repr=False)
    mutations: tuple[StrictWorkflowVariableMutation, ...] = field(repr=False)

    def __repr__(self) -> str:
        return "StrictWorkflowVariableLedgerInfo(<verified>)"


@dataclass(frozen=True, slots=True)
class _VersionState:
    version: int
    present: bool
    value_digest: str | None
    value_json: str | None


@dataclass(frozen=True, slots=True)
class _History:
    states: dict[int, _VersionState]
    state_row: sqlite3.Row | None


class StrictWorkflowVariableMutationStore:
    """Explicit internal repository for one initialized run-local DB."""

    def __init__(
        self,
        run_directory: Path,
        expected_run_binding: StrictWorkflowRunBindingInput,
        compiled_contract: CompiledContract,
        *,
        _fault_injector: _FAULT_INJECTOR | None = None,
    ) -> None:
        self._variable_store = StrictWorkflowVariableStore(run_directory)
        self._expected_binding = expected_run_binding
        self._expected_created_at = _validate_binding(expected_run_binding)
        if (
            not isinstance(compiled_contract, CompiledContract)
            or compiled_contract.contract_sha256 != expected_run_binding.contract_sha256
        ):
            raise StrictWorkflowVariableInvalidMetadata()
        self._compiled_contract = compiled_contract
        declarations = resolve_variable_declarations(compiled_contract)
        self._declarations = {item.declaration_id: item for item in declarations}
        if len(self._declarations) != len(declarations):
            raise StrictWorkflowVariableInvalidMetadata()
        self._fault_injector = _fault_injector

    def apply(
        self,
        context: MutationContext,
        declaration_id: str,
        operation: str,
        *,
        expected_before_version: int,
        expected_before_digest: str | None,
        **operation_args: object,
    ) -> StrictWorkflowVariableMutation:
        """Atomically commit a state change and ledger row, or replay history."""
        context_identity, vector = self._validate_context(context)
        self._validate_cas_metadata(expected_before_version, expected_before_digest)
        if type(declaration_id) is not str or type(operation) is not str:
            raise StrictWorkflowVariableInvalidMetadata()
        if operation not in _OPERATIONS:
            raise StrictWorkflowVariableInvalidMetadata()
        if "operation" in operation_args:
            raise StrictWorkflowVariableInvalidMetadata()
        try:
            stable_payload = parse_strict_json(
                _snapshot_operation_payload({"operation": operation, **operation_args})
            )
            if type(stable_payload) is not dict or stable_payload.get("operation") != operation:
                raise StrictVariableLogicError()
            stable_operation_args = {
                name: value for name, value in stable_payload.items() if name != "operation"
            }
        except StrictVariableLogicError:
            raise
        except Exception:
            raise StrictVariableLogicError() from None
        execution_key = self._execution_key(context_identity)

        connection: sqlite3.Connection | None = None
        committed = False
        try:
            connection = self._open_write_connection()
            connection.execute("BEGIN IMMEDIATE")
            frames = self._validate_database(connection)
            frame = frames.get(context.frame_id)
            if frame is None:
                raise StrictWorkflowVariableConflict()
            stored_vector = _frames._decode_iteration_vector(frame["iteration_vector_json"])
            is_root_frame = (
                context.frame_id == "workflow-root"
                and frame["parent_frame_id"] is None
                and frame["scope_path"] == "workflow"
                and not stored_vector
            )
            if not is_root_frame and stored_vector != vector:
                raise StrictWorkflowVariableInvalidMetadata()
            declaration = resolve_variable_declaration(
                self._compiled_contract, declaration_id, frame["scope_path"]
            )
            history = self._load_history(connection, frame, declaration)
            mutation_count = self._mutation_count(connection)

            existing = connection.execute(
                "SELECT * FROM variable_mutations WHERE execution_key=?",
                (execution_key,),
            ).fetchone()
            if existing is not None:
                result = self._replay_existing(
                    existing,
                    history,
                    frame,
                    declaration,
                    context,
                    context_identity,
                    operation,
                    expected_before_version,
                    expected_before_digest,
                    stable_operation_args,
                    execution_key,
                )
                connection.execute("COMMIT")
                committed = True
                return result

            if mutation_count >= _MAX_MUTATIONS:
                raise StrictWorkflowVariableResourceLimit()
            if frame["status"] != "ACTIVE":
                raise StrictWorkflowVariableConflict()
            latest = history.states[max(history.states)]
            if (
                latest.version != expected_before_version
                or latest.value_digest != expected_before_digest
            ):
                raise StrictWorkflowVariableConflict()
            candidate = calculate_variable_candidate(
                self._compiled_contract,
                declaration_id,
                frame["scope_path"],
                operation,
                previous_exists=history.state_row is not None,
                previous_present=latest.present,
                previous_value=(
                    parse_strict_json(latest.value_json)
                    if latest.present and latest.value_json is not None
                    else MISSING
                ),
                **stable_operation_args,
            )
            if not candidate.state_exists:
                # I4-B's initialize-without-initial is a genuine no-op. It must
                # never create a tombstone or a mutation ledger entry here.
                raise StrictWorkflowVariableConflict()
            payload_digest = self._payload_digest(
                context_identity,
                execution_key,
                declaration_id,
                operation,
                expected_before_version,
                expected_before_digest,
                stable_operation_args,
            )
            current_value_bytes = self._current_value_bytes(connection)
            current_value_bytes = self._replace_value_size(
                current_value_bytes,
                latest,
                candidate,
            )
            if current_value_bytes > _MAX_STATE_BYTES:
                raise StrictWorkflowVariableResourceLimit()

            now = _format_utc_timestamp(datetime.now(UTC))
            after_version = latest.version + 1
            if history.state_row is None:
                connection.execute(
                    """
                    INSERT INTO variable_states (
                        run_id, frame_id, declaration_id, name, present,
                        value_json, version, value_digest, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self._expected_binding.run_id,
                        context.frame_id,
                        declaration_id,
                        declaration.name,
                        int(candidate.present),
                        candidate.canonical,
                        after_version,
                        candidate.value_digest,
                        now,
                    ),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE variable_states
                    SET present=?, value_json=?, version=?, value_digest=?, updated_at=?
                    WHERE run_id=? AND frame_id=? AND declaration_id=? AND version=?
                    """,
                    (
                        int(candidate.present),
                        candidate.canonical,
                        after_version,
                        candidate.value_digest,
                        now,
                        self._expected_binding.run_id,
                        context.frame_id,
                        declaration_id,
                        latest.version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise StrictWorkflowVariableConflict()
            self._fault_checkpoint("after_state_write")
            connection.execute(
                """
                INSERT INTO variable_mutations (
                    execution_key, run_id, frame_id, declaration_id,
                    mutation_ordinal, operation, payload_digest, before_version,
                    after_version, before_present, after_present, before_digest,
                    after_digest, after_value_json, committed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    execution_key,
                    self._expected_binding.run_id,
                    context.frame_id,
                    declaration_id,
                    context.mutation_ordinal,
                    operation,
                    payload_digest,
                    expected_before_version,
                    after_version,
                    int(latest.present),
                    int(candidate.present),
                    expected_before_digest,
                    candidate.value_digest,
                    candidate.canonical,
                    now,
                ),
            )
            self._fault_checkpoint("after_ledger_write")
            _check_schema(connection)
            _check_integrity(connection)
            verified = self._load_history(connection, frame, declaration)
            if not self._candidate_matches_history(candidate, verified.states[after_version]):
                raise StrictWorkflowVariableStorageUnavailable()
            result = self._mutation_from_row(
                connection.execute(
                    "SELECT * FROM variable_mutations WHERE execution_key=?",
                    (execution_key,),
                ).fetchone(),
                replayed=False,
            )
            self._fault_checkpoint("before_commit")
            connection.execute("COMMIT")
            committed = True
            return result
        except (StrictWorkflowVariableError, StrictVariableLogicError):
            _rollback(connection)
            raise
        except Exception:
            _rollback(connection)
            raise StrictWorkflowVariableStorageUnavailable() from None
        finally:
            self._close_connection(connection, committed)

    def read_current(self, frame_id: str, declaration_id: str) -> StrictWorkflowVariableState:
        """Read and validate one variable's complete chain and latest state."""
        if type(frame_id) is not str or not frame_id or type(declaration_id) is not str:
            raise StrictWorkflowVariableInvalidMetadata()
        connection: sqlite3.Connection | None = None
        try:
            connection = self._open_read_connection()
            frames = self._validate_database(connection)
            frame = frames.get(frame_id)
            if frame is None:
                raise StrictWorkflowVariableConflict()
            if frame["status"] != "ACTIVE":
                raise StrictWorkflowVariableConflict()
            declaration = resolve_variable_declaration(
                self._compiled_contract, declaration_id, frame["scope_path"]
            )
            history = self._load_history(connection, frame, declaration)
            current_bytes = self._current_value_bytes(connection)
            if current_bytes > _MAX_STATE_BYTES:
                raise StrictWorkflowVariableStorageUnavailable()
            latest = history.states[max(history.states)]
            state_row = history.state_row
            return StrictWorkflowVariableState(
                run_id=self._expected_binding.run_id,
                frame_id=frame_id,
                declaration_id=declaration_id,
                name=declaration.name,
                exists=state_row is not None,
                present=latest.present,
                version=latest.version,
                value_digest=latest.value_digest,
                _value_json=latest.value_json,
            )
        except (StrictWorkflowVariableError, StrictVariableLogicError):
            raise
        except Exception:
            raise StrictWorkflowVariableStorageUnavailable() from None
        finally:
            self._close_read_connection(connection)

    def inspect_ledger(self) -> StrictWorkflowVariableLedgerInfo:
        """Perform an explicit full audit of all frames, states and mutations."""
        connection: sqlite3.Connection | None = None
        try:
            connection = self._open_read_connection()
            frames = self._validate_database(connection)
            mutations, state_count, current_bytes = self._audit_all(connection, frames)
            records = tuple(
                self._mutation_from_row(row, replayed=False)
                for row in sorted(
                    mutations,
                    key=lambda item: (
                        item["frame_id"],
                        item["declaration_id"],
                        item["after_version"],
                    ),
                )
            )
            return StrictWorkflowVariableLedgerInfo(
                run_id=self._expected_binding.run_id,
                frame_count=len(frames),
                state_count=state_count,
                mutation_count=len(mutations),
                total_current_value_bytes=current_bytes,
                mutations=records,
            )
        except (StrictWorkflowVariableError, StrictVariableLogicError):
            raise
        except Exception:
            raise StrictWorkflowVariableStorageUnavailable() from None
        finally:
            self._close_read_connection(connection)

    def read_root_projection(self) -> dict[str, Any]:
        """Read all present workflow variables from one audited root snapshot."""
        connection: sqlite3.Connection | None = None
        try:
            connection = self._open_read_connection()
            connection.execute("BEGIN")
            frames = self._validate_database(connection)
            root = frames.get("workflow-root")
            if (
                root is None
                or root["parent_frame_id"] is not None
                or root["scope_path"] != "workflow"
                or root["status"] != "ACTIVE"
                or _frames._decode_iteration_vector(root["iteration_vector_json"])
            ):
                raise StrictWorkflowVariableConflict()
            _mutations, _state_count, current_bytes = self._audit_all(connection, frames)
            if current_bytes > _MAX_STATE_BYTES:
                raise StrictWorkflowVariableStorageUnavailable()
            projection: dict[str, Any] = {}
            for declaration in self._declarations.values():
                if declaration.owner_scope != "workflow":
                    continue
                history = self._load_history(connection, root, declaration)
                latest = history.states[max(history.states)]
                if history.state_row is None or not latest.present or latest.value_json is None:
                    continue
                projection[declaration.declaration_id] = parse_strict_json(latest.value_json)
            return projection
        except (StrictWorkflowVariableError, StrictVariableLogicError):
            raise
        except Exception:
            raise StrictWorkflowVariableStorageUnavailable() from None
        finally:
            self._close_read_connection(connection)

    def get_mutation(self, execution_key: str) -> StrictWorkflowVariableMutation | None:
        """Read one historical result after validating its variable chain."""
        if type(execution_key) is not str or _EXECUTION_KEY.fullmatch(execution_key) is None:
            raise StrictWorkflowVariableInvalidMetadata()
        connection: sqlite3.Connection | None = None
        try:
            connection = self._open_read_connection()
            frames = self._validate_database(connection)
            row = connection.execute(
                "SELECT * FROM variable_mutations WHERE execution_key=?",
                (execution_key,),
            ).fetchone()
            if row is None:
                return None
            frame = frames.get(row["frame_id"])
            declaration = self._declarations.get(row["declaration_id"])
            if frame is None or declaration is None:
                raise StrictWorkflowVariableStorageUnavailable()
            if declaration.owner_scope != frame["scope_path"]:
                raise StrictWorkflowVariableStorageUnavailable()
            history = self._load_history(connection, frame, declaration)
            self._validate_mutation_row(row, frame, declaration.declaration_id)
            if row["after_version"] not in history.states:
                raise StrictWorkflowVariableStorageUnavailable()
            return self._mutation_from_row(row, replayed=False)
        except (StrictWorkflowVariableError, StrictVariableLogicError):
            raise
        except Exception:
            raise StrictWorkflowVariableStorageUnavailable() from None
        finally:
            self._close_read_connection(connection)

    def verify_existing_prepared(
        self,
        context: MutationContext,
        declaration_id: str,
        operation: str,
        *,
        expected_before_version: int,
        expected_before_digest: str | None,
        **operation_args: object,
    ) -> StrictWorkflowVariableMutation:
        """Verify an already committed prepared mutation without writing.

        This is used after a DBOS step checkpoint is returned. Unlike ``apply``,
        a missing execution row is uncertainty and never causes a new mutation.
        """
        context_identity, vector = self._validate_context(context)
        self._validate_cas_metadata(expected_before_version, expected_before_digest)
        if (
            type(declaration_id) is not str
            or type(operation) is not str
            or operation not in _OPERATIONS
            or "operation" in operation_args
        ):
            raise StrictWorkflowVariableInvalidMetadata()
        try:
            stable_payload = parse_strict_json(
                _snapshot_operation_payload({"operation": operation, **operation_args})
            )
            if type(stable_payload) is not dict or stable_payload.get("operation") != operation:
                raise StrictVariableLogicError()
            stable_operation_args = {
                name: value for name, value in stable_payload.items() if name != "operation"
            }
        except StrictWorkflowVariableError:
            raise
        except Exception:
            raise StrictVariableLogicError() from None

        execution_key = self._execution_key(context_identity)
        connection: sqlite3.Connection | None = None
        try:
            connection = self._open_read_connection()
            connection.execute("BEGIN")
            frames = self._validate_database(connection)
            _self_audit, _state_count, _current_bytes = self._audit_all(connection, frames)
            frame = frames.get(context.frame_id)
            if frame is None or frame["status"] != "ACTIVE":
                raise StrictWorkflowVariableNeedsReconciliation()
            stored_vector = _frames._decode_iteration_vector(frame["iteration_vector_json"])
            is_root_frame = (
                context.frame_id == "workflow-root"
                and frame["parent_frame_id"] is None
                and frame["scope_path"] == "workflow"
                and not stored_vector
            )
            if not is_root_frame and stored_vector != vector:
                raise StrictWorkflowVariableNeedsReconciliation()
            declaration = resolve_variable_declaration(
                self._compiled_contract, declaration_id, frame["scope_path"]
            )
            history = self._load_history(connection, frame, declaration)
            existing = connection.execute(
                "SELECT * FROM variable_mutations WHERE execution_key=?", (execution_key,)
            ).fetchone()
            if existing is None:
                raise StrictWorkflowVariableNeedsReconciliation()
            result = self._replay_existing(
                existing,
                history,
                frame,
                declaration,
                context,
                context_identity,
                operation,
                expected_before_version,
                expected_before_digest,
                stable_operation_args,
                execution_key,
            )
            connection.execute("COMMIT")
            return result
        except (StrictWorkflowVariableError, StrictVariableLogicError):
            _rollback(connection)
            raise
        except Exception:
            _rollback(connection)
            raise StrictWorkflowVariableStorageUnavailable() from None
        finally:
            self._close_read_connection(connection)

    def enter_frame(
        self,
        parent_frame_id: str,
        scope_path: str,
        iteration_vector: list[int],
        created_at: datetime,
    ) -> _frames.StrictWorkflowScopeFrameInfo:
        """Create or replay a child frame after a complete ledger audit."""
        if type(parent_frame_id) is not str or not parent_frame_id:
            raise StrictWorkflowVariableInvalidMetadata()
        created = _frames._canonical_timestamp(created_at)
        vector_json = _frames._canonical_iteration_vector(iteration_vector)
        _frames._validate_scope_path_value(scope_path)
        frame_id = _frames._child_frame_id(
            self._expected_binding.run_id, parent_frame_id, scope_path, vector_json
        )
        connection: sqlite3.Connection | None = None
        committed = False
        try:
            connection = self._open_write_connection()
            connection.execute("BEGIN IMMEDIATE")
            frames = self._validate_database(connection)
            self._audit_all(connection, frames)
            parent = frames.get(parent_frame_id)
            if parent is None or parent["status"] != "ACTIVE":
                raise StrictWorkflowVariableConflict()
            parent_vector = _frames._decode_iteration_vector(parent["iteration_vector_json"])
            child_vector = _frames._decode_iteration_vector(vector_json)
            if (
                len(child_vector) < len(parent_vector)
                or child_vector[: len(parent_vector)] != parent_vector
            ):
                raise StrictWorkflowVariableInvalidMetadata()
            if not _frames._is_static_child_scope(parent["scope_path"], scope_path):
                raise StrictWorkflowVariableInvalidMetadata()
            existing = frames.get(frame_id)
            if existing is not None:
                if (
                    existing["parent_frame_id"] != parent_frame_id
                    or existing["scope_path"] != scope_path
                    or existing["iteration_vector_json"] != vector_json
                    or existing["status"] != "ACTIVE"
                ):
                    raise StrictWorkflowVariableConflict()
                result = _frames._frame_info(existing)
                connection.execute("COMMIT")
                committed = True
                return result
            if len(frames) >= _frames._MAX_FRAMES:
                raise StrictWorkflowVariableConflict()
            depths = _frames._validate_frames(
                list(frames.values()),
                self._expected_binding.run_id,
                self._expected_created_at,
            )
            if depths[parent_frame_id] + 1 > _frames._MAX_DEPTH:
                raise StrictWorkflowVariableConflict()
            connection.execute(
                """
                INSERT INTO scope_frames (
                    run_id, frame_id, parent_frame_id, scope_path,
                    iteration_vector_json, status, created_at, closed_at
                ) VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?, NULL)
                """,
                (
                    self._expected_binding.run_id,
                    frame_id,
                    parent_frame_id,
                    scope_path,
                    vector_json,
                    created,
                ),
            )
            self._fault_checkpoint("after_frame_insert")
            row = connection.execute(
                "SELECT * FROM scope_frames WHERE run_id=? AND frame_id=?",
                (self._expected_binding.run_id, frame_id),
            ).fetchone()
            if row is None:
                raise StrictWorkflowVariableStorageUnavailable()
            inserted_frames = self._validate_database(connection)
            self._audit_all(connection, inserted_frames)
            result = _frames._frame_info(row)
            self._fault_checkpoint("before_commit")
            connection.execute("COMMIT")
            committed = True
            return result
        except StrictWorkflowVariableError:
            _rollback(connection)
            raise
        except Exception:
            _rollback(connection)
            raise StrictWorkflowVariableStorageUnavailable() from None
        finally:
            self._close_connection(connection, committed)

    def close_frame(
        self, frame_id: str, closed_at: datetime
    ) -> _frames.StrictWorkflowScopeFrameInfo:
        """Close a non-root frame after descendant and full-ledger validation."""
        if type(frame_id) is not str or not frame_id:
            raise StrictWorkflowVariableInvalidMetadata()
        closed = _frames._canonical_timestamp(closed_at)
        connection: sqlite3.Connection | None = None
        committed = False
        try:
            connection = self._open_write_connection()
            connection.execute("BEGIN IMMEDIATE")
            frames = self._validate_database(connection)
            self._audit_all(connection, frames)
            frame = frames.get(frame_id)
            if frame is None or frame_id == _frames._ROOT_FRAME_ID:
                raise StrictWorkflowVariableConflict()
            if frame["parent_frame_id"] is None:
                raise StrictWorkflowVariableConflict()
            if frame["status"] == "CLOSED":
                if frame["closed_at"] != closed:
                    raise StrictWorkflowVariableConflict()
                result = _frames._frame_info(frame)
                connection.execute("COMMIT")
                committed = True
                return result
            active_child = connection.execute(
                """
                SELECT 1 FROM scope_frames
                WHERE run_id=? AND parent_frame_id=? AND status='ACTIVE' LIMIT 1
                """,
                (self._expected_binding.run_id, frame_id),
            ).fetchone()
            if active_child is not None:
                raise StrictWorkflowVariableConflict()
            connection.execute(
                """
                UPDATE scope_frames SET status='CLOSED', closed_at=?
                WHERE run_id=? AND frame_id=? AND status='ACTIVE'
                """,
                (closed, self._expected_binding.run_id, frame_id),
            )
            self._fault_checkpoint("after_frame_close")
            updated = connection.execute(
                "SELECT * FROM scope_frames WHERE run_id=? AND frame_id=?",
                (self._expected_binding.run_id, frame_id),
            ).fetchone()
            if updated is None:
                raise StrictWorkflowVariableStorageUnavailable()
            self._validate_database(connection)
            self._audit_all(connection, self._load_frames(connection))
            self._fault_checkpoint("before_commit")
            result = _frames._frame_info(updated)
            connection.execute("COMMIT")
            committed = True
            return result
        except StrictWorkflowVariableError:
            _rollback(connection)
            raise
        except Exception:
            _rollback(connection)
            raise StrictWorkflowVariableStorageUnavailable() from None
        finally:
            self._close_connection(connection, committed)

    def _validate_context(
        self, context: MutationContext
    ) -> tuple[dict[str, object], tuple[int, ...]]:
        if not isinstance(context, MutationContext):
            raise StrictWorkflowVariableInvalidMetadata()
        if (
            type(context.node_path) is not str
            or not context.node_path
            or type(context.frame_id) is not str
            or not context.frame_id
            or type(context.mutation_ordinal) is not int
            or context.mutation_ordinal < 0
        ):
            raise StrictWorkflowVariableInvalidMetadata()
        try:
            context.node_path.encode("utf-8", errors="strict")
            vector_source = context.iteration_vector
            if type(vector_source) not in (list, tuple) or any(
                type(item) is not int or item < 0 for item in vector_source
            ):
                raise StrictWorkflowVariableInvalidMetadata()
            vector = tuple(vector_source)
            subflow = context.subflow_call_context
            if subflow is not None and (
                type(subflow) not in (list, tuple)
                or any(type(item) is not str or not item for item in subflow)
            ):
                raise StrictWorkflowVariableInvalidMetadata()
            if subflow is not None:
                for item in subflow:
                    item.encode("utf-8", errors="strict")
            # Prove the complete identity can be canonicalized before opening DB.
            identity: dict[str, object] = {
                "node_path": context.node_path,
                "iteration_vector": list(vector),
                "frame_id": context.frame_id,
                "mutation_ordinal": context.mutation_ordinal,
                "subflow_call_context": None if subflow is None else list(subflow),
            }
            canonical_json(identity)
            return identity, vector
        except StrictWorkflowVariableError:
            raise
        except Exception:
            raise StrictWorkflowVariableInvalidMetadata() from None

    @staticmethod
    def _validate_cas_metadata(version: int, digest: str | None) -> None:
        if type(version) is not int or version < 0:
            raise StrictWorkflowVariableInvalidMetadata()
        if version == 0:
            if digest is not None:
                raise StrictWorkflowVariableInvalidMetadata()
        elif type(digest) is not str or _SHA256.fullmatch(digest) is None:
            raise StrictWorkflowVariableInvalidMetadata()

    def _execution_key(self, context_identity: dict[str, object]) -> str:
        binding = self._expected_binding
        identity = {
            "domain": "codexbridge.workflow.variable.execution-key.v1",
            "run_binding": {
                "run_id": binding.run_id,
                "workflow_id": binding.workflow_id,
                "definition_id": binding.definition_id,
                "definition_revision": binding.definition_revision,
                "contract_sha256": binding.contract_sha256,
                "binding_digest": binding.binding_digest,
                "normalized_input_sha256": binding.normalized_input_sha256,
                "run_format_version": binding.run_format_version,
                "store_schema_version": binding.store_schema_version,
                "status": binding.status,
                "created_at": self._expected_created_at,
            },
            "mutation_context": context_identity,
        }
        return "mutation-" + sha256_json(identity)

    def execution_key_for(self, context: MutationContext) -> str:
        """Derive the established key for one validated execution context."""
        context_identity, _vector = self._validate_context(context)
        return self._execution_key(context_identity)

    def _payload_digest(
        self,
        context_identity: dict[str, object],
        execution_key: str,
        declaration_id: str,
        operation: str,
        before_version: int,
        before_digest: str | None,
        operation_args: dict[str, object],
    ) -> str:
        binding = self._expected_binding
        payload: dict[str, object] = {
            "domain": "codexbridge.workflow.variable.payload.v1",
            "execution_key": execution_key,
            "mutation_context": context_identity,
            "run_id": binding.run_id,
            "workflow_id": binding.workflow_id,
            "definition_id": binding.definition_id,
            "definition_revision": binding.definition_revision,
            "contract_sha256": binding.contract_sha256,
            "binding_digest": binding.binding_digest,
            "normalized_input_sha256": binding.normalized_input_sha256,
            "declaration_id": declaration_id,
            "operation": operation,
            "expected_before_version": before_version,
            "expected_before_digest": before_digest,
            "operation_args": operation_args,
        }
        return sha256_json(payload)

    def _open_write_connection(self) -> sqlite3.Connection:
        database = self._variable_store._database_path(required=True)
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                database.resolve(strict=True).as_uri() + "?mode=rw",
                uri=True,
                timeout=_BUSY_TIMEOUT_MS / 1000,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            _configure_write_connection(connection)
            return connection
        except StrictWorkflowVariableError:
            if connection is not None:
                connection.close()
            raise
        except Exception:
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass
            raise StrictWorkflowVariableStorageUnavailable() from None

    def _open_read_connection(self) -> sqlite3.Connection:
        database = self._variable_store._database_path(required=True)
        return _read_connection(database)

    def _validate_database(self, connection: sqlite3.Connection) -> dict[str, sqlite3.Row]:
        _check_schema(connection)
        _check_integrity(connection)
        _frames._verify_binding(connection, self._expected_binding, self._expected_created_at)
        frames = self._load_frames(connection)
        self._mutation_count(connection)
        return frames

    def _load_frames(self, connection: sqlite3.Connection) -> dict[str, sqlite3.Row]:
        rows = connection.execute("SELECT * FROM scope_frames").fetchall()
        _frames._validate_frames(
            rows,
            self._expected_binding.run_id,
            self._expected_created_at,
        )
        return {row["frame_id"]: row for row in rows}

    def _mutation_count(self, connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT COUNT(*) FROM variable_mutations WHERE run_id=?",
            (self._expected_binding.run_id,),
        ).fetchone()
        if row is None or type(row[0]) is not int or row[0] > _MAX_MUTATIONS:
            raise StrictWorkflowVariableStorageUnavailable()
        return row[0]

    def _load_history(
        self,
        connection: sqlite3.Connection,
        frame: sqlite3.Row,
        declaration: VariableDeclaration,
    ) -> _History:
        declaration_id = declaration.declaration_id
        declaration_info = self._declarations.get(declaration_id)
        if declaration_info is None or declaration_info.owner_scope != frame["scope_path"]:
            raise StrictWorkflowVariableStorageUnavailable()
        run_id = self._expected_binding.run_id
        frame_id = frame["frame_id"]
        mutation_rows = connection.execute(
            """
            SELECT * FROM variable_mutations
            WHERE run_id=? AND frame_id=? AND declaration_id=?
            ORDER BY after_version
            """,
            (run_id, frame_id, declaration_id),
        ).fetchall()
        state_rows = connection.execute(
            """
            SELECT * FROM variable_states
            WHERE run_id=? AND frame_id=? AND declaration_id=?
            """,
            (run_id, frame_id, declaration_id),
        ).fetchall()
        if len(state_rows) > 1:
            raise StrictWorkflowVariableStorageUnavailable()
        state_row = state_rows[0] if state_rows else None
        states: dict[int, _VersionState] = {0: _VersionState(0, False, None, None)}
        previous = states[0]
        for row in mutation_rows:
            self._validate_mutation_row(row, frame, declaration_id)
            if (
                row["before_version"] != previous.version
                or row["after_version"] != previous.version + 1
                or bool(row["before_present"]) != previous.present
                or row["before_digest"] != previous.value_digest
            ):
                raise StrictWorkflowVariableStorageUnavailable()
            after_present = bool(row["after_present"])
            after_value_json = row["after_value_json"]
            self._validate_stored_value(
                declaration_info,
                frame["scope_path"],
                after_present,
                after_value_json,
                row["after_digest"],
            )
            current = _VersionState(
                row["after_version"],
                after_present,
                row["after_digest"],
                after_value_json,
            )
            if current.version in states:
                raise StrictWorkflowVariableStorageUnavailable()
            states[current.version] = current
            previous = current

        if state_row is None:
            if mutation_rows:
                raise StrictWorkflowVariableStorageUnavailable()
            return _History(states=states, state_row=None)
        self._validate_state_row(state_row, declaration_info, frame)
        if not mutation_rows:
            raise StrictWorkflowVariableStorageUnavailable()
        last = states[max(states)]
        if (
            state_row["version"] != last.version
            or bool(state_row["present"]) != last.present
            or state_row["value_digest"] != last.value_digest
            or state_row["value_json"] != last.value_json
        ):
            raise StrictWorkflowVariableStorageUnavailable()
        return _History(states=states, state_row=state_row)

    def _validate_mutation_row(
        self, row: sqlite3.Row, frame: sqlite3.Row, declaration_id: str
    ) -> None:
        if (
            type(row["execution_key"]) is not str
            or _EXECUTION_KEY.fullmatch(row["execution_key"]) is None
            or row["run_id"] != self._expected_binding.run_id
            or row["frame_id"] != frame["frame_id"]
            or row["declaration_id"] != declaration_id
            or type(row["mutation_ordinal"]) is not int
            or row["mutation_ordinal"] < 0
            or type(row["operation"]) is not str
            or row["operation"] not in _OPERATIONS
            or type(row["payload_digest"]) is not str
            or _SHA256.fullmatch(row["payload_digest"]) is None
            or type(row["before_version"]) is not int
            or type(row["after_version"]) is not int
            or row["before_version"] < 0
            or row["after_version"] <= 0
            or type(row["before_present"]) is not int
            or row["before_present"] not in (0, 1)
            or type(row["after_present"]) is not int
            or row["after_present"] not in (0, 1)
            or type(row["after_digest"]) is not str
            or _SHA256.fullmatch(row["after_digest"]) is None
            or (row["before_digest"] is not None and type(row["before_digest"]) is not str)
        ):
            raise StrictWorkflowVariableStorageUnavailable()
        if row["before_version"] == 0:
            if row["before_present"] != 0 or row["before_digest"] is not None:
                raise StrictWorkflowVariableStorageUnavailable()
        elif row["before_digest"] is None or _SHA256.fullmatch(row["before_digest"]) is None:
            raise StrictWorkflowVariableStorageUnavailable()
        if type(row["committed_at"]) is not str:
            raise StrictWorkflowVariableStorageUnavailable()
        try:
            parsed = datetime.fromisoformat(row["committed_at"].removesuffix("Z") + "+00:00")
            canonical = _format_utc_timestamp(parsed)
        except Exception:
            raise StrictWorkflowVariableStorageUnavailable() from None
        if not row["committed_at"].endswith("Z") or canonical != row["committed_at"]:
            raise StrictWorkflowVariableStorageUnavailable()

    def _validate_stored_value(
        self,
        declaration: VariableDeclaration,
        frame_scope_path: str,
        present: bool,
        value_json: object,
        digest: str,
    ) -> None:
        if not present:
            if value_json is not None or digest != sha256_json({"present": False}):
                raise StrictWorkflowVariableStorageUnavailable()
            return
        if type(value_json) is not str:
            raise StrictWorkflowVariableStorageUnavailable()
        try:
            value = parse_strict_json(value_json)
            if canonical_json(value) != value_json:
                raise StrictWorkflowVariableStorageUnavailable()
            candidate = calculate_variable_candidate(
                self._compiled_contract,
                declaration.declaration_id,
                frame_scope_path,
                "assign",
                previous_exists=False,
                previous_present=False,
                previous_value=MISSING,
                value=value,
            )
        except StrictWorkflowVariableError:
            raise
        except StrictVariableLogicResourceLimit:
            raise StrictWorkflowVariableStorageUnavailable() from None
        except StrictVariableLogicError:
            raise StrictWorkflowVariableStorageUnavailable() from None
        except Exception:
            raise StrictWorkflowVariableStorageUnavailable() from None
        if candidate.canonical != value_json or candidate.value_digest != digest:
            raise StrictWorkflowVariableStorageUnavailable()

    def _validate_state_row(
        self, row: sqlite3.Row, declaration: VariableDeclaration, frame: sqlite3.Row
    ) -> None:
        declaration_id = declaration.declaration_id
        if (
            row["run_id"] != self._expected_binding.run_id
            or row["frame_id"] != frame["frame_id"]
            or row["declaration_id"] != declaration_id
            or row["name"] != declaration.name
            or type(row["present"]) is not int
            or row["present"] not in (0, 1)
            or type(row["version"]) is not int
            or row["version"] < 1
            or type(row["value_digest"]) is not str
            or _SHA256.fullmatch(row["value_digest"]) is None
            or type(row["updated_at"]) is not str
        ):
            raise StrictWorkflowVariableStorageUnavailable()
        if row["present"] == 0:
            self._validate_stored_value(
                declaration, frame["scope_path"], False, row["value_json"], row["value_digest"]
            )
        else:
            self._validate_stored_value(
                declaration, frame["scope_path"], True, row["value_json"], row["value_digest"]
            )
        try:
            parsed = datetime.fromisoformat(row["updated_at"].removesuffix("Z") + "+00:00")
            canonical = _format_utc_timestamp(parsed)
        except Exception:
            raise StrictWorkflowVariableStorageUnavailable() from None
        if not row["updated_at"].endswith("Z") or canonical != row["updated_at"]:
            raise StrictWorkflowVariableStorageUnavailable()

    def _audit_all(
        self, connection: sqlite3.Connection, frames: dict[str, sqlite3.Row]
    ) -> tuple[list[sqlite3.Row], int, int]:
        mutations = connection.execute(
            "SELECT * FROM variable_mutations WHERE run_id=?",
            (self._expected_binding.run_id,),
        ).fetchall()
        if len(mutations) > _MAX_MUTATIONS:
            raise StrictWorkflowVariableStorageUnavailable()
        states = connection.execute(
            "SELECT * FROM variable_states WHERE run_id=?",
            (self._expected_binding.run_id,),
        ).fetchall()
        mutation_groups: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
        state_groups: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
        for row in mutations:
            mutation_groups[(row["frame_id"], row["declaration_id"])].append(row)
        for row in states:
            state_groups[(row["frame_id"], row["declaration_id"])].append(row)
        current_bytes = 0
        for row in states:
            if type(row["present"]) is not int or row["present"] not in (0, 1):
                raise StrictWorkflowVariableStorageUnavailable()
            if row["present"] == 0:
                if row["value_json"] is not None:
                    raise StrictWorkflowVariableStorageUnavailable()
            else:
                if type(row["value_json"]) is not str:
                    raise StrictWorkflowVariableStorageUnavailable()
                try:
                    current_bytes += len(row["value_json"].encode("utf-8", errors="strict"))
                except Exception:
                    raise StrictWorkflowVariableStorageUnavailable() from None
        if current_bytes > _MAX_STATE_BYTES:
            raise StrictWorkflowVariableStorageUnavailable()
        keys = set(mutation_groups) | set(state_groups)
        for frame_id, declaration_id in keys:
            frame = frames.get(frame_id)
            declaration = self._declarations.get(declaration_id)
            if frame is None or declaration is None:
                raise StrictWorkflowVariableStorageUnavailable()
            if declaration.owner_scope != frame["scope_path"]:
                raise StrictWorkflowVariableStorageUnavailable()
            group_mutations = sorted(
                mutation_groups.get((frame_id, declaration_id), []),
                key=lambda item: (
                    item["after_version"] if type(item["after_version"]) is int else -1
                ),
            )
            group_states = state_groups.get((frame_id, declaration_id), [])
            if len(group_states) > 1:
                raise StrictWorkflowVariableStorageUnavailable()
            self._validate_history_rows(
                group_mutations,
                group_states[0] if group_states else None,
                frame,
                declaration,
            )
        return mutations, len(states), current_bytes

    def _validate_history_rows(
        self,
        mutation_rows: list[sqlite3.Row],
        state_row: sqlite3.Row | None,
        frame: sqlite3.Row,
        declaration: VariableDeclaration,
    ) -> _History:
        declaration_id = declaration.declaration_id
        states: dict[int, _VersionState] = {0: _VersionState(0, False, None, None)}
        previous = states[0]
        for row in mutation_rows:
            self._validate_mutation_row(row, frame, declaration_id)
            if (
                row["before_version"] != previous.version
                or row["after_version"] != previous.version + 1
                or bool(row["before_present"]) != previous.present
                or row["before_digest"] != previous.value_digest
            ):
                raise StrictWorkflowVariableStorageUnavailable()
            after_present = bool(row["after_present"])
            self._validate_stored_value(
                declaration,
                frame["scope_path"],
                after_present,
                row["after_value_json"],
                row["after_digest"],
            )
            current = _VersionState(
                row["after_version"],
                after_present,
                row["after_digest"],
                row["after_value_json"],
            )
            if current.version in states:
                raise StrictWorkflowVariableStorageUnavailable()
            states[current.version] = current
            previous = current
        if state_row is None:
            if mutation_rows:
                raise StrictWorkflowVariableStorageUnavailable()
            return _History(states=states, state_row=None)
        self._validate_state_row(state_row, declaration, frame)
        if not mutation_rows:
            raise StrictWorkflowVariableStorageUnavailable()
        last = states[max(states)]
        if (
            state_row["version"] != last.version
            or bool(state_row["present"]) != last.present
            or state_row["value_digest"] != last.value_digest
            or state_row["value_json"] != last.value_json
        ):
            raise StrictWorkflowVariableStorageUnavailable()
        return _History(states=states, state_row=state_row)

    def _replay_existing(
        self,
        row: sqlite3.Row,
        history: _History,
        frame: sqlite3.Row,
        declaration: VariableDeclaration,
        context: MutationContext,
        context_identity: dict[str, object],
        operation: str,
        before_version: int,
        before_digest: str | None,
        operation_args: dict[str, object],
        execution_key: str,
    ) -> StrictWorkflowVariableMutation:
        declaration_id = declaration.declaration_id
        if (
            row["run_id"] != self._expected_binding.run_id
            or row["frame_id"] != context.frame_id
            or row["declaration_id"] != declaration_id
            or row["mutation_ordinal"] != context.mutation_ordinal
            or row["operation"] != operation
        ):
            raise StrictWorkflowVariableNeedsReconciliation()
        before = history.states.get(before_version)
        if before is None or before.value_digest != before_digest:
            raise StrictWorkflowVariableNeedsReconciliation()
        try:
            candidate = calculate_variable_candidate(
                self._compiled_contract,
                declaration_id,
                frame["scope_path"],
                operation,
                previous_exists=before_version > 0,
                previous_present=before.present,
                previous_value=(
                    parse_strict_json(before.value_json)
                    if before.present and before.value_json is not None
                    else MISSING
                ),
                **operation_args,
            )
        except StrictVariableLogicError:
            raise StrictWorkflowVariableNeedsReconciliation() from None
        if not candidate.state_exists:
            raise StrictWorkflowVariableNeedsReconciliation()
        expected_payload_digest = self._payload_digest(
            context_identity,
            execution_key,
            declaration_id,
            operation,
            before_version,
            before_digest,
            operation_args,
        )
        if row["payload_digest"] != expected_payload_digest:
            raise StrictWorkflowVariableNeedsReconciliation()
        after = history.states.get(row["after_version"])
        if (
            after is None
            or row["before_version"] != before_version
            or row["before_digest"] != before_digest
            or not self._candidate_matches_history(candidate, after)
            or row["after_version"] != before_version + 1
        ):
            raise StrictWorkflowVariableStorageUnavailable()
        return self._mutation_from_row(row, replayed=True)

    @staticmethod
    def _candidate_matches_history(candidate: VariableCandidate, state: _VersionState) -> bool:
        return (
            candidate.present == state.present
            and candidate.canonical == state.value_json
            and candidate.value_digest == state.value_digest
        )

    def _current_value_bytes(self, connection: sqlite3.Connection) -> int:
        rows = connection.execute(
            "SELECT present, value_json FROM variable_states WHERE run_id=?",
            (self._expected_binding.run_id,),
        ).fetchall()
        total = 0
        for row in rows:
            if type(row["present"]) is not int or row["present"] not in (0, 1):
                raise StrictWorkflowVariableStorageUnavailable()
            if row["present"] == 0:
                if row["value_json"] is not None:
                    raise StrictWorkflowVariableStorageUnavailable()
            elif type(row["value_json"]) is str:
                try:
                    total += len(row["value_json"].encode("utf-8", errors="strict"))
                except Exception:
                    raise StrictWorkflowVariableStorageUnavailable() from None
            else:
                raise StrictWorkflowVariableStorageUnavailable()
        if total > _MAX_STATE_BYTES:
            raise StrictWorkflowVariableStorageUnavailable()
        return total

    @staticmethod
    def _replace_value_size(
        current_bytes: int, previous: _VersionState, candidate: VariableCandidate
    ) -> int:
        previous_size = (
            len(previous.value_json.encode("utf-8"))
            if previous.present and previous.value_json is not None
            else 0
        )
        next_size = (
            len(candidate.canonical.encode("utf-8"))
            if candidate.present and candidate.canonical is not None
            else 0
        )
        return current_bytes - previous_size + next_size

    def _mutation_from_row(
        self, row: sqlite3.Row | None, *, replayed: bool
    ) -> StrictWorkflowVariableMutation:
        if row is None:
            raise StrictWorkflowVariableStorageUnavailable()
        return StrictWorkflowVariableMutation(
            execution_key=row["execution_key"],
            payload_digest=row["payload_digest"],
            run_id=row["run_id"],
            frame_id=row["frame_id"],
            declaration_id=row["declaration_id"],
            mutation_ordinal=row["mutation_ordinal"],
            operation=row["operation"],
            before_version=row["before_version"],
            after_version=row["after_version"],
            before_present=bool(row["before_present"]),
            after_present=bool(row["after_present"]),
            before_digest=row["before_digest"],
            after_digest=row["after_digest"],
            committed_at=row["committed_at"],
            replayed=replayed,
            _after_value_json=row["after_value_json"],
        )

    def _fault_checkpoint(self, name: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(name)

    @staticmethod
    def _close_connection(connection: sqlite3.Connection | None, committed: bool) -> None:
        if connection is None:
            return
        try:
            connection.close()
        except sqlite3.Error:
            if committed:
                raise StrictWorkflowVariableStorageUnavailable() from None

    @staticmethod
    def _close_read_connection(connection: sqlite3.Connection | None) -> None:
        if connection is None:
            return
        try:
            connection.close()
        except sqlite3.Error:
            pass


__all__ = [
    "MutationContext",
    "StrictWorkflowVariableLedgerInfo",
    "StrictWorkflowVariableMutation",
    "StrictWorkflowVariableMutationStore",
    "StrictWorkflowVariableNeedsReconciliation",
    "StrictWorkflowVariableResourceLimit",
    "StrictWorkflowVariableState",
]
