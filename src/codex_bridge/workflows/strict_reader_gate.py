"""Read-only run classification and reserved verification for reader entry points.

Classification checks format and request binding only. The shared strict
reader helper also verifies a reserved v2 snapshot, but does not authorize
runtime execution.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from enum import Enum
from pathlib import Path
from typing import Any, NoReturn

from .strict_manifest import (
    StrictWorkflowManifestV2,
    StrictWorkflowSnapshotVerification,
    StrictWorkflowSnapshotVerifier,
    parse_strict_manifest_v2,
)
from .strict_requests import StrictWorkflowRequestStore

_REASON_CODE = "NEEDS_RECONCILIATION"
_RUN_ID = re.compile(r"run-[0-9a-f]{32}\Z", re.ASCII)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_LEGACY_TABLES = frozenset({"workflow_requests", "workflow_request_sources"})
_LEGACY_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "workflow_requests": (
        ("client_request_id", "TEXT"),
        ("workflow_id", "TEXT"),
        ("run_id", "TEXT"),
        ("request_digest", "TEXT"),
        ("state", "TEXT"),
        ("created_at", "TEXT"),
        ("updated_at", "TEXT"),
    ),
    "workflow_request_sources": (
        ("run_id", "TEXT"),
        ("definition_id", "TEXT"),
        ("revision", "INTEGER"),
        ("source_sha256", "TEXT"),
    ),
}


class RunFormatVersion(Enum):
    """Known on-disk run formats understood by the service entry gate."""

    LEGACY_V1 = "legacy-v1"
    STRICT_V2 = "strict-v2"


class StrictReaderGateError(RuntimeError):
    """A safe, value-free signal that a run needs reconciliation."""

    reason_code = _REASON_CODE

    def __init__(self) -> None:
        super().__init__(_REASON_CODE)


def classify_readonly(
    state_root: Path,
    run_id: str,
    manifest: bytes | dict[str, Any],
    *,
    expected_workflow_id: str,
    expected_request_digest: str,
) -> RunFormatVersion:
    """Classify a manifest only after matching it to the read-only request DB.

    Legacy v1 is the sole format represented by an absent manifest version.
    The v1 request schema is checked through ``StrictWorkflowRequestStore``;
    the legacy v0 check reads only its version and structural SQLite metadata
    so its historical request digest is never recalculated.
    """
    try:
        if (
            type(run_id) is not str
            or _RUN_ID.fullmatch(run_id) is None
            or type(expected_workflow_id) is not str
            or type(expected_request_digest) is not str
            or _SHA256.fullmatch(expected_request_digest) is None
        ):
            _fail()

        manifest_value, manifest_bytes = _decode_manifest(manifest)
        has_version = "run_format_version" in manifest_value
        parsed_strict: StrictWorkflowManifestV2 | None = None
        if has_version:
            version = manifest_value["run_format_version"]
            if type(version) is not int or version != 2:
                _fail()
            parsed_strict = parse_strict_manifest_v2(manifest_bytes)

        root = _resolve_state_root(state_root)
        database = _request_database(root)
        with closing(_open_readonly(database)) as connection:
            user_version = _user_version(connection)
            if user_version == 0:
                _check_legacy_v0_metadata(connection)
                request = _read_request_identity(connection, run_id)
                if parsed_strict is not None:
                    _fail()
                _check_legacy_identity(
                    manifest_value,
                    request,
                    run_id=run_id,
                    workflow_id=expected_workflow_id,
                    request_digest=expected_request_digest,
                )
                return RunFormatVersion.LEGACY_V1

            if user_version != 1:
                _fail()

            store = StrictWorkflowRequestStore(root)
            info = store.inspect()
            if info.user_version != 1:
                _fail()

            request = _read_request_identity(connection, run_id)
            binding_row = connection.execute(
                "SELECT 1 FROM workflow_request_contract_bindings WHERE run_id = ?",
                (run_id,),
            ).fetchone()

            if binding_row is None:
                if parsed_strict is not None:
                    _fail()
                _check_legacy_identity(
                    manifest_value,
                    request,
                    run_id=run_id,
                    workflow_id=expected_workflow_id,
                    request_digest=expected_request_digest,
                )
                return RunFormatVersion.LEGACY_V1

            if parsed_strict is None:
                _fail()
            reservation = store.read_binding(run_id)
            if reservation is None:
                _fail()
            _check_strict_identity(
                parsed_strict,
                request,
                reservation,
                run_id=run_id,
                workflow_id=expected_workflow_id,
                request_digest=expected_request_digest,
            )
            return RunFormatVersion.STRICT_V2
    except StrictReaderGateError:
        raise
    except Exception:
        _fail()


def verify_strict_reserved_readonly(
    state_root: Path,
    run_id: str,
    manifest_bytes: bytes,
    *,
    expected_workflow_id: str,
    expected_request_digest: str,
    allowed_roots: tuple[str, ...],
) -> StrictWorkflowSnapshotVerification | None:
    """Verify a strict reserved snapshot, returning ``None`` for valid legacy runs.

    The supplied manifest bytes are the caller's original read. The reserved
    verifier independently reads the snapshot again; its parsed manifest must
    match that original value before the verification result is trusted.
    """
    try:
        if type(manifest_bytes) is not bytes:
            _fail()
        run_format = classify_readonly(
            state_root,
            run_id,
            manifest_bytes,
            expected_workflow_id=expected_workflow_id,
            expected_request_digest=expected_request_digest,
        )
        if run_format is RunFormatVersion.LEGACY_V1:
            return None
        if run_format is not RunFormatVersion.STRICT_V2:
            _fail()

        original_manifest = parse_strict_manifest_v2(manifest_bytes)
        verification = StrictWorkflowSnapshotVerifier(state_root, allowed_roots).verify_reserved(
            run_id
        )
        if (
            verification.reservation_verified is not True
            or verification.manifest != original_manifest
            or verification.manifest.run_id != run_id
            or verification.manifest.workflow_id != expected_workflow_id
            or verification.manifest.request_digest != expected_request_digest
        ):
            _fail()
        return verification
    except Exception:
        raise StrictReaderGateError() from None


def _decode_manifest(manifest: bytes | dict[str, Any]) -> tuple[dict[str, Any], bytes]:
    if type(manifest) is bytes:
        payload = manifest
        value = json.loads(
            payload, parse_constant=_reject_constant, object_pairs_hook=_unique_object
        )
    elif type(manifest) is dict:
        value = manifest
        try:
            payload = json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            _fail()
    else:
        _fail()

    if type(value) is not dict or any(type(key) is not str for key in value):
        _fail()
    return value, payload


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail()
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    _fail()


def _resolve_state_root(state_root: Path) -> Path:
    candidate = Path(state_root).expanduser()
    if candidate.is_symlink() or not candidate.is_dir():
        _fail()
    root = candidate.resolve(strict=True)
    if candidate.resolve(strict=False) != root:
        _fail()
    return root


def _request_database(root: Path) -> Path:
    database = root / "requests.sqlite"
    if database.is_symlink() or not database.is_file():
        _fail()
    resolved = database.resolve(strict=True)
    if resolved.parent != root or resolved.name != "requests.sqlite":
        _fail()
    return resolved


def _open_readonly(database: Path) -> sqlite3.Connection:
    uri = database.as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=2.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA busy_timeout=2000")
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
    except Exception:
        connection.close()
        raise
    return connection


def _user_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("PRAGMA user_version").fetchone()
    if row is None or type(row[0]) is not int:
        _fail()
    return row[0]


def _check_legacy_v0_metadata(connection: sqlite3.Connection) -> None:
    tables = frozenset(
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    )
    if tables != _LEGACY_TABLES:
        _fail()
    if (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='workflow_request_contract_bindings' LIMIT 1"
        ).fetchone()
        is not None
    ):
        _fail()
    for table, expected_columns in _LEGACY_COLUMNS.items():
        actual = tuple(
            (str(row["name"]), str(row["type"]).upper())
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        )
        if actual != expected_columns:
            _fail()


def _read_request_identity(connection: sqlite3.Connection, run_id: str) -> tuple[str, str, str]:
    row = connection.execute(
        "SELECT workflow_id, run_id, request_digest FROM workflow_requests WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if (
        row is None
        or type(row["workflow_id"]) is not str
        or type(row["run_id"]) is not str
        or type(row["request_digest"]) is not str
    ):
        _fail()
    return row["workflow_id"], row["run_id"], row["request_digest"]


def _check_legacy_identity(
    manifest: dict[str, Any],
    request: tuple[str, str, str],
    *,
    run_id: str,
    workflow_id: str,
    request_digest: str,
) -> None:
    if (
        request != (workflow_id, run_id, request_digest)
        or manifest.get("workflow_id") != workflow_id
        or manifest.get("run_id") != run_id
        or manifest.get("request_digest") != request_digest
    ):
        _fail()


def _check_strict_identity(
    manifest: StrictWorkflowManifestV2,
    request: tuple[str, str, str],
    reservation: Any,
    *,
    run_id: str,
    workflow_id: str,
    request_digest: str,
) -> None:
    if (
        request != (workflow_id, run_id, request_digest)
        or manifest.workflow_id != workflow_id
        or manifest.run_id != run_id
        or manifest.request_digest != request_digest
        or reservation.workflow_id != workflow_id
        or reservation.run_id != run_id
        or reservation.request_digest != request_digest
        or reservation.run_format_version != 2
        or manifest.definition_id != reservation.definition_id
        or manifest.definition_revision != reservation.definition_revision
        or manifest.definition_sha256 != reservation.source_sha256
        or manifest.source_sha256 != reservation.source_sha256
        or manifest.contract_version != reservation.contract_version
        or manifest.contract_sha256 != reservation.contract_sha256
        or manifest.binding_digest != reservation.binding_digest
        or manifest.normalized_input_sha256 != reservation.normalized_input_sha256
    ):
        _fail()


def _fail() -> NoReturn:
    raise StrictReaderGateError()
