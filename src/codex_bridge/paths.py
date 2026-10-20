from __future__ import annotations

import os
import stat
from pathlib import Path


class PathPolicyError(ValueError):
    """Raised when a cwd is outside the configured canonical roots."""

    def __init__(self, message: str, *, category: str = "path_invalid") -> None:
        super().__init__(message)
        self.category = category


class AllowedPathPolicy:
    def __init__(self, allowed_roots: tuple[str, ...]) -> None:
        self._allowed_roots = tuple(self._canonical_directory(root) for root in allowed_roots)

    @staticmethod
    def _canonical_directory(value: str) -> str:
        if not value or not Path(value).is_absolute():
            raise PathPolicyError("allowed roots must be absolute directories")
        try:
            path = Path(value).resolve(strict=True)
        except OSError as exc:
            raise PathPolicyError("allowed root cannot be resolved") from exc
        if not path.is_dir():
            raise PathPolicyError("allowed root must be a directory")
        return os.path.normcase(os.fspath(path))

    @staticmethod
    def _contains_parent_component(value: str) -> bool:
        return any(part == ".." for part in Path(value).parts)

    def validate_cwd(self, cwd: str) -> str:
        candidate = Path(cwd)
        if not candidate.is_absolute():
            raise PathPolicyError("cwd must be absolute", category="cwd_not_absolute")
        if self._contains_parent_component(cwd):
            raise PathPolicyError(
                "cwd must not contain parent traversal", category="cwd_parent_traversal"
            )
        try:
            canonical = candidate.resolve(strict=True)
        except OSError as exc:
            raise PathPolicyError(
                "cwd cannot be resolved", category="cwd_missing_or_unavailable"
            ) from exc
        if not canonical.is_dir():
            raise PathPolicyError("cwd must be an existing directory", category="cwd_not_directory")

        normalized = os.path.normcase(os.fspath(canonical))
        for root in self._allowed_roots:
            try:
                if os.path.commonpath((root, normalized)) == root:
                    return os.fspath(canonical)
            except ValueError:
                continue
        raise PathPolicyError(
            "cwd is outside CODEX_BRIDGE_ALLOWED_ROOTS", category="cwd_outside_allowed_root"
        )

    def validate_file(self, value: str) -> str:
        """Return an existing regular file path inside a configured canonical root."""
        candidate = Path(value)
        if not candidate.is_absolute():
            raise PathPolicyError("file path must be absolute", category="attachment_not_absolute")
        try:
            canonical = candidate.resolve(strict=True)
            mode = canonical.stat().st_mode
        except (OSError, RuntimeError) as exc:
            raise PathPolicyError(
                "file path cannot be resolved", category="attachment_missing_or_unavailable"
            ) from exc
        if not stat.S_ISREG(mode):
            raise PathPolicyError(
                "path must be a regular file", category="attachment_not_regular_file"
            )

        normalized = os.path.normcase(os.fspath(canonical))
        for root in self._allowed_roots:
            try:
                if os.path.commonpath((root, normalized)) == root:
                    return os.fspath(canonical)
            except ValueError:
                continue
        raise PathPolicyError(
            "file path is outside CODEX_BRIDGE_ALLOWED_ROOTS",
            category="attachment_outside_allowed_root",
        )

    def safe_relative_path(self, path: str) -> str:
        """Return an allowed-root-relative display path without exposing outside paths."""
        candidate = Path(path)
        if not candidate.is_absolute():
            return "<path omitted>"
        try:
            canonical = candidate.resolve(strict=False)
        except OSError:
            return "<path omitted>"
        normalized = os.path.normcase(os.fspath(canonical))
        for root in self._allowed_roots:
            try:
                if os.path.commonpath((root, normalized)) != root:
                    continue
                relative = os.path.relpath(normalized, root)
            except ValueError:
                continue
            return relative.replace(os.sep, "/")
        return "<path omitted>"
