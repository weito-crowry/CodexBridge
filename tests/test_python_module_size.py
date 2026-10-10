from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_python_module_size.py"
POLICY_PATH = ROOT / "docs" / "development" / "python_module_size_policy.json"

EMPTY_POLICY: dict[str, Any] = {
    "production": {
        "physical_loc_limit": 1500,
        "raw_bytes_limit": 65536,
        "baselines": {},
    },
    "tests": {
        "review_threshold_bytes": 50000,
        "strong_review_threshold_bytes": 100000,
        "baselines": {},
    },
}


@pytest.fixture(scope="module")
def checker() -> ModuleType:
    assert SCRIPT.is_file(), "the Python module size checker must exist"
    spec = importlib.util.spec_from_file_location("python_module_size_checker", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _blob_with_metrics(lines: int, size: int) -> bytes:
    prefix = b"x\n" * (lines - 1)
    assert size > len(prefix)
    return prefix + b"x" * (size - len(prefix))


def _codes(findings: Any) -> set[str]:
    return {finding.code for finding in findings}


def _run_git(repo: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        input=input_bytes,
        capture_output=True,
        check=True,
    )
    return completed.stdout


def _init_repo(repo: Path) -> None:
    repo.mkdir()
    _run_git(repo, "init", "--quiet")
    _run_git(repo, "config", "core.autocrlf", "false")


def test_checker_policy_matches_checker_owned_exceptions(checker: ModuleType) -> None:
    loaded = checker.load_policy(POLICY_PATH)
    assert loaded == checker.POLICY
    production = loaded["production"]["baselines"]
    tests = loaded["tests"]["baselines"]
    assert production["src/codex_bridge/console/main_window.py"]["physical_loc"]["value"] == 2588
    assert production["src/codex_bridge/console/main_window.py"]["raw_bytes"]["value"] == 109534
    assert tests["tests/test_bridge.py"]["raw_bytes"]["value"] == 76784
    assert tests["tests/test_console_main_window.py"]["raw_bytes"]["value"] == 135064
    assert tests["tests/test_console_widgets.py"]["raw_bytes"]["value"] == 60460


def test_production_line_limit_accepts_1500_and_rejects_1501(checker: ModuleType) -> None:
    accepted = checker.evaluate_blobs(
        {"src/example.py": _blob_with_metrics(1500, 3000)}, policy=EMPTY_POLICY
    )
    rejected = checker.evaluate_blobs(
        {"src/example.py": _blob_with_metrics(1501, 3001)}, policy=EMPTY_POLICY
    )
    assert not accepted.errors
    assert "production_physical_loc" in _codes(rejected.errors)


def test_production_byte_limit_accepts_65536_and_rejects_65537(
    checker: ModuleType,
) -> None:
    accepted = checker.evaluate_blobs({"scripts/example.py": b"x" * 65536}, policy=EMPTY_POLICY)
    rejected = checker.evaluate_blobs({"scripts/example.py": b"x" * 65537}, policy=EMPTY_POLICY)
    assert not accepted.errors
    assert "production_raw_bytes" in _codes(rejected.errors)


def test_new_oversized_production_file_fails_without_a_baseline(checker: ModuleType) -> None:
    report = checker.evaluate_blobs({"src/new_module.py": b"x" * 65537}, policy=EMPTY_POLICY)
    assert "production_raw_bytes" in _codes(report.errors)


def test_test_review_threshold_accepts_49999_and_requires_baseline_at_50000(
    checker: ModuleType,
) -> None:
    below = checker.evaluate_blobs({"tests/test_small.py": b"x" * 49999}, policy=EMPTY_POLICY)
    at = checker.evaluate_blobs({"tests/test_large.py": b"x" * 50000}, policy=EMPTY_POLICY)
    assert not below.errors
    assert "test_review_baseline_missing" in _codes(at.errors)


def test_100kb_test_baseline_is_accepted_and_flagged_for_strong_review(
    checker: ModuleType,
) -> None:
    policy = json.loads(json.dumps(EMPTY_POLICY))
    policy["tests"]["baselines"] = {
        "tests/test_large.py": {
            "raw_bytes": {"value": 100000, "reason": "Reviewed legacy test module."}
        }
    }
    report = checker.evaluate_blobs({"tests/test_large.py": b"x" * 100000}, policy=policy)
    assert not report.errors
    assert "test_strong_review" in _codes(report.notices)


def test_exact_production_grandfather_values_are_accepted(checker: ModuleType) -> None:
    path = "src/legacy.py"
    blob = _blob_with_metrics(1501, 65537)
    policy = json.loads(json.dumps(EMPTY_POLICY))
    policy["production"]["baselines"] = {
        path: {
            "physical_loc": {"value": 1501, "reason": "Existing reviewed module."},
            "raw_bytes": {"value": 65537, "reason": "Existing reviewed module."},
        }
    }
    report = checker.evaluate_blobs({path: blob}, policy=policy)
    assert not report.errors


def test_production_baseline_growth_and_hard_limit_recovery_fail(
    checker: ModuleType,
) -> None:
    path = "src/legacy.py"
    policy = json.loads(json.dumps(EMPTY_POLICY))
    policy["production"]["baselines"] = {
        path: {
            "physical_loc": {"value": 1501, "reason": "Existing reviewed module."},
            "raw_bytes": {"value": 65537, "reason": "Existing reviewed module."},
        }
    }
    grown = checker.evaluate_blobs({path: _blob_with_metrics(1501, 65538)}, policy=policy)
    shrunk = checker.evaluate_blobs({path: _blob_with_metrics(1501, 65536)}, policy=policy)
    assert "baseline_growth" in _codes(grown.errors)
    assert "production_baseline_remove" in _codes(shrunk.errors)


def test_production_baseline_recovery_requires_metric_removal(checker: ModuleType) -> None:
    path = "src/legacy.py"
    policy = json.loads(json.dumps(EMPTY_POLICY))
    policy["production"]["baselines"] = {
        path: {
            "physical_loc": {"value": 1501, "reason": "Existing reviewed module."},
            "raw_bytes": {"value": 65537, "reason": "Existing reviewed module."},
        }
    }

    both_at_limit = checker.evaluate_blobs({path: _blob_with_metrics(1500, 65536)}, policy=policy)
    assert [finding.code for finding in both_at_limit.errors] == [
        "production_baseline_remove",
        "production_baseline_remove",
    ]
    assert {finding.message.split()[0] for finding in both_at_limit.errors} == {
        "physical_loc",
        "raw_bytes",
    }

    loc_recovered = checker.evaluate_blobs({path: _blob_with_metrics(1500, 65537)}, policy=policy)
    assert [finding.code for finding in loc_recovered.errors] == ["production_baseline_remove"]
    assert "physical_loc" in loc_recovered.errors[0].message

    bytes_recovered = checker.evaluate_blobs({path: _blob_with_metrics(1501, 65536)}, policy=policy)
    assert [finding.code for finding in bytes_recovered.errors] == ["production_baseline_remove"]
    assert "raw_bytes" in bytes_recovered.errors[0].message

    policy["production"]["baselines"][path] = {
        "raw_bytes": {"value": 65537, "reason": "Still above the byte limit."}
    }
    loc_removed = checker.evaluate_blobs({path: _blob_with_metrics(1500, 65537)}, policy=policy)
    assert not loc_removed.errors

    policy["production"]["baselines"][path] = {
        "physical_loc": {"value": 1501, "reason": "Still above the LOC limit."}
    }
    bytes_removed = checker.evaluate_blobs({path: _blob_with_metrics(1501, 65536)}, policy=policy)
    assert not bytes_removed.errors

    policy["production"]["baselines"] = {}
    both_removed = checker.evaluate_blobs({path: _blob_with_metrics(1500, 65536)}, policy=policy)
    assert not both_removed.errors


@pytest.mark.parametrize(("metric", "value"), (("physical_loc", 1500), ("raw_bytes", 65536)))
def test_production_baseline_approval_must_exceed_hard_limit(
    checker: ModuleType, metric: str, value: int
) -> None:
    policy = json.loads(json.dumps(EMPTY_POLICY))
    policy["production"]["baselines"] = {
        "src/legacy.py": {metric: {"value": value, "reason": "At the hard limit."}}
    }
    with pytest.raises(checker.PolicyError, match="strictly exceed"):
        checker._validate_policy(policy)


def test_test_baseline_growth_and_shrink_ratchet_and_recovery_removal(
    checker: ModuleType,
) -> None:
    path = "tests/test_legacy.py"
    policy = json.loads(json.dumps(EMPTY_POLICY))
    policy["tests"]["baselines"] = {
        path: {"raw_bytes": {"value": 50001, "reason": "Existing reviewed test module."}}
    }
    growth = checker.evaluate_blobs({path: b"x" * 50002}, policy=policy)
    shrink = checker.evaluate_blobs({path: b"x" * 50000}, policy=policy)
    recovered = checker.evaluate_blobs({path: b"x" * 49999}, policy=policy)
    assert "baseline_growth" in _codes(growth.errors)
    assert "baseline_shrink" in _codes(shrink.errors)
    assert "test_baseline_remove" in _codes(recovered.errors)
    policy["tests"]["baselines"][path]["raw_bytes"]["value"] = 50000
    updated = checker.evaluate_blobs({path: b"x" * 50000}, policy=policy)
    assert not updated.errors
    policy["tests"]["baselines"] = {}
    removed = checker.evaluate_blobs({path: b"x" * 49999}, policy=policy)
    assert not removed.errors


def test_missing_policy_path_is_stale(checker: ModuleType) -> None:
    policy = json.loads(json.dumps(EMPTY_POLICY))
    policy["tests"]["baselines"] = {
        "tests/test_missing.py": {"raw_bytes": {"value": 50000, "reason": "Removed test module."}}
    }
    report = checker.evaluate_blobs({}, policy=policy)
    assert "stale_baseline" in _codes(report.errors)


def test_malformed_and_duplicate_key_policy_json_fail_closed(
    checker: ModuleType, tmp_path: Path
) -> None:
    policy_path = tmp_path / "policy.json"
    policy_path.write_text("{", encoding="utf-8")
    with pytest.raises(checker.PolicyError):
        checker.load_policy(policy_path, expected_policy=EMPTY_POLICY)
    policy_path.write_text('{"tests": {}, "tests": {}}', encoding="utf-8")
    with pytest.raises(checker.PolicyError, match="duplicate"):
        checker.load_policy(policy_path, expected_policy=EMPTY_POLICY)


def test_json_only_unknown_exception_path_fails_closed(checker: ModuleType, tmp_path: Path) -> None:
    policy_path = tmp_path / "policy.json"
    policy = json.loads(json.dumps(EMPTY_POLICY))
    policy["tests"]["baselines"] = {
        "tests/test_unreviewed.py": {
            "raw_bytes": {"value": 50000, "reason": "Unilateral exception."}
        }
    }
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    with pytest.raises(checker.PolicyError, match="unknown path"):
        checker.load_policy(policy_path, expected_policy=EMPTY_POLICY)


def test_unstaged_and_untracked_changes_do_not_replace_stage0_blob(
    checker: ModuleType, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    tracked = repo / "src" / "sample.py"
    tracked.parent.mkdir()
    tracked.write_bytes(b"safe\n")
    _run_git(repo, "add", "src/sample.py")
    tracked.write_bytes(b"x" * 70000)
    untracked = repo / "src" / "untracked.py"
    untracked.write_bytes(b"x" * 70000)
    blobs = checker.read_stage0_blobs(repo)
    assert blobs == {"src/sample.py": b"safe\n"}
    _run_git(repo, "add", "src/sample.py")
    blobs = checker.read_stage0_blobs(repo)
    assert blobs["src/sample.py"] == b"x" * 70000


def test_stage0_blob_is_independent_of_worktree_crlf_materialization(
    checker: ModuleType, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    tracked = repo / "src" / "lines.py"
    tracked.parent.mkdir()
    tracked.write_bytes(b"one\r\ntwo\r\n")
    _run_git(repo, "add", "src/lines.py")
    tracked.write_bytes(b"different\n")
    blobs = checker.read_stage0_blobs(repo)
    assert blobs["src/lines.py"] == b"one\r\ntwo\r\n"
    assert len(blobs["src/lines.py"].splitlines()) == 2


def test_unmerged_index_fails_closed(checker: ModuleType, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    object_ids = []
    for content in (b"base", b"ours", b"theirs"):
        object_ids.append(
            _run_git(repo, "hash-object", "-w", "--stdin", input_bytes=content).strip()
        )
    records = b"".join(
        b"100644 " + oid + b" " + str(stage).encode() + b"\tsrc/conflict.py\n"
        for stage, oid in enumerate(object_ids, start=1)
    )
    _run_git(repo, "update-index", "--index-info", input_bytes=records)
    with pytest.raises(checker.IndexReadError, match="unmerged"):
        checker.read_stage0_blobs(repo)


def test_test_support_modules_are_excluded_from_review_gate(checker: ModuleType) -> None:
    report = checker.evaluate_blobs(
        {"tests/fakes.py": b"x" * 150000, "src/small.py": b"ok"}, policy=EMPTY_POLICY
    )
    assert not report.errors
    assert not report.notices


def test_empty_target_index_fails_closed(checker: ModuleType) -> None:
    report = checker.evaluate_blobs({}, policy=EMPTY_POLICY)
    assert "python_targets_missing" in _codes(report.errors)


def test_noncanonical_index_path_fails_closed(checker: ModuleType) -> None:
    listing = b"100644 " + b"a" * 40 + b" 0\tsrc//module.py\0"
    with pytest.raises(checker.IndexReadError, match="non-canonical"):
        checker._parse_index_listing(listing)


def test_malformed_index_listing_fails_closed(checker: ModuleType) -> None:
    with pytest.raises(checker.IndexReadError, match="non-NUL-terminated"):
        checker._parse_index_listing(b"100644 " + b"a" * 40 + b" 0\tsrc/module.py")
    with pytest.raises(checker.IndexReadError, match="malformed index entry"):
        checker._parse_index_listing(b"broken record\0")


def test_stage0_rejects_target_symlink_but_ignores_non_target_symlink(
    checker: ModuleType, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    regular_oid = _run_git(repo, "hash-object", "-w", "--stdin", input_bytes=b"safe\n").strip()
    symlink_oid = _run_git(repo, "hash-object", "-w", "--stdin", input_bytes=b"src/evil.py").strip()
    _run_git(
        repo,
        "update-index",
        "--add",
        "--cacheinfo",
        f"100644,{regular_oid.decode()},src/good.py",
    )
    _run_git(
        repo,
        "update-index",
        "--add",
        "--cacheinfo",
        f"120000,{symlink_oid.decode()},docs/ignored.py",
    )
    assert checker.read_stage0_blobs(repo) == {"src/good.py": b"safe\n"}

    _run_git(
        repo,
        "update-index",
        "--add",
        "--cacheinfo",
        f"120000,{symlink_oid.decode()},src/evil.py",
    )
    with pytest.raises(checker.IndexReadError, match="mode.*src/evil.py|src/evil.py.*mode"):
        checker.read_stage0_blobs(repo)
