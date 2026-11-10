from __future__ import annotations

import sqlite3
from pathlib import Path

LOGICAL_OPERATION = "charge"
EFFECT_NAME = "invoice_charge"
EFFECT_VALUE = 41


class TransientServiceError(Exception):
    """A fake pre-effect service outage used by the bounded retry case."""


class ServiceCapabilityError(Exception):
    """The fake service does not expose the requested capability."""


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path, timeout=5.0)


def initialize_external_database(
    path: Path, *, case_name: str, transient_failures: int = 0
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    supports_lookup = case_name == "idempotent_recovery"
    supports_idempotency = case_name == "idempotent_recovery"
    with _connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE service_capabilities (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                supports_lookup INTEGER NOT NULL,
                supports_idempotency INTEGER NOT NULL
            );
            CREATE TABLE service_totals (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                total_effect_count INTEGER NOT NULL,
                total_effect_value INTEGER NOT NULL
            );
            CREATE TABLE effect_ledger (
                effect_id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_key TEXT NOT NULL,
                workflow_id TEXT NOT NULL,
                logical_operation TEXT NOT NULL,
                effect_name TEXT NOT NULL,
                effect_value INTEGER NOT NULL
            );
            CREATE TABLE service_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation TEXT NOT NULL,
                outcome TEXT NOT NULL,
                request_key TEXT NOT NULL,
                error_kind TEXT
            );
            CREATE TABLE failure_injection (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                transient_failures_remaining INTEGER NOT NULL
            );
            """
        )
        if supports_idempotency:
            connection.execute(
                "CREATE UNIQUE INDEX uq_effect_ledger_request_key ON effect_ledger(request_key)"
            )
        connection.execute(
            "INSERT INTO service_capabilities VALUES (1, ?, ?)",
            (int(supports_lookup), int(supports_idempotency)),
        )
        connection.execute("INSERT INTO service_totals VALUES (1, 0, 0)")
        connection.execute("INSERT INTO failure_injection VALUES (1, ?)", (transient_failures,))


def _capabilities(connection: sqlite3.Connection) -> tuple[bool, bool]:
    row = connection.execute(
        "SELECT supports_lookup, supports_idempotency FROM service_capabilities WHERE singleton = 1"
    ).fetchone()
    if row is None:
        raise RuntimeError("external service capabilities are missing")
    return bool(row[0]), bool(row[1])


def _record_call(
    connection: sqlite3.Connection,
    operation: str,
    outcome: str,
    request_key: str,
    error_kind: str | None = None,
) -> None:
    connection.execute(
        "INSERT INTO service_calls(operation, outcome, request_key, error_kind) "
        "VALUES (?, ?, ?, ?)",
        (operation, outcome, request_key, error_kind),
    )


def _result(row: tuple[object, ...]) -> dict[str, object]:
    return {
        "request_key": str(row[0]),
        "workflow_id": str(row[1]),
        "logical_operation": str(row[2]),
        "effect_name": str(row[3]),
        "effect_value": int(row[4]),
    }


def _find_effect(connection: sqlite3.Connection, request_key: str) -> dict[str, object] | None:
    row = connection.execute(
        "SELECT request_key, workflow_id, logical_operation, effect_name, effect_value "
        "FROM effect_ledger WHERE request_key = ? ORDER BY effect_id LIMIT 1",
        (request_key,),
    ).fetchone()
    return None if row is None else _result(row)


def lookup_effect(path: Path, request_key: str) -> dict[str, object] | None:
    with _connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        supports_lookup, _ = _capabilities(connection)
        if not supports_lookup:
            _record_call(connection, "lookup", "UNAVAILABLE", request_key)
            connection.commit()
            raise ServiceCapabilityError("fake service has no lookup API")
        result = _find_effect(connection, request_key)
        _record_call(connection, "lookup", "HIT" if result is not None else "MISS", request_key)
        connection.commit()
        return result


def _apply_effect(
    connection: sqlite3.Connection,
    *,
    request_key: str,
    workflow_id: str,
    logical_operation: str,
) -> dict[str, object]:
    _, supports_idempotency = _capabilities(connection)
    if supports_idempotency:
        previous = _find_effect(connection, request_key)
        if previous is not None:
            return previous
    connection.execute(
        "INSERT INTO effect_ledger "
        "(request_key, workflow_id, logical_operation, effect_name, effect_value) "
        "VALUES (?, ?, ?, ?, ?)",
        (request_key, workflow_id, logical_operation, EFFECT_NAME, EFFECT_VALUE),
    )
    connection.execute(
        "UPDATE service_totals SET total_effect_count = total_effect_count + 1, "
        "total_effect_value = total_effect_value + ? WHERE singleton = 1",
        (EFFECT_VALUE,),
    )
    result = _find_effect(connection, request_key)
    if result is None:
        raise RuntimeError("external effect was not visible after insert")
    return result


def send_idempotent(
    path: Path,
    *,
    request_key: str,
    workflow_id: str,
    logical_operation: str,
) -> dict[str, object]:
    with _connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _, supports_idempotency = _capabilities(connection)
        if not supports_idempotency:
            _record_call(connection, "send_idempotent", "UNAVAILABLE", request_key)
            connection.commit()
            raise ServiceCapabilityError("fake service does not deduplicate request keys")
        previous = _find_effect(connection, request_key)
        result = previous or _apply_effect(
            connection,
            request_key=request_key,
            workflow_id=workflow_id,
            logical_operation=logical_operation,
        )
        _record_call(
            connection,
            "send_idempotent",
            "RETURNED_EXISTING" if previous is not None else "APPLIED",
            request_key,
        )
        connection.commit()
        return result


def send_transient(
    path: Path,
    *,
    request_key: str,
    workflow_id: str,
    logical_operation: str,
) -> dict[str, object]:
    transient_error = False
    with _connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT transient_failures_remaining FROM failure_injection WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise RuntimeError("transient failure injection row is missing")
        if int(row[0]) > 0:
            connection.execute(
                "UPDATE failure_injection SET transient_failures_remaining = "
                "transient_failures_remaining - 1 WHERE singleton = 1"
            )
            _record_call(
                connection,
                "send_transient",
                "TRANSIENT_ERROR",
                request_key,
                "TransientServiceError",
            )
            transient_error = True
            connection.commit()
        else:
            result = _apply_effect(
                connection,
                request_key=request_key,
                workflow_id=workflow_id,
                logical_operation=logical_operation,
            )
            _record_call(connection, "send_transient", "APPLIED", request_key)
            connection.commit()
            return result
    if transient_error:
        raise TransientServiceError("injected failure before external effect")
    raise RuntimeError("unexpected transient service state")


def send_unqueryable(
    path: Path,
    *,
    request_key: str,
    workflow_id: str,
    logical_operation: str,
) -> dict[str, object]:
    with _connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        supports_lookup, supports_idempotency = _capabilities(connection)
        if supports_lookup or supports_idempotency:
            raise RuntimeError("unqueryable fake service was given a safety capability")
        connection.execute(
            "INSERT INTO effect_ledger "
            "(request_key, workflow_id, logical_operation, effect_name, effect_value) "
            "VALUES (?, ?, ?, ?, ?)",
            (request_key, workflow_id, logical_operation, EFFECT_NAME, EFFECT_VALUE),
        )
        connection.execute(
            "UPDATE service_totals SET total_effect_count = total_effect_count + 1, "
            "total_effect_value = total_effect_value + ? WHERE singleton = 1",
            (EFFECT_VALUE,),
        )
        _record_call(connection, "send_unqueryable", "APPLIED", request_key)
        result = _find_effect(connection, request_key)
        if result is None:
            raise RuntimeError("unqueryable external effect was not visible after insert")
        connection.commit()
        return result


def initialize_adapter_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE adapter_state (
                request_key TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                status TEXT NOT NULL,
                receipt TEXT,
                reason TEXT
            );
            """
        )


