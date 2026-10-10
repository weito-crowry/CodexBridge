from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

POLICY: dict[str, Any] = {
    "production": {
        "physical_loc_limit": 1500,
        "raw_bytes_limit": 65536,
        "baselines": {
            "src/codex_bridge/console/main_window.py": {
                "physical_loc": {
                    "value": 2588,
                    "reason": (
                        "Existing console file exceeds the LOC ceiling; Issue #45 defers splitting."
                    ),
                },
                "raw_bytes": {
                    "value": 109534,
                    "reason": (
                        "Existing console file exceeds the byte ceiling; "
                        "Issue #45 defers splitting."
                    ),
                },
            }
        },
    },
    "tests": {
        "review_threshold_bytes": 50000,
        "strong_review_threshold_bytes": 100000,
        "baselines": {
            "tests/test_bridge.py": {
                "raw_bytes": {
                    "value": 76784,
                    "reason": (
                        "Existing bridge regression tests stay intact; splitting is out of scope."
                    ),
                }
            },
            "tests/test_console_main_window.py": {
                "raw_bytes": {
                    "value": 135064,
                    "reason": (
                        "Existing main-window tests stay intact; >100 KB needs reviewer attention."
                    ),
                }
            },
            "tests/test_console_widgets.py": {
                "raw_bytes": {
                    "value": 60460,
                    "reason": (
                        "Existing console-widget tests stay intact; splitting is out of scope."
                    ),
                }
            },
        },
    },
}

POLICY_RELATIVE_PATH = Path("docs/development/python_module_size_policy.json")


class PolicyError(ValueError):
    """The reviewable policy file is invalid or disagrees with checker-owned policy."""


class IndexReadError(RuntimeError):
    """The Git index or one of its stage-0 blobs could not be read safely."""


@dataclass(frozen=True)
class Finding:
    code: str
    path: str
    message: str


@dataclass(frozen=True)
class CheckReport:
    errors: tuple[Finding, ...]
    notices: tuple[Finding, ...]
    files_checked: int


def _as_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise PolicyError(f"{label} must be a JSON object with string keys")
    return value


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PolicyError(f"{label} must be a positive integer")
    return value


def _classify_path(path: str) -> str | None:
    if path.startswith(("src/", "scripts/")) and path.endswith(".py"):
        return "production"
    basename = path.rsplit("/", maxsplit=1)[-1]
    if path.startswith("tests/") and basename.startswith("test_") and path.endswith(".py"):
        return "tests"
    return None


def _validate_repo_path(path: object) -> str:
    if not isinstance(path, str) or not path or "\\" in path or path.startswith("/"):
        raise IndexReadError("Git index contains an invalid repository path")
    parts = path.split("/")
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise IndexReadError(f"Git index contains a non-canonical path: {path!r}")
    return path


def _validate_policy(policy: object) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    top = _as_mapping(policy, "policy")
    if set(top) != {"production", "tests"}:
        raise PolicyError("policy must contain only production and tests sections")
    production = _as_mapping(top["production"], "production policy")
    tests = _as_mapping(top["tests"], "test policy")
    if set(production) != {"physical_loc_limit", "raw_bytes_limit", "baselines"}:
        raise PolicyError("production policy has unknown or missing fields")
    if set(tests) != {"review_threshold_bytes", "strong_review_threshold_bytes", "baselines"}:
        raise PolicyError("test policy has unknown or missing fields")
    _positive_int(production["physical_loc_limit"], "physical_loc_limit")
    _positive_int(production["raw_bytes_limit"], "raw_bytes_limit")
    review_limit = _positive_int(tests["review_threshold_bytes"], "review_threshold_bytes")
    strong_limit = _positive_int(
        tests["strong_review_threshold_bytes"], "strong_review_threshold_bytes"
    )
    if strong_limit < review_limit:
        raise PolicyError("strong review threshold must not be below review threshold")

    for section, expected_kind, metric_names in (
        (production, "production", {"physical_loc", "raw_bytes"}),
        (tests, "tests", {"raw_bytes"}),
    ):
        baselines = _as_mapping(section["baselines"], f"{expected_kind} baselines")
        for path, entry_value in baselines.items():
            if _classify_path(path) != expected_kind:
                raise PolicyError(f"unknown path in {expected_kind} baselines: {path}")
            entry = _as_mapping(entry_value, f"baseline for {path}")
            if not entry or not set(entry).issubset(metric_names):
                raise PolicyError(f"unknown or missing metric in baseline for {path}")
            for metric, record_value in entry.items():
                record = _as_mapping(record_value, f"{path} {metric} baseline")
                if set(record) != {"value", "reason"}:
                    raise PolicyError(f"{path} {metric} baseline needs value and reason")
                _positive_int(record["value"], f"{path} {metric} baseline value")
                if not isinstance(record["reason"], str) or not record["reason"].strip():
                    raise PolicyError(f"{path} {metric} baseline needs a substantive reason")
    return production, tests


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PolicyError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _baseline_paths(policy: Mapping[str, Any], section_name: str) -> Mapping[str, Any]:
    section = _as_mapping(policy[section_name], f"{section_name} policy")
    return _as_mapping(section["baselines"], f"{section_name} baselines")


