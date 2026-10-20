from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from pathlib import Path

from .paths import AllowedPathPolicy, PathPolicyError

MAX_FILE_PUT_BYTES = 1024 * 1024
_WINDOWS_RESERVED_NAME = re.compile(
    r"^(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?$",
    re.IGNORECASE | re.ASCII,
)
_WINDOWS_INVALID_CHARS = set('<>"|?*')
_REPARSE_POINT_ATTRIBUTE = 0x400


def _has_reparse_point(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & _REPARSE_POINT_ATTRIBUTE)


def validate_file_put_inbox(inbox_dir: str, allowed_roots: tuple[str, ...]) -> str:
    """Validate an existing canonical inbox and return its resolved path."""
    candidate = Path(inbox_dir)
    if not candidate.is_absolute() or any(part == ".." for part in candidate.parts):
        raise ValueError("file put inbox must be an absolute directory")
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current /= part
        if _has_reparse_point(current):
            raise ValueError("file put inbox must not use reparse points")
    try:
        canonical = AllowedPathPolicy(allowed_roots).validate_cwd(os.fspath(candidate))
    except (OSError, RuntimeError, PathPolicyError, TypeError) as exc:
        raise ValueError("file put inbox must exist inside allowed roots") from exc
    return canonical


def _valid_filename(filename: object) -> bool:
    if not isinstance(filename, str) or not filename or filename in {".", ".."}:
        return False
    if filename.endswith((".", " ")) or filename[0] == " ":
        return False
    if any(ord(char) < 32 or char in _WINDOWS_INVALID_CHARS for char in filename):
        return False
    if "/" in filename or "\\" in filename or ":" in filename:
        return False
    reserved_stem = filename.split(".", maxsplit=1)[0].rstrip(" .")
    if (
        Path(filename).name != filename
        or _WINDOWS_RESERVED_NAME.fullmatch(filename)
        or _WINDOWS_RESERVED_NAME.fullmatch(reserved_stem)
    ):
        return False
    return True


def file_put(
    filename: str,
    content: str,
    *,
    inbox_dir: str | None,
    allowed_roots: tuple[str, ...],
) -> dict[str, object]:
    """Publish one bounded UTF-8 file into the configured inbox without replacement."""
    if inbox_dir is None:
        return {"ok": False, "error": "file_put_disabled"}
    if not _valid_filename(filename):
        return {"ok": False, "error": "invalid_filename"}
    if not isinstance(content, str):
        return {"ok": False, "error": "content_must_be_utf8_text"}
    try:
        payload = content.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return {"ok": False, "error": "content_must_be_utf8_text"}
    if len(payload) > MAX_FILE_PUT_BYTES:
        return {"ok": False, "error": "content_too_large"}
    try:
        inbox = validate_file_put_inbox(inbox_dir, allowed_roots)
    except ValueError:
        return {"ok": False, "error": "file_put_inbox_invalid"}
    directory = Path(inbox)
    destination = directory / filename
    if destination.exists():
        return {"ok": False, "error": "file_exists"}
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=".codexbridge-file-put-", suffix=".tmp", dir=inbox
        )
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())

        # Recheck the canonical directory immediately before publishing. The final
        # rename/link operation itself is atomic and never replaces an existing name.
        if validate_file_put_inbox(inbox, allowed_roots) != inbox:
            return {"ok": False, "error": "file_put_inbox_invalid"}
        if os.name == "nt":
            os.rename(temporary, destination)
        else:
            os.link(temporary, destination)
            os.unlink(temporary)
        temporary = None
        return {
            "ok": True,
            "path": os.fspath(destination.resolve(strict=True)),
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    except FileExistsError:
        return {"ok": False, "error": "file_exists"}
    except OSError:
        return {"ok": False, "error": "file_put_write_failed"}
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
