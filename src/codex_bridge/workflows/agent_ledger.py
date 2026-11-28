"""Run-local durable ledger for D2 Agent invocations.

The file is initialized explicitly for a new isolated run. Opening or inspecting an
existing run never creates, repairs, or migrates its database.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from .agent_contracts import (
    AgentError,
    AgentRequest,
    AgentResult,
    DispatchReceipt,
    Observation,
    PreparedProviderRequest,
    TargetContext,
    result_from_observation,
)
from .contracts import canonical_json, parse_strict_json
from .strict_variables import _is_symlink_or_junction

_DATABASE_NAME = "agent_invocations.sqlite"
_BUSY_TIMEOUT_MS = 10_000
_SQLITE_INTEGER_MAX = 2**63 - 1
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_CODEX_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,99}\Z", re.ASCII)
_CODEX_REMOTE_STATES = frozenset(
    {
        "in_progress",
        "needs_approval",
        "needs_input",
        "completed",
        "failed",
        "interrupted",
        "timed_out",
        "unknown",
    }
)
_CODEX_TERMINAL_REMOTE_STATES = frozenset({"completed", "failed", "interrupted", "timed_out"})
_BINDING_KEYS = frozenset(
    {
        "run_id",
        "workflow_id",
        "definition_id",
        "definition_revision",
        "request_digest",
        "source_sha256",
        "contract_sha256",
        "binding_digest",
        "normalized_input_sha256",
    }
)

_RUN_BINDING_SQL = """
CREATE TABLE run_binding (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    binding_json TEXT NOT NULL,
    binding_sha256 TEXT NOT NULL,
    target_context_json TEXT NOT NULL,
    target_context_sha256 TEXT NOT NULL
)
"""

_INVOCATIONS_SQL = """
CREATE TABLE invocations (
    invocation_id TEXT PRIMARY KEY,
    node_pointer TEXT NOT NULL,
    iteration_json TEXT NOT NULL,
    operation TEXT NOT NULL CHECK(operation IN ('invoke','observe','wait','interrupt')),
    request_json TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    client_request_id TEXT UNIQUE,
    dispatch_state TEXT NOT NULL CHECK(dispatch_state IN
      ('NONE','PREPARED','MAYBE_SENT','ACCEPTED','REJECTED')),
    dispatch_receipt_json TEXT,
    dispatch_receipt_sha256 TEXT,
    created_ms INTEGER NOT NULL,
    deadline_ms INTEGER,
    result_json TEXT,
    result_sha256 TEXT,
    UNIQUE(node_pointer,iteration_json),
    CHECK((result_json IS NULL)=(result_sha256 IS NULL)),
    CHECK((dispatch_receipt_json IS NULL)=(dispatch_receipt_sha256 IS NULL)),
    CHECK((operation IN ('observe','wait') AND dispatch_state='NONE'
      AND client_request_id IS NULL) OR
      (operation IN ('invoke','interrupt') AND dispatch_state<>'NONE'
      AND client_request_id IS NOT NULL)),
    CHECK(deadline_ms IS NULL OR deadline_ms>=created_ms)
)
"""

_OWNED_HANDLES_SQL = """
CREATE TABLE owned_handles (
    invocation_id TEXT PRIMARY KEY REFERENCES invocations(invocation_id),
    provider TEXT NOT NULL,
    target_identity_sha256 TEXT NOT NULL,
    handle_json TEXT NOT NULL,
    handle_sha256 TEXT NOT NULL,
    remote_state TEXT,
    observed_ms INTEGER,
    remote_terminal INTEGER NOT NULL CHECK(remote_terminal IN (0,1)),
    UNIQUE(provider,target_identity_sha256,handle_sha256)
)
"""

_EXPECTED_TABLE_SQL = {
    "run_binding": _RUN_BINDING_SQL,
    "invocations": _INVOCATIONS_SQL,
    "owned_handles": _OWNED_HANDLES_SQL,
}


class AgentLedgerIntegrityError(RuntimeError):
    """The run-local ledger is absent, invalid, or disagrees with its binding."""


class AgentLedgerConflictError(RuntimeError):
    """A logical invocation or immutable ledger value conflicts with prior state."""


@dataclass(frozen=True, slots=True)
class AgentLedgerInspection:
    binding_identity: dict[str, Any] = field(repr=False)
    target_context: dict[str, Any] = field(repr=False)

    def __repr__(self) -> str:
        return "AgentLedgerInspection(<verified>)"


@dataclass(frozen=True, slots=True)
class AgentInvocationRecord:
    invocation_id: str
    node_pointer: str
    iteration_json: str = field(repr=False)
    operation: str
    request_json: str = field(repr=False)
    request_sha256: str
    client_request_id: str | None
    dispatch_state: str
    dispatch_receipt_json: str | None = field(repr=False)
    dispatch_receipt_sha256: str | None
    created_ms: int
    deadline_ms: int | None
    result_json: str | None = field(repr=False)
    result_sha256: str | None

    @property
    def iteration_vector(self) -> list[int]:
        return cast(list[int], _load_canonical(self.iteration_json))

    @property
    def request(self) -> PreparedProviderRequest:
        return PreparedProviderRequest.from_json(self.request_json)


@dataclass(frozen=True, slots=True)
class AgentOwnedHandle:
    invocation_id: str
    provider: str
    target_identity_sha256: str
    handle_json: str = field(repr=False)
    handle_sha256: str
    remote_state: str | None
    observed_ms: int | None
    remote_terminal: bool

    @property
    def handle(self) -> dict[str, Any]:
        return cast(dict[str, Any], _load_canonical(self.handle_json))


class AgentInvocationLedger:
    """A per-run SQLite store with separated create, open, and inspect paths."""

    def __init__(
        self,
        run_directory: Path,
        binding_identity: object,
        target_context: TargetContext,
    ) -> None:
        self._run_directory = Path(run_directory)
        self._binding_json = _canonical_binding(binding_identity)
        self._target_context_json = _canonical_target_context(target_context)

    @classmethod
    def initialize_new_database(
        cls,
        run_directory: Path,
        binding_identity: object,
        target_context: TargetContext,
    ) -> AgentInvocationLedger:
        """Create a new database and its binding atomically, refusing every existing path."""
        ledger = cls(run_directory, binding_identity, target_context)
        root = ledger._resolve_run_directory()
        database = root / _DATABASE_NAME
        related_paths = _database_related_paths(database)
        if any(_is_symlink_or_junction(path) or path.exists() for path in related_paths):
            raise AgentLedgerIntegrityError("agent ledger already exists")
        if database.parent.resolve(strict=True) != root:
            raise AgentLedgerIntegrityError("agent ledger path is invalid")

        connection: sqlite3.Connection | None = None
        created_by_call = False
        initialized = False
        try:
            with database.open("xb"):
                pass
            created_by_call = True
            connection = _connect_write(database, initialize=True)
            connection.execute("BEGIN IMMEDIATE")
            _expect_empty_database(connection)
            _create_schema(connection)
            connection.execute("PRAGMA user_version=1")
            connection.execute(
                "INSERT INTO run_binding VALUES (1, ?, ?, ?, ?)",
                (
                    ledger._binding_json,
                    _sha256(ledger._binding_json),
                    ledger._target_context_json,
                    _sha256(ledger._target_context_json),
                ),
            )
            _check_schema(connection)
            _validate_run_binding(
                connection,
                ledger._binding_json,
                ledger._target_context_json,
            )
            connection.execute("COMMIT")
            initialized = True
        except AgentLedgerIntegrityError:
            _rollback(connection)
            raise
        except Exception:
            _rollback(connection)
            raise AgentLedgerIntegrityError("agent ledger initialization failed") from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    if initialized:
                        raise AgentLedgerIntegrityError("agent ledger close failed") from None
            if created_by_call and not initialized:
                _remove_created_files(related_paths)
        return ledger

    @classmethod
    def open_existing(
        cls,
        run_directory: Path,
        expected_binding_identity: object,
        expected_target_context: TargetContext,
    ) -> AgentInvocationLedger:
        """Open a required ledger for updates without creating or migrating it."""
        ledger = cls(run_directory, expected_binding_identity, expected_target_context)
        with ledger._read_connection() as connection:
            _check_schema(connection)
            _validate_run_binding(
                connection,
                ledger._binding_json,
                ledger._target_context_json,
            )
        return ledger

    @classmethod
    def inspect_read_only(
        cls,
        run_directory: Path,
        expected_binding_identity: object,
        expected_target_context: TargetContext,
    ) -> AgentLedgerInspection:
        """Verify an existing ledger using a query-only, non-creating connection."""
        ledger = cls(run_directory, expected_binding_identity, expected_target_context)
        with ledger._read_connection() as connection:
            _check_schema(connection)
            _validate_run_binding(
                connection,
                ledger._binding_json,
                ledger._target_context_json,
            )
        return AgentLedgerInspection(
            cast(dict[str, Any], _load_canonical(ledger._binding_json)),
            cast(dict[str, Any], _load_canonical(ledger._target_context_json)),
        )

    @classmethod
    def inspect_stored_binding(cls, run_directory: Path) -> AgentLedgerInspection:
        """Read and validate stored binding and target context without current config."""
        ledger = object.__new__(cls)
        ledger._run_directory = Path(run_directory)
        with ledger._read_connection() as connection:
            _check_schema(connection)
            binding, target_context = _read_validated_run_binding(connection)
        return AgentLedgerInspection(binding, target_context)

    def read_binding(self) -> dict[str, Any]:
        with self._read_connection() as connection:
            _check_schema(connection)
            _validate_run_binding(connection, self._binding_json, self._target_context_json)
        return {
            "binding_identity": cast(dict[str, Any], _load_canonical(self._binding_json)),
            "target_context": cast(dict[str, Any], _load_canonical(self._target_context_json)),
        }

    def prepare_invoke(
        self,
        node_pointer: str,
        iteration_vector: object,
        prepared: PreparedProviderRequest,
        *,
        created_ms: int | None = None,
    ) -> AgentInvocationRecord:
        if type(prepared) is not PreparedProviderRequest:
            raise AgentLedgerIntegrityError("prepared Agent request is invalid")
        try:
            normalized = PreparedProviderRequest.from_json(prepared.to_json())
        except Exception:
            raise AgentLedgerIntegrityError("prepared Agent request is invalid") from None
        target_context = TargetContext.from_dict(normalized.target_identity)
        if (
            normalized.operation not in {"invoke", "observe", "wait", "interrupt"}
            or normalized.provider != target_context.provider
            or normalized.target != target_context.target
            or canonical_json(target_context.to_dict()) != self._target_context_json
        ):
            raise AgentLedgerIntegrityError("prepared Agent request does not match the run")
        if normalized.operation in {"observe", "wait", "interrupt"}:
            _canonical_handle(normalized.handle, self._target_context_json)
        pointer = _validate_node_pointer(node_pointer)
        iteration_json = _canonical_iteration(iteration_vector)
        timestamp = _utc_epoch_ms() if created_ms is None else _sqlite_timestamp(created_ms)
        timeout_seconds = normalized.timeout_seconds
        deadline = None
        if timeout_seconds is not None:
            candidate = timestamp + timeout_seconds * 1000
            if candidate > _SQLITE_INTEGER_MAX:
                raise AgentLedgerConflictError("Agent deadline is outside SQLite integer range")
            deadline = candidate

        identity = {
            "namespace": "codexbridge-agent-v1",
            "binding": cast(dict[str, Any], _load_canonical(self._binding_json)),
            "node_pointer": pointer,
            "iteration_vector": cast(list[int], _load_canonical(iteration_json)),
        }
        invocation_id = _sha256(canonical_json(identity))
        client_request_id = (
            f"cba-{invocation_id}" if normalized.operation in {"invoke", "interrupt"} else None
        )
        dispatch_state = "NONE" if normalized.operation in {"observe", "wait"} else "PREPARED"
        request_json = normalized.to_json()
        request_sha256 = _sha256(request_json)

        with self._write_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM invocations WHERE invocation_id=?",
                (invocation_id,),
            ).fetchone()
            if row is not None:
                existing = _validate_invocation_row(
                    row, self._binding_json, self._target_context_json
                )
                if (
                    existing.node_pointer != pointer
                    or existing.iteration_json != iteration_json
                    or existing.operation != normalized.operation
                    or existing.request_json != request_json
                    or existing.request_sha256 != request_sha256
                    or existing.client_request_id != client_request_id
                ):
                    raise AgentLedgerConflictError("logical Agent invocation changed its request")
                _validate_row_relationships(connection, existing, self._target_context_json)
                return existing
            connection.execute(
                """INSERT INTO invocations (
                    invocation_id,node_pointer,iteration_json,operation,request_json,
                    request_sha256,client_request_id,dispatch_state,dispatch_receipt_json,
                    dispatch_receipt_sha256,created_ms,deadline_ms,result_json,result_sha256
                ) VALUES (?,?,?,?,?,?,?,?,NULL,NULL,?,?,NULL,NULL)""",
                (
                    invocation_id,
                    pointer,
                    iteration_json,
                    normalized.operation,
                    request_json,
                    request_sha256,
                    client_request_id,
                    dispatch_state,
                    timestamp,
                    deadline,
                ),
            )
            stored = connection.execute(
                "SELECT * FROM invocations WHERE invocation_id=?", (invocation_id,)
            ).fetchone()
            if stored is None:
                raise AgentLedgerIntegrityError("prepared Agent invocation was not stored")
            return _validate_invocation_row(stored, self._binding_json, self._target_context_json)

    def invocation_id_for(
        self,
        node_pointer: str,
        iteration_vector: object,
    ) -> str:
        pointer = _validate_node_pointer(node_pointer)
        iteration_json = _canonical_iteration(iteration_vector)
        identity = {
            "namespace": "codexbridge-agent-v1",
            "binding": cast(dict[str, Any], _load_canonical(self._binding_json)),
            "node_pointer": pointer,
            "iteration_vector": cast(list[int], _load_canonical(iteration_json)),
        }
        return _sha256(canonical_json(identity))

    def get_invocation_for_node(
        self,
        node_pointer: str,
        iteration_vector: object,
    ) -> AgentInvocationRecord | None:
        pointer = _validate_node_pointer(node_pointer)
        iteration_json = _canonical_iteration(iteration_vector)
        expected_id = self.invocation_id_for(pointer, iteration_vector)
        with self._read_connection() as connection:
            _check_schema(connection)
            _validate_run_binding(connection, self._binding_json, self._target_context_json)
            row = connection.execute(
                "SELECT * FROM invocations WHERE node_pointer=? AND iteration_json=?",
                (pointer, iteration_json),
            ).fetchone()
            if row is None:
                return None
            record = _validate_invocation_row(row, self._binding_json, self._target_context_json)
            if record.invocation_id != expected_id:
                raise AgentLedgerIntegrityError("logical Agent invocation identity is invalid")
            _validate_row_relationships(connection, record, self._target_context_json)
            return record

    def get_invocation(self, invocation_id: str) -> AgentInvocationRecord:
        _validate_invocation_id(invocation_id)
        with self._read_connection() as connection:
            _check_schema(connection)
            _validate_run_binding(connection, self._binding_json, self._target_context_json)
            row = connection.execute(
                "SELECT * FROM invocations WHERE invocation_id=?", (invocation_id,)
            ).fetchone()
            if row is None:
                raise AgentLedgerIntegrityError("Agent invocation is missing")
            record = _validate_invocation_row(row, self._binding_json, self._target_context_json)
            _validate_row_relationships(connection, record, self._target_context_json)
            return record

    def claim_dispatch(self, invocation_id: str, expected_request_sha256: str) -> bool:
        _validate_invocation_id(invocation_id)
        if _SHA256.fullmatch(expected_request_sha256) is None:
            raise AgentLedgerIntegrityError("Agent request digest is invalid")
        with self._write_transaction() as connection:
            row = _get_invocation_row(connection, invocation_id)
            record = _validate_invocation_row(row, self._binding_json, self._target_context_json)
            _validate_row_relationships(connection, record, self._target_context_json)
            if record.request_sha256 != expected_request_sha256:
                raise AgentLedgerConflictError("Agent request changed before dispatch")
            if record.dispatch_state != "PREPARED" or record.result_json is not None:
                return False
            cursor = connection.execute(
                "UPDATE invocations SET dispatch_state='MAYBE_SENT' "
                "WHERE invocation_id=? AND dispatch_state='PREPARED' "
                "AND request_sha256=? AND result_json IS NULL",
                (invocation_id, expected_request_sha256),
            )
            return bool(cursor.rowcount == 1)

    def save_dispatch_receipt(
        self,
        invocation_id: str,
        receipt: DispatchReceipt,
    ) -> AgentInvocationRecord:
        _validate_invocation_id(invocation_id)
        if type(receipt) is not DispatchReceipt:
            raise AgentLedgerIntegrityError("dispatch receipt is invalid")
        try:
            raw_json = receipt.to_json()
            decoded = parse_strict_json(raw_json)
            normalized = DispatchReceipt.from_json(raw_json)
        except Exception:
            raise AgentLedgerIntegrityError("dispatch receipt is invalid") from None
        if canonical_json(decoded) != raw_json or normalized.to_json() != raw_json:
            raise AgentLedgerIntegrityError("dispatch receipt is not canonical")
        with self._write_transaction() as connection:
            row = _get_invocation_row(connection, invocation_id)
            record = _validate_invocation_row(row, self._binding_json, self._target_context_json)
            _validate_row_relationships(connection, record, self._target_context_json)
            _validate_receipt_for_request(record.request, receipt)
            if record.dispatch_state in {"ACCEPTED", "REJECTED"}:
                if record.dispatch_receipt_json == raw_json:
                    return record
                raise AgentLedgerConflictError("dispatch receipt is immutable")
            if record.dispatch_state != "MAYBE_SENT" or record.result_json is not None:
                raise AgentLedgerIntegrityError("dispatch receipt has no matching claim")
            state = receipt.disposition
            if state == "UNCERTAIN":
                raise AgentLedgerIntegrityError("uncertain dispatch is not a confirmed receipt")
            receipt_sha256 = _sha256(raw_json)
            if state == "ACCEPTED":
                handle_json = canonical_json(receipt.handle)
                handle_sha256 = _sha256(handle_json)
                target_identity_sha256 = _sha256(self._target_context_json)
                connection.execute(
                    """UPDATE invocations SET dispatch_state='ACCEPTED',
                        dispatch_receipt_json=?,dispatch_receipt_sha256=?
                        WHERE invocation_id=? AND dispatch_state='MAYBE_SENT'
                        AND dispatch_receipt_json IS NULL""",
                    (raw_json, receipt_sha256, invocation_id),
                )
                if record.operation == "invoke":
                    connection.execute(
                        """INSERT INTO owned_handles (
                            invocation_id,provider,target_identity_sha256,handle_json,
                            handle_sha256,remote_state,observed_ms,remote_terminal
                        ) VALUES (?,?,?,?,?,?,?,?)""",
                        (
                            invocation_id,
                            record.request.provider,
                            target_identity_sha256,
                            handle_json,
                            handle_sha256,
                            receipt.remote_state,
                            None,
                            int(
                                record.request.provider == "codex"
                                and receipt.remote_state in _CODEX_TERMINAL_REMOTE_STATES
                            ),
                        ),
                    )
            else:
                connection.execute(
                    """UPDATE invocations SET dispatch_state='REJECTED',
                        dispatch_receipt_json=?,dispatch_receipt_sha256=?
                        WHERE invocation_id=? AND dispatch_state='MAYBE_SENT'
                        AND dispatch_receipt_json IS NULL""",
                    (raw_json, receipt_sha256, invocation_id),
                )
            stored = _get_invocation_row(connection, invocation_id)
            updated = _validate_invocation_row(
                stored, self._binding_json, self._target_context_json
            )
            _validate_row_relationships(connection, updated, self._target_context_json)
            return updated

    def save_result(
        self,
        invocation_id: str,
        result: AgentResult,
    ) -> AgentInvocationRecord:
        _validate_invocation_id(invocation_id)
        if type(result) is not AgentResult:
            raise AgentLedgerIntegrityError("Agent result is invalid")
        try:
            raw_json = result.to_json()
            decoded = parse_strict_json(raw_json)
            normalized = AgentResult.from_json(raw_json)
        except Exception:
            raise AgentLedgerIntegrityError("Agent result is invalid") from None
        if canonical_json(decoded) != raw_json or normalized.to_json() != raw_json:
            raise AgentLedgerIntegrityError("Agent result is not canonical")
        with self._write_transaction() as connection:
            row = _get_invocation_row(connection, invocation_id)
            record = _validate_invocation_row(row, self._binding_json, self._target_context_json)
            owned = _validate_row_relationships(connection, record, self._target_context_json)
            _validate_result_for_record(record, result, owned)
            if record.result_json is not None:
                if record.result_json == raw_json:
                    return record
                raise AgentLedgerConflictError("logical Agent result is immutable")
            digest = _sha256(raw_json)
            connection.execute(
                "UPDATE invocations SET result_json=?,result_sha256=? "
                "WHERE invocation_id=? AND result_json IS NULL AND result_sha256 IS NULL",
                (raw_json, digest, invocation_id),
            )
            stored = _get_invocation_row(connection, invocation_id)
            updated = _validate_invocation_row(
                stored, self._binding_json, self._target_context_json
            )
            _validate_row_relationships(connection, updated, self._target_context_json)
            return updated

    def update_remote_state(
        self,
        invocation_id: str,
        handle: object,
        target_context: TargetContext,
        *,
        remote_state: str,
        observed_ms: int,
        remote_terminal: bool,
    ) -> AgentOwnedHandle:
        _validate_invocation_id(invocation_id)
        if (
            type(target_context) is not TargetContext
            or canonical_json(target_context.to_dict()) != self._target_context_json
            or type(remote_state) is not str
            or not remote_state
            or type(remote_terminal) is not bool
        ):
            raise AgentLedgerIntegrityError("remote observation identity is invalid")
        if target_context.provider == "codex":
            _validate_codex_remote_state(remote_state, remote_terminal)
        timestamp = _sqlite_timestamp(observed_ms)
        handle_json = _canonical_handle(handle, self._target_context_json)
        handle_sha256 = _sha256(handle_json)
        with self._write_transaction() as connection:
            record = _validate_invocation_row(
                _get_invocation_row(connection, invocation_id),
                self._binding_json,
                self._target_context_json,
            )
            owned = _validate_row_relationships(connection, record, self._target_context_json)
            if (
                record.dispatch_state != "ACCEPTED"
                or owned is None
                or owned.handle_json != handle_json
                or owned.target_identity_sha256 != _sha256(self._target_context_json)
            ):
                raise AgentLedgerIntegrityError("remote observation is not for an owned handle")
            if owned.observed_ms is not None:
                if timestamp < owned.observed_ms:
                    return owned
                if timestamp == owned.observed_ms and (
                    owned.remote_state != remote_state or owned.remote_terminal != remote_terminal
                ):
                    raise AgentLedgerConflictError("remote observation timestamp conflicts")
                if owned.remote_terminal and not remote_terminal:
                    raise AgentLedgerConflictError(
                        "terminal remote state cannot become nonterminal"
                    )
                if owned.remote_terminal and owned.remote_state != remote_state:
                    raise AgentLedgerConflictError("terminal remote state is immutable")
            connection.execute(
                """UPDATE owned_handles SET remote_state=?,observed_ms=?,remote_terminal=?
                    WHERE invocation_id=? AND provider=? AND target_identity_sha256=?
                    AND handle_json=? AND handle_sha256=?""",
                (
                    remote_state,
                    timestamp,
                    int(remote_terminal),
                    invocation_id,
                    record.request.provider,
                    _sha256(self._target_context_json),
                    handle_json,
                    handle_sha256,
                ),
            )
            stored = connection.execute(
                "SELECT * FROM owned_handles WHERE invocation_id=?", (invocation_id,)
            ).fetchone()
            if stored is None:
                raise AgentLedgerIntegrityError("owned Agent handle is missing")
            return _validate_owned_row(stored, record, self._target_context_json)

    def get_owned_handle(self, invocation_id: str) -> AgentOwnedHandle:
        record = self.get_invocation(invocation_id)
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM owned_handles WHERE invocation_id=?", (invocation_id,)
            ).fetchone()
            if row is None:
                raise AgentLedgerIntegrityError("owned Agent handle is missing")
            return _validate_owned_row(row, record, self._target_context_json)

    def find_owned_handle(
        self,
        handle: object,
        target_context: TargetContext,
    ) -> AgentOwnedHandle | None:
        """Find a previously accepted handle by its complete provider/target/handle identity."""
        if (
            type(target_context) is not TargetContext
            or canonical_json(target_context.to_dict()) != self._target_context_json
        ):
            raise AgentLedgerIntegrityError("remote observation identity is invalid")
        handle_json = _canonical_handle(handle, self._target_context_json)
        handle_digest = _sha256(handle_json)
        target_digest = _sha256(self._target_context_json)
        with self._read_connection() as connection:
            _check_schema(connection)
            _validate_run_binding(connection, self._binding_json, self._target_context_json)
            rows = connection.execute(
                """SELECT * FROM owned_handles WHERE provider=?
                   AND target_identity_sha256=? AND handle_sha256=?""",
                (target_context.provider, target_digest, handle_digest),
            ).fetchall()
            if len(rows) > 1:
                raise AgentLedgerIntegrityError("owned Agent handle identity is ambiguous")
            if not rows:
                return None
            row = rows[0]
            if row["handle_json"] != handle_json:
                raise AgentLedgerIntegrityError("owned Agent handle digest collides")
            record = _validate_invocation_row(
                _get_invocation_row(connection, row["invocation_id"]),
                self._binding_json,
                self._target_context_json,
            )
            if record.dispatch_state != "ACCEPTED":
                raise AgentLedgerIntegrityError("owned Agent handle has no accepted invoke")
            _validate_row_relationships(connection, record, self._target_context_json)
            return _validate_owned_row(row, record, self._target_context_json)

    def update_remote_state_for_handle(
        self,
        handle: object,
        target_context: TargetContext,
        *,
        remote_state: str,
        observed_ms: int,
        remote_terminal: bool,
    ) -> AgentOwnedHandle | None:
        """Update an existing exact owner row; foreign handles are never adopted."""
        owned = self.find_owned_handle(handle, target_context)
        if owned is None:
            return None
        if target_context.provider == "codex":
            _validate_codex_remote_state(remote_state, remote_terminal)
            if remote_state == "unknown" and owned.remote_terminal:
                timestamp = _sqlite_timestamp(observed_ms)
                if owned.observed_ms is not None and timestamp == owned.observed_ms:
                    raise AgentLedgerConflictError("remote observation timestamp conflicts")
                return owned
        return self.update_remote_state(
            owned.invocation_id,
            handle,
            target_context,
            remote_state=remote_state,
            observed_ms=observed_ms,
            remote_terminal=remote_terminal,
        )

    def list_invocations(
        self,
        *,
        limit: int = 50,
        after: tuple[int, str] | None = None,
    ) -> tuple[AgentInvocationRecord, ...]:
        page_size = _page_size(limit)
        if after is not None:
            if type(after) is not tuple or len(after) != 2:
                raise AgentLedgerIntegrityError("Agent invocation cursor is invalid")
            timestamp = _sqlite_timestamp(after[0])
            _validate_invocation_id(after[1])
        with self._read_connection() as connection:
            _check_schema(connection)
            _validate_run_binding(connection, self._binding_json, self._target_context_json)
            if after is None:
                rows = connection.execute(
                    "SELECT * FROM invocations ORDER BY created_ms,invocation_id LIMIT ?",
                    (page_size,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT * FROM invocations WHERE (created_ms,invocation_id)>(?,?)
                       ORDER BY created_ms,invocation_id LIMIT ?""",
                    (timestamp, after[1], page_size),
                ).fetchall()
            records = tuple(
                _validate_invocation_row(row, self._binding_json, self._target_context_json)
                for row in rows
            )
            for record in records:
                _validate_row_relationships(connection, record, self._target_context_json)
            return records

    def list_owned_handles(
        self,
        *,
        limit: int = 50,
        after_invocation_id: str | None = None,
    ) -> tuple[AgentOwnedHandle, ...]:
        page_size = _page_size(limit)
        if after_invocation_id is not None:
            _validate_invocation_id(after_invocation_id)
        with self._read_connection() as connection:
            _check_schema(connection)
            _validate_run_binding(connection, self._binding_json, self._target_context_json)
            if after_invocation_id is None:
                rows = connection.execute(
                    "SELECT * FROM owned_handles ORDER BY invocation_id LIMIT ?", (page_size,)
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM owned_handles WHERE invocation_id>? "
                    "ORDER BY invocation_id LIMIT ?",
                    (after_invocation_id, page_size),
                ).fetchall()
            result: list[AgentOwnedHandle] = []
            for row in rows:
                record_row = _get_invocation_row(connection, row["invocation_id"])
                record = _validate_invocation_row(
                    record_row, self._binding_json, self._target_context_json
                )
                if record.dispatch_state != "ACCEPTED":
                    raise AgentLedgerIntegrityError("owned Agent handle has no accepted invoke")
                result.append(_validate_owned_row(row, record, self._target_context_json))
            return tuple(result)

    def _resolve_run_directory(self) -> Path:
        candidate = self._run_directory.expanduser()
        if _is_symlink_or_junction(candidate):
            raise AgentLedgerIntegrityError("Agent run directory is invalid")
        try:
            if not candidate.exists() or not candidate.is_dir():
                raise AgentLedgerIntegrityError("Agent run directory is missing")
            root = candidate.resolve(strict=True)
            if not root.is_dir():
                raise AgentLedgerIntegrityError("Agent run directory is invalid")
            return root
        except AgentLedgerIntegrityError:
            raise
        except Exception:
            raise AgentLedgerIntegrityError("Agent run directory is invalid") from None

    def _database_path(self) -> Path:
        root = self._resolve_run_directory()
        database = root / _DATABASE_NAME
        related = _database_related_paths(database)
        if any(_is_symlink_or_junction(path) for path in related):
            raise AgentLedgerIntegrityError("Agent ledger path is invalid")
        if not database.exists() or not database.is_file():
            raise AgentLedgerIntegrityError("Agent ledger is missing")
        try:
            if (
                database.parent.resolve(strict=True) != root
                or database.resolve(strict=True).parent != root
            ):
                raise AgentLedgerIntegrityError("Agent ledger path is invalid")
        except AgentLedgerIntegrityError:
            raise
        except Exception:
            raise AgentLedgerIntegrityError("Agent ledger path is invalid") from None
        return database

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        database = self._database_path()
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                database.resolve(strict=True).as_uri() + "?mode=ro",
                uri=True,
                timeout=_BUSY_TIMEOUT_MS / 1000,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
        except Exception:
            if connection is not None:
                connection.close()
            raise AgentLedgerIntegrityError("Agent ledger could not be opened read-only") from None
        try:
            yield connection
        finally:
            connection.close()

    def _write_connection(self) -> sqlite3.Connection:
        return _connect_write(self._database_path(), initialize=False)

    def _write_transaction(self) -> _WriteTransaction:
        return _WriteTransaction(
            self._write_connection,
            self._binding_json,
            self._target_context_json,
        )