def load_policy(path: Path, *, expected_policy: Mapping[str, Any] = POLICY) -> dict[str, Any]:
    _validate_policy(expected_policy)
    try:
        raw_text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise PolicyError(f"cannot read policy JSON at {path}: {exc}") from exc
    try:
        actual = json.loads(raw_text, object_pairs_hook=_reject_duplicate_keys)
    except PolicyError:
        raise
    except json.JSONDecodeError as exc:
        raise PolicyError(f"malformed policy JSON at {path}: {exc}") from exc
    actual_mapping = _as_mapping(actual, "policy JSON")

    for section_name in ("production", "tests"):
        actual_section_value = actual_mapping.get(section_name)
        if not isinstance(actual_section_value, Mapping):
            continue
        actual_baselines = actual_section_value.get("baselines")
        if not isinstance(actual_baselines, Mapping):
            continue
        expected_paths = _baseline_paths(expected_policy, section_name)
        for baseline_path in actual_baselines:
            if baseline_path not in expected_paths:
                raise PolicyError(f"unknown path in policy JSON: {baseline_path}")
            if _classify_path(baseline_path) != section_name:
                raise PolicyError(f"unknown path in policy JSON: {baseline_path}")

    _validate_policy(actual_mapping)
    expected_text = json.dumps(
        expected_policy, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    actual_text = json.dumps(
        actual_mapping, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    if actual_text != expected_text:
        raise PolicyError("policy JSON does not exactly match checker-owned policy")
    return dict(actual_mapping)


def _parse_index_listing(raw_listing: bytes) -> list[tuple[str, str, int, str]]:
    if not raw_listing:
        return []
    records = raw_listing.split(b"\0")
    if records[-1] != b"":
        raise IndexReadError("git ls-files returned a non-NUL-terminated index listing")
    entries: list[tuple[str, str, int, str]] = []
    seen: set[tuple[str, int]] = set()
    unmerged_paths: set[str] = set()
    for raw_record in records[:-1]:
        metadata, separator, raw_path = raw_record.partition(b"\t")
        fields = metadata.split(b" ")
        if not separator or len(fields) != 3 or any(not field for field in fields) or not raw_path:
            raise IndexReadError("git ls-files returned a malformed index entry")
        try:
            mode = fields[0].decode("ascii")
            object_id = fields[1].decode("ascii")
            stage_token = fields[2].decode("ascii")
            if stage_token not in {"0", "1", "2", "3"}:
                raise ValueError("invalid stage token")
            stage = int(stage_token)
            path = raw_path.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as exc:
            raise IndexReadError("git ls-files returned an invalid index entry") from exc
        path = _validate_repo_path(path)
        if mode not in {"100644", "100755", "120000", "160000"}:
            raise IndexReadError(f"unsupported Git index mode for {path}: {mode}")
        if len(object_id) not in {40, 64} or any(
            char not in "0123456789abcdef" for char in object_id
        ):
            raise IndexReadError(f"invalid Git object ID for {path}")
        if stage not in {0, 1, 2, 3}:
            raise IndexReadError(f"invalid Git index stage for {path}: {stage}")
        identity = (path, stage)
        if identity in seen:
            raise IndexReadError(f"duplicate Git index entry for {path} at stage {stage}")
        seen.add(identity)
        if stage != 0:
            unmerged_paths.add(path)
        entries.append((path, object_id, stage, mode))
    if unmerged_paths:
        paths = ", ".join(sorted(unmerged_paths))
        raise IndexReadError(f"unmerged Git index entries are not allowed: {paths}")
    return entries


def read_stage0_blobs(repo_root: Path) -> dict[str, bytes]:
    listing = subprocess.run(
        ["git", "ls-files", "--stage", "-z"],
        cwd=repo_root,
        capture_output=True,
        check=False,
    )
    if listing.returncode != 0:
        detail = listing.stderr.decode("utf-8", errors="replace").strip()
        raise IndexReadError(f"cannot read Git index: {detail}")
    entries = _parse_index_listing(listing.stdout)
    blobs: dict[str, bytes] = {}
    for path, object_id, _stage, mode in entries:
        if _classify_path(path) is None:
            continue
        if mode == "160000":
            raise IndexReadError(f"Python target is a Git submodule entry: {path}")
        blob_result = subprocess.run(
            ["git", "cat-file", "blob", object_id],
            cwd=repo_root,
            capture_output=True,
            check=False,
        )
        if blob_result.returncode != 0:
            detail = blob_result.stderr.decode("utf-8", errors="replace").strip()
            raise IndexReadError(f"cannot read stage-0 blob for {path}: {detail}")
        if path in blobs:
            raise IndexReadError(f"duplicate stage-0 Python path: {path}")
        blobs[path] = blob_result.stdout
    return blobs


def _finding(code: str, path: str, message: str) -> Finding:
    return Finding(code=code, path=path, message=message)


def evaluate_blobs(
    blobs: Mapping[str, bytes], *, policy: Mapping[str, Any] = POLICY
) -> CheckReport:
    production_policy, test_policy = _validate_policy(policy)
    candidates: dict[str, tuple[str, bytes]] = {}
    for raw_path, blob in blobs.items():
        path = _validate_repo_path(raw_path)
        if not isinstance(blob, bytes):
            raise IndexReadError(f"stage-0 content for {path} is not bytes")
        kind = _classify_path(path)
        if kind is not None:
            candidates[path] = (kind, blob)

    errors: list[Finding] = []
    notices: list[Finding] = []
    if not candidates:
        errors.append(
            _finding(
                "python_targets_missing",
                "<index>",
                "no tracked src/scripts Python or tests/test_*.py stage-0 blobs were found",
            )
        )
    production_baselines = _as_mapping(production_policy["baselines"], "production baselines")
    test_baselines = _as_mapping(test_policy["baselines"], "test baselines")
    for path in production_baselines:
        if path not in candidates or candidates[path][0] != "production":
            errors.append(
                _finding(
                    "stale_baseline", path, "production baseline path is not in the stage-0 index"
                )
            )
    for path in test_baselines:
        if path not in candidates or candidates[path][0] != "tests":
            errors.append(
                _finding("stale_baseline", path, "test baseline path is not in the stage-0 index")
            )

    production_limits = {
        "physical_loc": _positive_int(
            production_policy["physical_loc_limit"], "physical_loc_limit"
        ),
        "raw_bytes": _positive_int(production_policy["raw_bytes_limit"], "raw_bytes_limit"),
    }
    review_threshold = _positive_int(
        test_policy["review_threshold_bytes"], "review_threshold_bytes"
    )
    strong_threshold = _positive_int(
        test_policy["strong_review_threshold_bytes"], "strong_review_threshold_bytes"
    )
    for path, (kind, blob) in sorted(candidates.items()):
        if kind == "production":
            metrics = {"physical_loc": len(blob.splitlines()), "raw_bytes": len(blob)}
            baseline = _as_mapping(production_baselines.get(path, {}), f"baseline for {path}")
            for metric, limit in production_limits.items():
                actual = metrics[metric]
                record_value = baseline.get(metric)
                if record_value is None:
                    if actual > limit:
                        errors.append(
                            _finding(
                                f"production_{metric}",
                                path,
                                f"{metric} is {actual}; approved maximum is {limit} "
                                "with no grandfather baseline",
                            )
                        )
                    continue
                record = _as_mapping(record_value, f"{path} {metric} baseline")
                approved = _positive_int(record["value"], f"{path} {metric} baseline value")
                if actual > approved:
                    errors.append(
                        _finding(
                            "baseline_growth",
                            path,
                            f"{metric} grew from approved {approved} to {actual}",
                        )
                    )
                elif actual < approved:
                    errors.append(
                        _finding(
                            "baseline_shrink",
                            path,
                            f"{metric} shrank from approved {approved} to {actual}; "
                            "ratchet it exactly",
                        )
                    )
        else:
            actual = len(blob)
            baseline = _as_mapping(test_baselines.get(path, {}), f"baseline for {path}")
            record_value = baseline.get("raw_bytes")
            if actual < review_threshold:
                if record_value is not None:
                    errors.append(
                        _finding(
                            "test_baseline_remove",
                            path,
                            f"test module is below {review_threshold} bytes; "
                            "remove its review baseline",
                        )
                    )
            elif record_value is None:
                errors.append(
                    _finding(
                        "test_review_baseline_missing",
                        path,
                        f"test module is {actual} bytes; a reviewed exact-byte baseline "
                        f"is required at {review_threshold}",
                    )
                )
            else:
                record = _as_mapping(record_value, f"{path} raw_bytes baseline")
                approved = _positive_int(record["value"], f"{path} raw_bytes baseline value")
                if actual > approved:
                    errors.append(
                        _finding(
                            "baseline_growth",
                            path,
                            f"raw_bytes grew from approved {approved} to {actual}",
                        )
                    )
                elif actual < approved:
                    errors.append(
                        _finding(
                            "baseline_shrink",
                            path,
                            f"raw_bytes shrank from approved {approved} to {actual}; "
                            "ratchet it exactly",
                        )
                    )
            if actual >= strong_threshold:
                notices.append(
                    _finding(
                        "test_strong_review",
                        path,
                        f"{actual} bytes is at or above {strong_threshold}; "
                        "explicit reviewer attention is required",
                    )
                )
    return CheckReport(tuple(errors), tuple(notices), len(candidates))


def inspect_repository(
    repo_root: Path,
    *,
    policy_path: Path | None = None,
    expected_policy: Mapping[str, Any] = POLICY,
) -> tuple[dict[str, bytes], CheckReport]:
    policy_file = policy_path or repo_root / POLICY_RELATIVE_PATH
    policy = load_policy(policy_file, expected_policy=expected_policy)
    blobs = read_stage0_blobs(repo_root)
    return blobs, evaluate_blobs(blobs, policy=policy)


def _print_report(blobs: Mapping[str, bytes], report: CheckReport) -> None:
    for path, blob in sorted(blobs.items()):
        kind = _classify_path(path)
        if kind == "production":
            print(f"{path}: physical_loc={len(blob.splitlines())} raw_bytes={len(blob)}")
        elif kind == "tests":
            print(f"{path}: raw_bytes={len(blob)}")
    for notice in report.notices:
        print(f"REVIEW {notice.path}: {notice.message}")
    for error in report.errors:
        print(f"FAIL {error.path}: {error.message}")
    if not report.errors:
        print(f"PASS: checked {report.files_checked} stage-0 Python blobs")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check indexed Python module sizes and reviewed baselines."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check", action="store_true", help="fail if limits or ratchets are violated"
    )
    mode.add_argument("--report", action="store_true", help="print a read-only size report")
    args = parser.parse_args(argv)
    repo_root = Path(__file__).resolve().parents[1]
    try:
        blobs, report = inspect_repository(repo_root)
    except (IndexReadError, PolicyError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    if args.report:
        _print_report(blobs, report)
        return 1 if report.errors else 0
    for notice in report.notices:
        print(f"REVIEW {notice.path}: {notice.message}")
    for error in report.errors:
        print(f"FAIL {error.path}: {error.message}")
    if report.errors:
        print(f"FAIL: {len(report.errors)} guardrail violation(s)")
        return 1
    print(f"PASS: checked {report.files_checked} stage-0 Python blobs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
