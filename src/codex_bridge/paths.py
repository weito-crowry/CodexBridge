from __future__ import annotations

import os
from pathlib import Path


class PathPolicyError(ValueError):
    """Raised when a cwd is outside the configured canonical roots."""


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
            raise PathPolicyError("cwd must be absolute")
        if self._contains_parent_component(cwd):
            raise PathPolicyError("cwd must not contain parent traversal")
        try:
            canonical = candidate.resolve(strict=True)
        except OSError as exc:
            raise PathPolicyError("cwd cannot be resolved") from exc
        if not canonical.is_dir():
            raise PathPolicyError("cwd must be an existing directory")

        normalized = os.path.normcase(os.fspath(canonical))
        for root in self._allowed_roots:
            try:
                if os.path.commonpath((root, normalized)) == root:
                    return os.fspath(canonical)
            except ValueError:
                continue
        raise PathPolicyError("cwd is outside the allowed roots")