class _WriteTransaction:
    def __init__(self, connect: Any, binding_json: str, target_context_json: str) -> None:
        self._connect = connect
        self._binding_json = binding_json
        self._target_context_json = target_context_json
        self.connection: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection:
        try:
            self.connection = self._connect()
            self.connection.execute("BEGIN IMMEDIATE")
            _check_schema(self.connection)
            _validate_run_binding(
                self.connection,
                self._binding_json,
                self._target_context_json,
            )
            return self.connection
        except AgentLedgerIntegrityError:
            _rollback(self.connection)
            self._close()
            raise
        except Exception:
            _rollback(self.connection)
            self._close()
            raise AgentLedgerIntegrityError(
                "Agent ledger write transaction could not start"
            ) from None

    def __exit__(self, error_type: Any, error: Any, traceback: Any) -> Literal[False]:
        connection = self.connection
        if connection is None:
            return False
        try:
            if error_type is None:
                connection.execute("COMMIT")
            else:
                _rollback(connection)
                if isinstance(error, sqlite3.Error):
                    raise AgentLedgerIntegrityError("Agent ledger transaction failed") from None
        except AgentLedgerIntegrityError:
            raise
        except sqlite3.Error:
            _rollback(connection)
            if error_type is None:
                raise AgentLedgerIntegrityError(
                    "Agent ledger transaction could not commit"
                ) from None
        finally:
            self._close()
        return False

    def _close(self) -> None:
        if self.connection is not None:
            try:
                self.connection.close()
            except sqlite3.Error:
                pass
            self.connection = None


