from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .paths import AllowedPathPolicy, PathPolicyError

MAX_ATTACHMENTS = 8
MAX_IMAGE_FILE_BYTES = 20 * 1024 * 1024
MAX_OTHER_FILE_BYTES = 50 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SIGNATURE = b"\xff\xd8\xff"
_IMAGE_SIGNATURES = {".png": _PNG_SIGNATURE, ".jpg": _JPEG_SIGNATURE, ".jpeg": _JPEG_SIGNATURE}


class AttachmentPolicyError(ValueError):
    """Raised when an attachment cannot be accepted for a Composer request."""


@dataclass(frozen=True, slots=True)
class ValidatedAttachment:
    canonical_path: str
    kind: Literal["image", "file"]
    size: int


class AttachmentPolicy:
    def __init__(self, path_policy: AllowedPathPolicy) -> None:
        self._path_policy = path_policy

    def validate(self, paths: Sequence[str]) -> tuple[ValidatedAttachment, ...]:
        if isinstance(paths, (str, bytes)) or not isinstance(paths, Sequence):
            raise AttachmentPolicyError("invalid attachments")
        if len(paths) > MAX_ATTACHMENTS:
            raise AttachmentPolicyError("invalid attachments")

        result: list[ValidatedAttachment] = []
        seen: set[str] = set()
        total_size = 0
        for raw_path in paths:
            if not isinstance(raw_path, str):
                raise AttachmentPolicyError("invalid attachments")
            try:
                canonical = self._path_policy.validate_file(raw_path)
                size = Path(canonical).stat().st_size
            except (OSError, PathPolicyError) as exc:
                raise AttachmentPolicyError("invalid attachment") from exc

            normalized = os.path.normcase(canonical)
            if normalized in seen:
                raise AttachmentPolicyError("invalid attachments")
            seen.add(normalized)

            extension = Path(canonical).suffix.casefold()
            signature = _IMAGE_SIGNATURES.get(extension)
            if signature is None:
                kind: Literal["image", "file"] = "file"
                if size > MAX_OTHER_FILE_BYTES:
                    raise AttachmentPolicyError("invalid attachment")
            else:
                kind = "image"
                if size > MAX_IMAGE_FILE_BYTES:
                    raise AttachmentPolicyError("invalid attachment")
                try:
                    with Path(canonical).open("rb") as image_file:
                        header = image_file.read(len(signature))
                except OSError as exc:
                    raise AttachmentPolicyError("invalid attachment") from exc
                if not header.startswith(signature):
                    raise AttachmentPolicyError("invalid attachment")

            total_size += size
            if total_size > MAX_TOTAL_BYTES:
                raise AttachmentPolicyError("invalid attachments")
            result.append(ValidatedAttachment(canonical, kind, size))
        return tuple(result)
