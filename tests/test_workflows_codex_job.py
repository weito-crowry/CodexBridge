from __future__ import annotations

from pathlib import Path

import pytest

from codex_bridge.workflows import interpreter
from codex_bridge.workflows.codex_job_adapter import (
    CodexJobAdapter,
    CodexJobConflictError,
    CodexJobRequestError,
)
from codex_bridge.workflows.interpreter import (
    SAFE_CODEX_JOB_PROMPT,
    OwsError,
)
from codex_bridge.workflows.worker import _persist_workflow_id


def test_preassigned_workflow_id_does_not_rewrite_immutable_manifest(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    original = b'{"workflow_id":"12345678-1234-4234-8234-123456789abc"}\n'
    manifest_path.write_bytes(original)

    _persist_workflow_id(
        tmp_path,
        {"workflow_id": "12345678-1234-4234-8234-123456789abc"},
        "12345678-1234-4234-8234-123456789abc",
    )

    assert manifest_path.read_bytes() == original


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


def test_product_codex_job_preserves_literal_prompt_and_existing_request_controls(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state" / "run-0123456789abcdef0123456789abcdef"
    state_root.mkdir(parents=True)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    adapter = CodexJobAdapter(state_root / "codex_jobs.sqlite")
    prompt = "  ${ . }\nＡＢＣ日本語\t末尾空白  "

    job = adapter.prepare_job(
        "workflow-12345678",
        "do[0].calculate",
        prompt,
        state_root,
        port=18001,
        workspace_root=workspace_root,
        allowed_roots=(str(workspace_root),),
    )

    assert job["request"]["prompt"] == prompt
    assert job["request"]["cwd"] == str(workspace_root.resolve())
    assert job["request"]["sandbox_mode"] == "inherit"
    assert job["request"]["confirm_full_access"] is False


def test_product_codex_job_prompt_change_conflicts_for_same_request_key(tmp_path: Path) -> None:
    state_root = tmp_path / "state" / "run-0123456789abcdef0123456789abcdef"
    state_root.mkdir(parents=True)
    adapter = CodexJobAdapter(state_root / "codex_jobs.sqlite")
    adapter.prepare_job(
        "workflow-12345678",
        "do[0].calculate",
        "日本の首都を一語で答えて",
        state_root,
        port=18001,
    )

    with pytest.raises(CodexJobConflictError, match="request digest mismatch"):
        adapter.prepare_job(
            "workflow-12345678",
            "do[0].calculate",
            "別の有効なliteral prompt",
            state_root,
            port=18001,
        )


@pytest.mark.parametrize(
    "prompt",
    [
        "",
        " \n\t",
        True,
        1,
        [],
        {},
        None,
        "bad\u0000prompt",
        "bad\u0001prompt",
        "bad\u007fprompt",
        "bad\u0085prompt",
        "bad\ud800prompt",
        "x" * (16 * 1024 + 1),
    ],
)
def test_codex_job_prompt_validator_rejects_invalid_literals(prompt: object) -> None:
    validator = getattr(interpreter, "validate_codex_job_prompt", None)
    assert callable(validator)
    with pytest.raises(OwsError) as error:
        validator(prompt)

    if isinstance(prompt, str) and prompt:
        assert prompt not in str(error.value)


def test_codex_job_prompt_validator_accepts_exact_utf8_limit_without_normalizing() -> None:
    prompt = "あ" * 5461 + "x"

    assert len(prompt.encode("utf-8")) == 16 * 1024
    assert interpreter.compute_codex_job_request({"with": {"prompt": prompt}}) == {"prompt": prompt}


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