def _canonical_binding(value: object) -> str:
    if type(value) is not dict or set(value) != _BINDING_KEYS:
        raise AgentLedgerIntegrityError("Agent run binding is incomplete")
    item = cast(dict[str, Any], value)
    identifiers = ("run_id", "workflow_id", "definition_id")
    digests = _BINDING_KEYS - {*identifiers, "definition_revision"}
    if any(type(item[key]) is not str or not item[key] for key in identifiers):
        raise AgentLedgerIntegrityError("Agent run binding is invalid")
    if type(item["definition_revision"]) is not int or item["definition_revision"] < 1:
        raise AgentLedgerIntegrityError("Agent run binding is invalid")
    if any(type(item[key]) is not str or _SHA256.fullmatch(item[key]) is None for key in digests):
        raise AgentLedgerIntegrityError("Agent run binding is invalid")
    return canonical_json(parse_strict_json(canonical_json(item)))


def _canonical_target_context(value: TargetContext) -> str:
    if type(value) is not TargetContext:
        raise AgentLedgerIntegrityError("Agent target context is invalid")
    try:
        return canonical_json(value.to_dict())
    except Exception:
        raise AgentLedgerIntegrityError("Agent target context is invalid") from None


def _validate_node_pointer(value: object) -> str:
    if type(value) is not str or not value.startswith("/") or "\x00" in value:
        raise AgentLedgerIntegrityError("Agent node pointer is invalid")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise AgentLedgerIntegrityError("Agent node pointer is invalid") from None
    return value


