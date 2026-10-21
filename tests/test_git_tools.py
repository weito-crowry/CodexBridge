from __future__ import annotations

import os
import subprocess
from pathlib import Path

from codex_bridge.git_tools import git_diff, git_status


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def _repo(root: Path) -> Path:
    repo = root / "repo with 日本語"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "tracked file.txt").write_text("original\n", encoding="utf-8")
    _git(repo, "add", "--", "tracked file.txt")
    _git(repo, "commit", "--quiet", "-m", "initial")
    return repo


def test_git_status_reports_branch_and_untracked_without_reading_outside_root(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    (repo / "new file 日本語.txt").write_text("new\n", encoding="utf-8")
    policy_roots = (os.fspath(tmp_path),)

    result = git_status(os.fspath(repo), allowed_roots=policy_roots)

    assert result["ok"] is True
    assert result["branch"]
    assert result["untracked"] == ["new file 日本語.txt"]
    assert result["staged"] == []
    assert result["unstaged"] == []


def test_git_diff_reports_tracked_unstaged_patch_and_excludes_untracked(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    (repo / "tracked file.txt").write_text("changed\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("not in patch\n", encoding="utf-8")

    result = git_diff(os.fspath(repo), allowed_roots=(os.fspath(tmp_path),))

    assert result["ok"] is True
    assert result["has_changes"] is True
    assert "changed" in result["diff"]
    assert "untracked.txt" not in result["diff"]
    assert result["untracked_included"] is False


def test_git_diff_staged_toggle_and_repo_relative_pathspec(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tracked file.txt").write_text("staged change\n", encoding="utf-8")
    _git(repo, "add", "--", "tracked file.txt")

    result = git_diff(
        os.fspath(repo),
        staged=True,
        path="tracked file.txt",
        allowed_roots=(os.fspath(tmp_path),),
    )

    assert result["ok"] is True
    assert result["has_changes"] is True
    assert "staged change" in result["diff"]


def test_git_status_rejects_repo_outside_allowed_roots(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    repo = _repo(outside)

    result = git_status(os.fspath(repo), allowed_roots=(os.fspath(allowed),))

    assert result["ok"] is False
    assert result["error"] == "repo_outside_allowed_root"


def test_git_diff_rejects_pathspec_traversal(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    result = git_diff(os.fspath(repo), path="..\\outside.txt", allowed_roots=(os.fspath(tmp_path),))

    assert result == {"ok": False, "error": "invalid_pathspec"}


def test_git_diff_empty_patch_is_distinct_from_git_error(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    result = git_diff(os.fspath(repo), allowed_roots=(os.fspath(tmp_path),))

    assert result["ok"] is True
    assert result["has_changes"] is False
    assert result["diff"] == ""


def test_git_status_rejects_non_repository(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()

    result = git_status(os.fspath(root), allowed_roots=(os.fspath(tmp_path),))

    assert result == {"ok": False, "error": "not_a_git_repository"}
