from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from codex_bridge.console.project_names import normalize_cwd, read_local_project_names


def test_normalize_cwd_requires_absolute_path_and_normalizes_segments(tmp_path) -> None:
    absolute = str(tmp_path / "workspace" / ".." / "Project")

    assert normalize_cwd(absolute) == os.path.normcase(os.path.normpath(absolute))
    assert normalize_cwd("relative/project") is None
    assert normalize_cwd("") is None
    assert normalize_cwd(42) is None


@pytest.mark.skipif(os.name != "nt", reason="Windows normcase is platform-specific")
def test_normalize_cwd_treats_windows_case_variants_as_the_same_key() -> None:
    assert normalize_cwd(r"C:\Users\Example\Project") == normalize_cwd(r"c:\users\example\project")


def test_reads_exact_normalized_local_project_roots(tmp_path) -> None:
    root = tmp_path / "workspace" / "Project"
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "local-projects": {
                    "project-1": {"name": "My Project", "rootPaths": [str(root)]},
                    "project-2": {"name": "Bad Root", "rootPaths": ["relative/root"]},
                }
            }
        ),
        encoding="utf-8",
    )

    assert read_local_project_names(state_path) == {normalize_cwd(str(root)): "My Project"}


@pytest.mark.parametrize(
    "raw_state",
    [
        "{malformed json",
        json.dumps({"local-projects": []}),
        json.dumps({"local-projects": {"bad": {"name": 12, "rootPaths": []}}}),
        json.dumps({"local-projects": {"bad": {"name": "Name", "rootPaths": "x"}}}),
    ],
)
def test_malformed_or_unexpected_state_returns_empty_mapping(tmp_path, raw_state: str) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(raw_state, encoding="utf-8")

    assert read_local_project_names(state_path) == {}


def test_missing_state_file_returns_empty_mapping(tmp_path) -> None:
    assert read_local_project_names(tmp_path / "missing.json") == {}


def test_read_failure_returns_empty_mapping(tmp_path, monkeypatch) -> None:
    state_path = tmp_path / "state.json"

    def fail_read(*args, **kwargs):
        raise PermissionError("access denied")

    monkeypatch.setattr(Path, "open", fail_read)

    assert read_local_project_names(state_path) == {}


def test_oversized_state_returns_empty_mapping(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_bytes(b" " * (8 * 1024 * 1024 + 1))

    assert read_local_project_names(state_path) == {}