def _canonical_iteration(value: object) -> str:
    if type(value) is not list or any(type(item) is not int or item < 0 for item in value):
        raise AgentLedgerIntegrityError("Agent iteration vector is invalid")
    return canonical_json(value)


def _sqlite_timestamp(value: object) -> int:
    if type(value) is not int or not 0 <= value <= _SQLITE_INTEGER_MAX:
        raise AgentLedgerIntegrityError("Agent timestamp is outside SQLite integer range")
    return value


def _utc_epoch_ms() -> int:
    return time.time_ns() // 1_000_000


def _validate_invocation_id(value: object) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise AgentLedgerIntegrityError("Agent invocation identity is invalid")
    return value


def _validate_codex_handle(value: object, target_context_json: str) -> str:
    if type(value) is not dict or not value:
        raise AgentLedgerIntegrityError("accepted invoke receipt has no complete handle")
    try:
        handle_json = canonical_json(parse_strict_json(canonical_json(value)))
        context = TargetContext.from_dict(_load_canonical(target_context_json))
    except Exception:
        raise AgentLedgerIntegrityError("accepted invoke receipt handle is invalid") from None
    if context.provider == "codex":
        if (
            set(value) != {"thread_id", "turn_id"}
            or type(value.get("thread_id")) is not str
            or type(value.get("turn_id")) is not str
            or _CODEX_ID.fullmatch(value["thread_id"]) is None
            or _CODEX_ID.fullmatch(value["turn_id"]) is None
        ):
            raise AgentLedgerIntegrityError("accepted Codex handle is incomplete")
    return handle_json


