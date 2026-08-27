from __future__ import annotations

import os

import pytest

from codex_bridge.paths import AllowedPathPolicy, PathPolicyError


def test_relative_cwd_is_rejected(tmp_path: os.PathLike[str]) -> None:
    with pytest.raises(PathPolicyError):
        AllowedPathPolicy((os.fspath(tmp_path),)).validate_cwd(".")


def test_sibling_prefix_is_rejected(tmp_path: os.PathLike[str]) -> None:
    allowed = tmp_path / "repo"
    sibling = tmp_path / "repo-other"
    allowed.mkdir()
    sibling.mkdir()

    with pytest.raises(PathPolicyError):
        AllowedPathPolicy((os.fspath(allowed),)).validate_cwd(os.fspath(sibling))


def test_cwd_is_returned_as_canonical_path(tmp_path: os.PathLike[str]) -> None:
    allowed = tmp_path / "repo"
    child = allowed / "src"
    child.mkdir(parents=True)

    result = AllowedPathPolicy((os.fspath(allowed),)).validate_cwd(os.fspath(child))

    assert os.path.normcase(result) == os.path.normcase(os.path.realpath(child))


def test_symlink_escape_is_rejected(tmp_path: os.PathLike[str]) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    link = allowed / "linked"
    allowed.mkdir()
    outside.mkdir()
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlink unavailable: {exc}")

    with pytest.raises(PathPolicyError):
        AllowedPathPolicy((os.fspath(allowed),)).validate_cwd(os.fspath(link))
