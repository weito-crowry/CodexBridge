from __future__ import annotations

from pathlib import Path

import pytest

from codex_bridge.workflows.codex_job_adapter import (
    CodexJobAdapter,
    CodexJobRequestError,
)
from codex_bridge.workflows.interpreter import SAFE_CODEX_JOB_PROMPT


def test_product_codex_job_uses_explicit_allowed_workspace_not_state_root(tmp_path: Path) -> None:
    state_root = tmp_path / "state" / "run-0123456789abcdef0123456789abcdef"
    state_root.mkdir(parents=True)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    adapter = CodexJobAdapter(state_root / "codex_jobs.sqlite")

    job = adapter.prepare_job(
        "workflow-12345678",
        "do[0].calculate",
        SAFE_CODEX_JOB_PROMPT,
        state_root,
        port=18001,
        workspace_root=workspace_root,
        allowed_roots=(str(workspace_root),),
    )

    assert job["request"]["cwd"] == str(workspace_root.resolve())
    assert job["agent_cwd"] == str(workspace_root.resolve())
    assert not (state_root / "codex-job-cwd").exists()


def test_product_codex_job_rejects_workspace_outside_allowed_roots(tmp_path: Path) -> None:
    state_root = tmp_path / "state" / "run-0123456789abcdef0123456789abcdef"
    state_root.mkdir(parents=True)
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    adapter = CodexJobAdapter(state_root / "codex_jobs.sqlite")

    with pytest.raises(CodexJobRequestError, match="allowed"):
        adapter.prepare_job(
            "workflow-12345678",
            "do[0].calculate",
            SAFE_CODEX_JOB_PROMPT,
            state_root,
            port=18001,
            workspace_root=outside,
            allowed_roots=(str(allowed),),
        )


def test_product_codex_job_rejects_parent_traversal(tmp_path: Path) -> None:
    state_root = tmp_path / "state" / "run-0123456789abcdef0123456789abcdef"
    state_root.mkdir(parents=True)
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    adapter = CodexJobAdapter(state_root / "codex_jobs.sqlite")

    with pytest.raises(CodexJobRequestError, match="allowed"):
        adapter.prepare_job(
            "workflow-12345678",
            "do[0].calculate",
            SAFE_CODEX_JOB_PROMPT,
            state_root,
            port=18001,
            workspace_root=allowed / ".." / outside.name,
            allowed_roots=(str(allowed),),
        )


def test_product_codex_job_rejects_symlink_escape(tmp_path: Path) -> None:
    state_root = tmp_path / "state" / "run-0123456789abcdef0123456789abcdef"
    state_root.mkdir(parents=True)
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    symlink = allowed / "linked-outside"
    try:
        symlink.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"directory symlink unavailable: {error}")
    adapter = CodexJobAdapter(state_root / "codex_jobs.sqlite")

    with pytest.raises(CodexJobRequestError, match="allowed"):
        adapter.prepare_job(
            "workflow-12345678",
            "do[0].calculate",
            SAFE_CODEX_JOB_PROMPT,
            state_root,
            port=18001,
            workspace_root=symlink,
            allowed_roots=(str(allowed),),
        )


def test_shutdown_ownership_comes_from_exact_ledger_ids_and_unknown_post_is_incomplete(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state" / "run-0123456789abcdef0123456789abcdef"
    state_root.mkdir(parents=True)
    adapter = CodexJobAdapter(state_root / "codex_jobs.sqlite")
    prepared = adapter.prepare_job(
        "workflow-12345678",
        "do[0].calculate",
        SAFE_CODEX_JOB_PROMPT,
        state_root,
        port=18001,
    )
    assert adapter.claim_post(prepared["job_id"])
    adapter.mark_accepted(prepared["job_id"], "thread-12345678", "turn-12345678", "in_progress")

    turns, complete = CodexJobAdapter.read_shutdown_ownership(state_root / "codex_jobs.sqlite")

    assert turns == {("thread-12345678", "turn-12345678")}
    assert complete


def test_shutdown_ownership_does_not_guess_identity_for_maybe_sent(tmp_path: Path) -> None:
    state_root = tmp_path / "state" / "run-0123456789abcdef0123456789abcdef"
    state_root.mkdir(parents=True)
    adapter = CodexJobAdapter(state_root / "codex_jobs.sqlite")
    prepared = adapter.prepare_job(
        "workflow-12345678",
        "do[0].calculate",
        SAFE_CODEX_JOB_PROMPT,
        state_root,
        port=18001,
    )
    assert adapter.claim_post(prepared["job_id"])

    turns, complete = CodexJobAdapter.read_shutdown_ownership(state_root / "codex_jobs.sqlite")

    assert turns == set()
    assert not complete


def test_shutdown_ownership_is_incomplete_when_ledger_is_missing(tmp_path: Path) -> None:
    turns, complete = CodexJobAdapter.read_shutdown_ownership(tmp_path / "missing.sqlite")

    assert turns == set()
    assert not complete