def _canonical_handle(value: object, target_context_json: str) -> str:
    return _validate_codex_handle(value, target_context_json)


def _validate_receipt_for_request(
    request: PreparedProviderRequest, receipt: DispatchReceipt
) -> None:
    if request.operation == "interrupt":
        target_handle = _validate_codex_handle(
            request.handle, canonical_json(request.target_identity)
        )
        if receipt.disposition == "ACCEPTED":
            if receipt.handle != request.handle:
                raise AgentLedgerIntegrityError("accepted interrupt receipt changed its target")
            if receipt.error is not None or receipt.remote_state is None:
                raise AgentLedgerIntegrityError("accepted interrupt receipt is incomplete")
            context = TargetContext.from_dict(request.target_identity)
            if context.provider == "codex":
                _validate_codex_remote_state(
                    receipt.remote_state,
                    receipt.remote_state in _CODEX_TERMINAL_REMOTE_STATES,
                )
        elif receipt.disposition == "REJECTED":
            if (
                receipt.handle != request.handle
                or receipt.remote_state is not None
                or receipt.error is None
                or receipt.error.get("code")
                not in {
                    "dispatch_rejected",
                    "unauthorized",
                    "request_too_large",
                    "unsupported_option",
                    "unsupported_operation",
                    "unsupported_target",
                    "preflight_failed",
                    "invalid_request",
                }
            ):
                raise AgentLedgerIntegrityError("rejected interrupt receipt is invalid")
        elif receipt.disposition == "UNCERTAIN":
            if receipt.handle != request.handle or receipt.remote_state is not None:
                raise AgentLedgerIntegrityError("uncertain interrupt receipt changed its target")
        else:
            raise AgentLedgerIntegrityError("interrupt receipt disposition is invalid")
        if target_handle != canonical_json(request.handle):
            raise AgentLedgerIntegrityError("interrupt request handle is invalid")
        return
    if request.operation != "invoke":
        raise AgentLedgerIntegrityError("dispatch receipt does not match a dispatch operation")
    if receipt.disposition == "ACCEPTED":
        _validate_codex_handle(receipt.handle, canonical_json(request.target_identity))
        if receipt.error is not None:
            raise AgentLedgerIntegrityError("accepted dispatch receipt contains an error")
        context = TargetContext.from_dict(request.target_identity)
        if context.provider == "codex":
            if receipt.remote_state is None:
                raise AgentLedgerIntegrityError("accepted Codex receipt has no remote state")
            _validate_codex_remote_state(
                receipt.remote_state,
                receipt.remote_state in _CODEX_TERMINAL_REMOTE_STATES,
            )
            session = request.session
            if (
                session is not None
                and session.get("mode") == "continue"
                and (
                    receipt.handle is None
                    or receipt.handle.get("thread_id") != session["thread_id"]
                )
            ):
                raise AgentLedgerIntegrityError(
                    "accepted Codex continue receipt changed the requested thread"
                )
    elif receipt.disposition == "REJECTED":
        if receipt.handle is not None or receipt.remote_state is not None:
            raise AgentLedgerIntegrityError("rejected dispatch receipt claims remote execution")
        if receipt.error is None or receipt.error.get("code") not in {
            "dispatch_rejected",
            "unauthorized",
            "request_too_large",
            "unsupported_option",
            "unsupported_operation",
            "unsupported_target",
            "preflight_failed",
            "invalid_request",
        }:
            raise AgentLedgerIntegrityError("rejected dispatch receipt is not a definite refusal")
    elif receipt.disposition == "UNCERTAIN":
        if receipt.handle is not None or receipt.remote_state is not None:
            raise AgentLedgerIntegrityError("uncertain dispatch receipt claims remote execution")
    else:
        raise AgentLedgerIntegrityError("dispatch receipt disposition is invalid")


