"""Read-only selection for the prepared workflow definition repository schema."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .definitions import (
    _DATABASE_NAME,
    DefinitionPage,
    DefinitionRepositoryUnavailable,
    DefinitionRevision,
    WorkflowDefinitionRepository,
    _is_symlink_or_junction,
)
from .strict_definition_repository import StrictWorkflowDefinitionRepository
from .strict_definitions import StrictWorkflowDefinitionStore

_UNAVAILABLE = "workflow definition storage is unavailable"


def select_workflow_definition_repository(
    state_root: Path,
) -> WorkflowDefinitionRepository:
    """Select only a verified, already-prepared v1 or v2 database.

    The probe is read-only. Missing storage retains the legacy repository's
    existing lazy behavior. Unknown, unsafe, or malformed storage is surfaced
    through an unavailable repository so it cannot be written as v1 by fallback.
    """

    legacy = WorkflowDefinitionRepository(state_root)
    try:
        root = legacy._resolve_state_root(create=False)
        if root is None:
            return legacy
        database = root / _DATABASE_NAME
        if _is_symlink_or_junction(database):
            raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
        if not database.exists():
            return legacy
        if not database.is_file():
            raise DefinitionRepositoryUnavailable(_UNAVAILABLE)

        version = _read_user_version(database)
        if version == 1:
            # Reuse the legacy repository's complete schema verifier.
            legacy.list(limit=1)
            return legacy
        if version == 2:
            store = StrictWorkflowDefinitionStore(root)
            # The strict read path checks the v2 schema and its persisted rows.
            store.list(limit=1)
            return StrictWorkflowDefinitionRepository(root, store=store)
        raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
    except Exception:
        return _UnavailableWorkflowDefinitionRepository(state_root)


def _read_user_version(database: Path) -> int:
    connection: sqlite3.Connection | None = None
    try:
        uri = f"{database.resolve(strict=True).as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=1.0)
        row = connection.execute("PRAGMA user_version").fetchone()
        if row is None or type(row[0]) is not int:
            raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
        return row[0]
    except DefinitionRepositoryUnavailable:
        raise
    except Exception:
        raise DefinitionRepositoryUnavailable(_UNAVAILABLE) from None
    finally:
        if connection is not None:
            connection.close()


class _UnavailableWorkflowDefinitionRepository(WorkflowDefinitionRepository):
    """A non-writing repository surface for unknown or invalid stored schemas."""

    def list(self, limit: int = 50, cursor: str | None = None) -> DefinitionPage:
        raise DefinitionRepositoryUnavailable(_UNAVAILABLE)

    def get(self, definition_id: str, revision: int | None = None) -> DefinitionRevision:
        raise DefinitionRepositoryUnavailable(_UNAVAILABLE)

    def revision_for_etag(self, definition_id: str, etag: str) -> int | None:
        raise DefinitionRepositoryUnavailable(_UNAVAILABLE)

    def create(
        self,
        *,
        mutation_request_id: str,
        source_yaml: str,
        max_live_codex_jobs: int,
    ) -> DefinitionRevision:
        raise DefinitionRepositoryUnavailable(_UNAVAILABLE)

    def update(
        self,
        *,
        definition_id: str,
        expected_revision: int,
        mutation_request_id: str,
        source_yaml: str,
        max_live_codex_jobs: int,
    ) -> DefinitionRevision:
        raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