def read_adapter_state(path: Path, request_key: str) -> dict[str, str | None] | None:
    with _connect(path) as connection:
        row = connection.execute(
            "SELECT request_key, workflow_id, status, receipt, reason FROM adapter_state "
            "WHERE request_key = ?",
            (request_key,),
        ).fetchone()
    if row is None:
        return None
    return {
        "request_key": str(row[0]),
        "workflow_id": str(row[1]),
        "status": str(row[2]),
        "receipt": None if row[3] is None else str(row[3]),
        "reason": None if row[4] is None else str(row[4]),
    }


def record_dispatch_intent(path: Path, *, request_key: str, workflow_id: str) -> None:
    with _connect(path) as connection:
        connection.execute(
            "INSERT INTO adapter_state(request_key, workflow_id, status, receipt, reason) "
            "VALUES (?, ?, 'DISPATCH_INTENT', NULL, NULL)",
            (request_key, workflow_id),
        )


def record_receipt(path: Path, *, request_key: str, receipt: str) -> None:
    with _connect(path) as connection:
        connection.execute(
            "UPDATE adapter_state SET status = 'RECEIPT', receipt = ?, reason = NULL "
            "WHERE request_key = ? AND status = 'DISPATCH_INTENT' AND receipt IS NULL",
            (receipt, request_key),
        )
        if connection.total_changes != 1:
            raise RuntimeError("dispatch intent was not available for its local receipt")


def record_unknown(path: Path, *, request_key: str, reason: str) -> None:
    with _connect(path) as connection:
        connection.execute(
            "UPDATE adapter_state SET status = 'UNKNOWN', reason = ? "
            "WHERE request_key = ? AND status = 'DISPATCH_INTENT' AND receipt IS NULL",
            (reason, request_key),
        )
        if connection.total_changes != 1:
            state = read_adapter_state(path, request_key)
            if state is None or state["status"] != "UNKNOWN":
                raise RuntimeError("dispatch intent could not be persisted as UNKNOWN")