def _validate_result_for_record(
    record: AgentInvocationRecord,
    result: AgentResult,
    owned: AgentOwnedHandle | None,
) -> None:
    request = record.request
    if (
        result.invocation_id != record.invocation_id
        or result.operation != record.operation
        or result.provider != request.provider
        or result.target != request.target
    ):
        raise AgentLedgerIntegrityError("Agent result identity does not match its request")
    if record.operation in {"observe", "wait"}:
        if record.dispatch_state != "NONE" or owned is not None:
            raise AgentLedgerIntegrityError("read-only Agent invocation has dispatch ownership")
        if result.handle != request.handle:
            raise AgentLedgerIntegrityError("read-only Agent result handle changed")
        if record.operation == "wait" and result.status == "RUNNING":
            raise AgentLedgerIntegrityError("wait Agent result is still running")
        if result.status == "TIMED_OUT":
            if (
                result.error is None
                or result.error.get("code") != "timeout"
                or result.final_text is not None
                or result.data is not None
                or (
                    result.remote_state is not None
                    and result.remote_state not in _CODEX_REMOTE_STATES
                )
            ):
                raise AgentLedgerIntegrityError("timed out read-only Agent result is invalid")
            return
        if result.status == "SUCCEEDED" or (
            result.status == "FAILED" and result.remote_state == "completed"
        ):
            observation_state = "COMPLETED"
        else:
            observation_state = result.status
        if observation_state not in {
            "RUNNING",
            "COMPLETED",
            "FAILED",
            "UNKNOWN",
            "BLOCKED",
            "INTERRUPTED",
        }:
            raise AgentLedgerIntegrityError("read-only Agent result status is invalid")
        try:
            request_model = AgentRequest(
                operation=request.operation,
                provider=request.provider,
                target=request.target,
                target_context=TargetContext.from_dict(request.target_identity),
                session=request.session,
                handle=request.handle,
                options=request.options,
                prompt=request.prompt,
                response=request.response,
                wait=request.wait,
                timeout_seconds=request.timeout_seconds,
            )
            observation = Observation(
                observation_state,
                handle=result.handle,
                remote_state=result.remote_state,
                final_text=result.final_text,
                usage=result.usage,
                error=result.error,
            )
            expected = result_from_observation(record.invocation_id, request_model, observation)
        except Exception:
            raise AgentLedgerIntegrityError(
                "read-only Agent result semantics are invalid"
            ) from None
        if expected.to_json() != result.to_json():
            raise AgentLedgerIntegrityError("read-only Agent result semantics are invalid")
        return
    if record.operation == "interrupt":
        if owned is not None:
            raise AgentLedgerIntegrityError("interrupt operation cannot own a remote handle")
        expected_handle = request.handle
        if (
            result.handle != expected_handle
            or result.remote_state is None
            and (record.dispatch_state == "ACCEPTED")
        ):
            raise AgentLedgerIntegrityError("interrupt result changed its exact target handle")
        if record.dispatch_state == "PREPARED":
            if (
                result.status != "FAILED"
                or result.remote_state is not None
                or result.error is None
                or result.error.get("code")
                not in {
                    "unauthorized",
                    "preflight_failed",
                    "invalid_request",
                    "unsupported_operation",
                    "unsupported_option",
                    "unsupported_target",
                    "request_too_large",
                }
            ):
                raise AgentLedgerIntegrityError("unclaimed interrupt has an invalid result")
            expected = AgentResult(
                invocation_id=record.invocation_id,
                operation="interrupt",
                provider=request.provider,
                target=request.target,
                status="FAILED",
                handle=expected_handle,
                error=result.error,
            )
        elif record.dispatch_state == "MAYBE_SENT":
            expected = AgentResult(
                invocation_id=record.invocation_id,
                operation="interrupt",
                provider=request.provider,
                target=request.target,
                status="UNKNOWN",
                handle=expected_handle,
                error=AgentError("dispatch_uncertain"),
            )
        elif record.dispatch_state == "REJECTED":
            receipt = DispatchReceipt.from_json(cast(str, record.dispatch_receipt_json))
            expected = AgentResult(
                invocation_id=record.invocation_id,
                operation="interrupt",
                provider=request.provider,
                target=request.target,
                status="FAILED",
                handle=expected_handle,
                error=receipt.error,
            )
        elif record.dispatch_state == "ACCEPTED":
            receipt = DispatchReceipt.from_json(cast(str, record.dispatch_receipt_json))
            expected = AgentResult(
                invocation_id=record.invocation_id,
                operation="interrupt",
                provider=request.provider,
                target=request.target,
                status="ACCEPTED",
                handle=receipt.handle,
                remote_state=receipt.remote_state,
            )
        else:
            raise AgentLedgerIntegrityError("interrupt dispatch state is invalid")
        if result.to_json() != expected.to_json():
            raise AgentLedgerIntegrityError("interrupt result disagrees with its receipt")
        return
    if record.dispatch_state == "PREPARED":
        timed_out_before_claim = (
            result.status == "TIMED_OUT"
            and result.error is not None
            and result.error.get("code") == "timeout"
        )
        definite_preflight_failure = (
            result.status == "FAILED"
            and result.error is not None
            and result.error.get("code")
            in {
                "unauthorized",
                "preflight_failed",
                "invalid_request",
                "unsupported_operation",
                "unsupported_option",
                "unsupported_target",
                "request_too_large",
            }
        )
        if not (
            result.handle is None
            and result.remote_state is None
            and (timed_out_before_claim or definite_preflight_failure)
        ):
            raise AgentLedgerIntegrityError("unclaimed invoke has an invalid result")
    elif record.dispatch_state == "MAYBE_SENT":
        if not (
            result.status == "UNKNOWN"
            and result.handle is None
            and result.remote_state is None
            and result.error is not None
            and result.error.get("code") == "dispatch_uncertain"
        ):
            raise AgentLedgerIntegrityError("uncertain invoke has an invalid result")
    elif record.dispatch_state == "REJECTED":
        receipt = DispatchReceipt.from_json(cast(str, record.dispatch_receipt_json))
        if not (
            result.status == "FAILED"
            and result.handle is None
            and result.remote_state is None
            and receipt.error == result.error
        ):
            raise AgentLedgerIntegrityError("rejected invoke result does not match its receipt")
    elif record.dispatch_state == "ACCEPTED":
        if owned is None or result.handle != owned.handle:
            raise AgentLedgerIntegrityError("Agent result handle is not exactly owned")
        if request.wait is False:
            receipt = DispatchReceipt.from_json(cast(str, record.dispatch_receipt_json))
            expected = AgentResult(
                invocation_id=record.invocation_id,
                operation=record.operation,
                provider=request.provider,
                target=request.target,
                status="ACCEPTED",
                handle=receipt.handle,
                remote_state=receipt.remote_state,
            )
            if result.to_json() != expected.to_json():
                raise AgentLedgerIntegrityError("dispatch-only Agent result disagrees with receipt")
            return
        if result.status == "ACCEPTED":
            raise AgentLedgerIntegrityError("waited Agent result is dispatch-only")
        if result.status == "SUCCEEDED" and (
            result.remote_state != "completed"
            or result.final_text is None
            or result.error is not None
        ):
            raise AgentLedgerIntegrityError("successful Agent result lacks completed turn evidence")
        if result.status == "FAILED" and (
            result.remote_state not in {"completed", "failed"}
            or result.error is None
            or result.error.get("code")
            not in {"output_invalid", "output_too_large", "remote_failed"}
        ):
            raise AgentLedgerIntegrityError("failed Agent result lacks matching remote evidence")
        if result.status == "BLOCKED" and (
            result.remote_state not in {"needs_approval", "needs_input"}
            or result.error is None
            or result.error.get("code") not in {"needs_approval", "needs_input"}
        ):
            raise AgentLedgerIntegrityError("blocked Agent result lacks matching remote evidence")
        if result.status == "TIMED_OUT" and (
            result.error is None or result.error.get("code") != "timeout"
        ):
            raise AgentLedgerIntegrityError("timed out Agent result has an invalid error")
        if result.status == "INTERRUPTED" and (
            result.remote_state != "interrupted"
            or result.error is None
            or result.error.get("code") != "remote_interrupted"
        ):
            raise AgentLedgerIntegrityError("interrupted Agent result lacks exact turn evidence")
        if result.status == "UNKNOWN" and (
            result.error is None
            or result.error.get("code") not in {"observation_unavailable", "response_unavailable"}
        ):
            raise AgentLedgerIntegrityError("unknown Agent result has an invalid error")
        if result.status == "RUNNING" and result.remote_state != "in_progress":
            raise AgentLedgerIntegrityError("running Agent result lacks in-progress evidence")
    else:
        raise AgentLedgerIntegrityError("Agent result dispatch state is invalid")


def _validate_invocation_row(
    row: sqlite3.Row,
    binding_json: str,
    target_context_json: str,
) -> AgentInvocationRecord:
    try:
        invocation_id = _validate_invocation_id(row["invocation_id"])
        node_pointer = _validate_node_pointer(row["node_pointer"])
        iteration_json = row["iteration_json"]
        if type(iteration_json) is not str:
            raise ValueError
        iteration = _load_canonical(iteration_json)
        if type(iteration) is not list or any(
            type(item) is not int or item < 0 for item in iteration
        ):
            raise ValueError
        operation = row["operation"]
        if type(operation) is not str or operation not in {
            "invoke",
            "observe",
            "wait",
            "interrupt",
        }:
            raise ValueError
        request_json = row["request_json"]
        request_sha256 = row["request_sha256"]
        if (
            type(request_json) is not str
            or type(request_sha256) is not str
            or _SHA256.fullmatch(request_sha256) is None
            or _sha256(request_json) != request_sha256
        ):
            raise ValueError
        request = PreparedProviderRequest.from_json(request_json)
        if request.to_json() != request_json or request.operation != operation:
            raise ValueError
        target_context = TargetContext.from_dict(request.target_identity)
        if (
            request.provider != target_context.provider
            or request.target != target_context.target
            or canonical_json(target_context.to_dict()) != target_context_json
        ):
            raise ValueError
        expected_identity = {
            "namespace": "codexbridge-agent-v1",
            "binding": _load_canonical(binding_json),
            "node_pointer": node_pointer,
            "iteration_vector": iteration,
        }
        if _sha256(canonical_json(expected_identity)) != invocation_id:
            raise ValueError
        created_ms = _sqlite_timestamp(row["created_ms"])
        deadline_value = row["deadline_ms"]
        deadline_ms = None if deadline_value is None else _sqlite_timestamp(deadline_value)
        expected_deadline = (
            None if request.timeout_seconds is None else created_ms + request.timeout_seconds * 1000
        )
        if expected_deadline is not None and expected_deadline > _SQLITE_INTEGER_MAX:
            raise ValueError
        if deadline_ms != expected_deadline or (
            deadline_ms is not None and deadline_ms < created_ms
        ):
            raise ValueError
        client_request_id = row["client_request_id"]
        dispatch_state = row["dispatch_state"]
        if type(dispatch_state) is not str or dispatch_state not in {
            "NONE",
            "PREPARED",
            "MAYBE_SENT",
            "ACCEPTED",
            "REJECTED",
        }:
            raise ValueError
        if operation in {"invoke", "interrupt"}:
            if client_request_id != f"cba-{invocation_id}" or dispatch_state == "NONE":
                raise ValueError
        elif client_request_id is not None or dispatch_state != "NONE":
            raise ValueError
        if operation == "invoke":
            if type(request.wait) is not bool:
                raise ValueError
        elif operation in {"observe", "wait"}:
            _canonical_handle(request.handle, target_context_json)
            if request.wait is not None:
                raise ValueError
        receipt_json = row["dispatch_receipt_json"]
        receipt_digest = row["dispatch_receipt_sha256"]
        if (receipt_json is None) != (receipt_digest is None):
            raise ValueError
        if receipt_json is not None:
            if type(receipt_json) is not str or type(receipt_digest) is not str:
                raise ValueError
            if _SHA256.fullmatch(receipt_digest) is None or _sha256(receipt_json) != receipt_digest:
                raise ValueError
            _load_canonical(receipt_json)
            receipt = DispatchReceipt.from_json(receipt_json)
            if receipt.to_json() != receipt_json:
                raise ValueError
            _validate_receipt_for_request(request, receipt)
            if (dispatch_state == "ACCEPTED" and receipt.disposition != "ACCEPTED") or (
                dispatch_state == "REJECTED" and receipt.disposition != "REJECTED"
            ):
                raise ValueError
            if dispatch_state not in {"ACCEPTED", "REJECTED"}:
                raise ValueError
        elif dispatch_state in {"ACCEPTED", "REJECTED"}:
            raise ValueError
        result_json = row["result_json"]
        result_digest = row["result_sha256"]
        if (result_json is None) != (result_digest is None):
            raise ValueError
        if result_json is not None:
            if type(result_json) is not str or type(result_digest) is not str:
                raise ValueError
            if _SHA256.fullmatch(result_digest) is None or _sha256(result_json) != result_digest:
                raise ValueError
            _load_canonical(result_json)
            result = AgentResult.from_json(result_json)
            if result.to_json() != result_json:
                raise ValueError
            if (
                result.invocation_id != invocation_id
                or result.operation != operation
                or result.provider != request.provider
                or result.target != request.target
            ):
                raise ValueError
        return AgentInvocationRecord(
            invocation_id=invocation_id,
            node_pointer=node_pointer,
            iteration_json=iteration_json,
            operation=operation,
            request_json=request_json,
            request_sha256=request_sha256,
            client_request_id=client_request_id,
            dispatch_state=dispatch_state,
            dispatch_receipt_json=receipt_json,
            dispatch_receipt_sha256=receipt_digest,
            created_ms=created_ms,
            deadline_ms=deadline_ms,
            result_json=result_json,
            result_sha256=result_digest,
        )
    except (sqlite3.Error, TypeError, ValueError, KeyError, OverflowError, RecursionError):
        raise AgentLedgerIntegrityError("Agent invocation row is invalid") from None


def _validate_row_relationships(
    connection: sqlite3.Connection,
    record: AgentInvocationRecord,
    target_context_json: str,
) -> AgentOwnedHandle | None:
    owned_row = connection.execute(
        "SELECT * FROM owned_handles WHERE invocation_id=?", (record.invocation_id,)
    ).fetchone()
    if record.dispatch_state == "ACCEPTED" and record.operation == "interrupt":
        if owned_row is not None:
            raise AgentLedgerIntegrityError("accepted interrupt unexpectedly owns its target")
        if record.result_json is not None:
            result = AgentResult.from_json(record.result_json)
            _validate_result_for_record(record, result, None)
        return None
    if record.dispatch_state == "ACCEPTED":
        if owned_row is None:
            raise AgentLedgerIntegrityError("accepted invoke has no owned handle")
        owned = _validate_owned_row(owned_row, record, target_context_json)
        receipt = DispatchReceipt.from_json(cast(str, record.dispatch_receipt_json))
        if owned.handle != receipt.handle or owned.provider != record.request.provider:
            raise AgentLedgerIntegrityError("accepted handle and receipt disagree")
        if record.result_json is not None:
            result = AgentResult.from_json(record.result_json)
            _validate_result_for_record(record, result, owned)
        return owned
    if owned_row is not None:
        raise AgentLedgerIntegrityError("unaccepted invoke owns a remote handle")
    if record.result_json is not None:
        _validate_result_for_record(record, AgentResult.from_json(record.result_json), None)
    return None


def _validate_owned_row(
    row: sqlite3.Row,
    record: AgentInvocationRecord,
    target_context_json: str,
) -> AgentOwnedHandle:
    try:
        invocation_id = _validate_invocation_id(row["invocation_id"])
        provider = row["provider"]
        target_digest = row["target_identity_sha256"]
        handle_json = row["handle_json"]
        handle_digest = row["handle_sha256"]
        remote_state = row["remote_state"]
        observed_value = row["observed_ms"]
        terminal_value = row["remote_terminal"]
        if (
            invocation_id != record.invocation_id
            or provider != record.request.provider
            or type(target_digest) is not str
            or target_digest != _sha256(target_context_json)
            or type(handle_json) is not str
            or type(handle_digest) is not str
            or _SHA256.fullmatch(handle_digest) is None
            or _sha256(handle_json) != handle_digest
            or canonical_json(_canonical_handle(_load_canonical(handle_json), target_context_json))
            != canonical_json(handle_json)
            or (remote_state is not None and (type(remote_state) is not str or not remote_state))
            or (observed_value is not None and type(observed_value) is not int)
            or (type(terminal_value) is not int or terminal_value not in {0, 1})
            or (observed_value is not None and observed_value < record.created_ms)
        ):
            raise ValueError
        if provider == "codex":
            if remote_state is None:
                if terminal_value != 0:
                    raise ValueError
            else:
                _validate_codex_remote_state(remote_state, bool(terminal_value))
        return AgentOwnedHandle(
            invocation_id,
            provider,
            target_digest,
            handle_json,
            handle_digest,
            remote_state,
            observed_value,
            bool(terminal_value),
        )
    except (sqlite3.Error, TypeError, ValueError, KeyError, RecursionError):
        raise AgentLedgerIntegrityError("owned Agent handle row is invalid") from None


def _validate_codex_remote_state(remote_state: str, remote_terminal: bool) -> None:
    if (
        type(remote_state) is not str
        or remote_state not in _CODEX_REMOTE_STATES
        or type(remote_terminal) is not bool
        or remote_terminal != (remote_state in _CODEX_TERMINAL_REMOTE_STATES)
    ):
        raise AgentLedgerIntegrityError("Codex remote state is inconsistent")


def _get_invocation_row(connection: sqlite3.Connection, invocation_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM invocations WHERE invocation_id=?", (invocation_id,)
    ).fetchone()
    if row is None:
        raise AgentLedgerIntegrityError("Agent invocation is missing")
    return cast(sqlite3.Row, row)


def _validate_run_binding(
    connection: sqlite3.Connection,
    expected_binding_json: str,
    expected_target_context_json: str,
) -> None:
    try:
        binding, target_context = _read_validated_run_binding(connection)
        if (
            canonical_json(binding) != expected_binding_json
            or canonical_json(target_context) != expected_target_context_json
        ):
            raise ValueError
    except (TypeError, ValueError, KeyError, RecursionError):
        raise AgentLedgerIntegrityError("Agent run binding is invalid or mismatched") from None


def _read_validated_run_binding(
    connection: sqlite3.Connection,
) -> tuple[dict[str, Any], dict[str, Any]]:
    rows = connection.execute("SELECT * FROM run_binding").fetchall()
    if len(rows) != 1:
        raise AgentLedgerIntegrityError("Agent run binding row count is invalid")
    row = rows[0]
    try:
        if (
            type(row["singleton"]) is not int
            or row["singleton"] != 1
            or type(row["binding_json"]) is not str
            or type(row["binding_sha256"]) is not str
            or type(row["target_context_json"]) is not str
            or type(row["target_context_sha256"]) is not str
            or _sha256(row["binding_json"]) != row["binding_sha256"]
            or _sha256(row["target_context_json"]) != row["target_context_sha256"]
        ):
            raise ValueError
        binding = _load_canonical(row["binding_json"])
        target_value = _load_canonical(row["target_context_json"])
        if _canonical_binding(binding) != row["binding_json"]:
            raise ValueError
        target_context = TargetContext.from_dict(target_value).to_dict()
        if canonical_json(target_context) != row["target_context_json"]:
            raise ValueError
        if type(binding) is not dict or type(target_context) is not dict:
            raise ValueError
        return binding, target_context
    except AgentLedgerIntegrityError:
        raise
    except (TypeError, ValueError, KeyError, RecursionError):
        raise AgentLedgerIntegrityError("Agent run binding is invalid") from None


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.execute(_RUN_BINDING_SQL)
    connection.execute(_INVOCATIONS_SQL)
    connection.execute(_OWNED_HANDLES_SQL)


def _check_schema(connection: sqlite3.Connection) -> None:
    try:
        version = connection.execute("PRAGMA user_version").fetchone()
        if version is None or type(version[0]) is not int or version[0] != 1:
            raise ValueError
        objects = {
            (row["type"], row["name"], row["tbl_name"], _compact_sql(row["sql"]))
            for row in connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
            ).fetchall()
        }
        expected = {
            ("table", name, name, _compact_sql(sql)) for name, sql in _EXPECTED_TABLE_SQL.items()
        }
        if objects != expected:
            raise ValueError
        if connection.execute("PRAGMA foreign_keys").fetchone() != (1,):
            connection.execute("PRAGMA foreign_keys=ON")
        for name, _sql in _EXPECTED_TABLE_SQL.items():
            rows = connection.execute(f"PRAGMA table_info({name})").fetchall()
            expected_columns = _expected_columns(name)
            actual_columns = tuple(
                (row["cid"], row["name"], row["type"], row["notnull"], row["dflt_value"], row["pk"])
                for row in rows
            )
            if actual_columns != expected_columns:
                raise ValueError
        foreign_keys = connection.execute("PRAGMA foreign_key_list(owned_handles)").fetchall()
        if len(foreign_keys) != 1 or tuple(
            foreign_keys[0][key] for key in ("table", "from", "to")
        ) != (
            "invocations",
            "invocation_id",
            "invocation_id",
        ):
            raise ValueError
    except (sqlite3.Error, TypeError, ValueError, KeyError):
        raise AgentLedgerIntegrityError("Agent ledger schema is invalid") from None


def _expected_columns(name: str) -> tuple[tuple[object, ...], ...]:
    columns = {
        "run_binding": (
            (0, "singleton", "INTEGER", 0, None, 1),
            (1, "binding_json", "TEXT", 1, None, 0),
            (2, "binding_sha256", "TEXT", 1, None, 0),
            (3, "target_context_json", "TEXT", 1, None, 0),
            (4, "target_context_sha256", "TEXT", 1, None, 0),
        ),
        "invocations": (
            (0, "invocation_id", "TEXT", 0, None, 1),
            (1, "node_pointer", "TEXT", 1, None, 0),
            (2, "iteration_json", "TEXT", 1, None, 0),
            (3, "operation", "TEXT", 1, None, 0),
            (4, "request_json", "TEXT", 1, None, 0),
            (5, "request_sha256", "TEXT", 1, None, 0),
            (6, "client_request_id", "TEXT", 0, None, 0),
            (7, "dispatch_state", "TEXT", 1, None, 0),
            (8, "dispatch_receipt_json", "TEXT", 0, None, 0),
            (9, "dispatch_receipt_sha256", "TEXT", 0, None, 0),
            (10, "created_ms", "INTEGER", 1, None, 0),
            (11, "deadline_ms", "INTEGER", 0, None, 0),
            (12, "result_json", "TEXT", 0, None, 0),
            (13, "result_sha256", "TEXT", 0, None, 0),
        ),
        "owned_handles": (
            (0, "invocation_id", "TEXT", 0, None, 1),
            (1, "provider", "TEXT", 1, None, 0),
            (2, "target_identity_sha256", "TEXT", 1, None, 0),
            (3, "handle_json", "TEXT", 1, None, 0),
            (4, "handle_sha256", "TEXT", 1, None, 0),
            (5, "remote_state", "TEXT", 0, None, 0),
            (6, "observed_ms", "INTEGER", 0, None, 0),
            (7, "remote_terminal", "INTEGER", 1, None, 0),
        ),
    }
    return columns[name]


def _expect_empty_database(connection: sqlite3.Connection) -> None:
    objects = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
    ).fetchone()
    if objects is not None:
        raise AgentLedgerIntegrityError("new Agent ledger file is not empty")
    version = connection.execute("PRAGMA user_version").fetchone()
    if version is None or type(version[0]) is not int or version[0] != 0:
        raise AgentLedgerIntegrityError("new Agent ledger has a schema version")


def _connect_write(database: Path, *, initialize: bool) -> sqlite3.Connection:
    connection: sqlite3.Connection | None = None
    try:
        if _is_symlink_or_junction(database):
            raise ValueError
        resolved = database.resolve(strict=True)
        if not resolved.is_file() or resolved.parent != database.parent.resolve(strict=True):
            raise ValueError
        connection = sqlite3.connect(
            resolved.as_uri() + "?mode=rw",
            uri=True,
            timeout=_BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        if initialize:
            mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
            if mode is None or str(mode[0]).casefold() != "wal":
                raise ValueError
        else:
            mode = connection.execute("PRAGMA journal_mode").fetchone()
            if mode is None or str(mode[0]).casefold() != "wal":
                raise ValueError
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        return connection
    except Exception:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass
        raise AgentLedgerIntegrityError("Agent ledger could not be opened for update") from None


def _database_related_paths(database: Path) -> tuple[Path, Path, Path]:
    return (database, Path(f"{database}-wal"), Path(f"{database}-shm"))


def _remove_created_files(paths: tuple[Path, Path, Path]) -> None:
    for path in paths:
        try:
            if not _is_symlink_or_junction(path) and path.is_file():
                path.unlink(missing_ok=True)
        except OSError:
            pass


def _compact_sql(value: str | None) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", "", value).lower().rstrip(";").replace('"', "").replace("`", "")


def _load_canonical(value: str) -> Any:
    parsed = parse_strict_json(value)
    if canonical_json(parsed) != value:
        raise ValueError("JSON is not canonical")
    return parsed


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="strict")).hexdigest()


def _page_size(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 100:
        raise AgentLedgerIntegrityError("Agent ledger page size is invalid")
    return value


def _rollback(connection: sqlite3.Connection | None) -> None:
    if connection is None:
        return
    try:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
    except sqlite3.Error:
        pass


__all__ = [
    "AgentInvocationLedger",
    "AgentInvocationRecord",
    "AgentLedgerConflictError",
    "AgentLedgerInspection",
    "AgentLedgerIntegrityError",
    "AgentOwnedHandle",
]
