from __future__ import annotations

import codecs
import json
import os
import time
from pathlib import Path
from typing import BinaryIO, Literal, TypedDict

from .paths import AllowedPathPolicy, PathPolicyError


class FileToolError(ValueError):
    """A bounded, user-safe error raised by a native file tool."""


class _Entry(TypedDict):
    name: str
    path: str
    type: Literal["file", "directory"]
    size_bytes: int | None


class _SearchMatch(TypedDict):
    path: str
    line_number: int
    snippet: str


class FileTools:
    """Bounded read-only file tools scoped to configured allowed roots."""

    MAX_FILE_READ_BYTES = 2 * 1024 * 1024
    FILE_READ_SECONDS = 2.0
    MAX_FILE_OUTPUT_BYTES = 256 * 1024
    DEFAULT_FILE_READ_LINES = 200
    MAX_FILE_READ_LINES = 2000
    DIRECTORY_ENTRY_LIMIT = 200
    DIRECTORY_SCAN_LIMIT = 1000
    DIRECTORY_TIME_SECONDS = 2.0
    SEARCH_RESULT_LIMIT = 100
    MAX_OUTPUT_BYTES = 256 * 1024
    SEARCH_ENTRY_LIMIT = 2000
    SEARCH_FILE_BYTES = 256 * 1024
    SEARCH_TOTAL_BYTES = 16 * 1024 * 1024
    SEARCH_SNIPPET_CHARS = 240
    SEARCH_TIME_SECONDS = 2.0

    def __init__(self, allowed_roots: tuple[str, ...]) -> None:
        self._policy = AllowedPathPolicy(allowed_roots)

    def file_read(
        self,
        path: str,
        start_line: int | None = None,
        max_lines: int | None = None,
    ) -> dict[str, object]:
        """Read bounded UTF-8 text from an allowed regular file."""
        if start_line is not None and (
            isinstance(start_line, bool) or not isinstance(start_line, int) or start_line < 1
        ):
            raise FileToolError("start_line must be an integer greater than or equal to 1")
        if max_lines is not None and (
            isinstance(max_lines, bool)
            or not isinstance(max_lines, int)
            or not 1 <= max_lines <= self.MAX_FILE_READ_LINES
        ):
            raise FileToolError(
                f"max_lines must be an integer between 1 and {self.MAX_FILE_READ_LINES}"
            )

        first_line = start_line or 1
        line_limit = max_lines or self.DEFAULT_FILE_READ_LINES
        canonical = self._validate_file(path)
        deadline = time.monotonic() + self.FILE_READ_SECONDS
        try:
            with open(canonical, "rb") as source:
                raw, byte_limited, time_limited = self._read_bounded(
                    source, self.MAX_FILE_READ_BYTES, deadline
                )
        except OSError as exc:
            raise FileToolError("file cannot be read") from exc

        raw = raw[: self.MAX_FILE_READ_BYTES]
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        try:
            decoded = decoder.decode(raw, final=not byte_limited)
        except UnicodeDecodeError as exc:
            raise FileToolError("file is not valid UTF-8 text") from exc
        lines = decoded.splitlines(keepends=True)
        total_lines = len(lines) if not byte_limited and not time_limited else None
        if first_line > len(lines) and not byte_limited and not time_limited:
            raise FileToolError("start_line is beyond the end of the file")
        if first_line > len(lines) and (byte_limited or time_limited):
            raise FileToolError("start_line exceeds the bounded file scan range")

        selected = lines[first_line - 1 : first_line - 1 + line_limit]
        text = "".join(selected)
        truncated = byte_limited or time_limited or len(lines) > first_line - 1 + len(selected)
        reason = "time_limit" if time_limited else "read_byte_limit" if byte_limited else None
        if len(lines) > first_line - 1 + len(selected):
            reason = "line_limit"
        if len(text.encode("utf-8")) > self.MAX_FILE_OUTPUT_BYTES:
            text = self._truncate_utf8(text, self.MAX_FILE_OUTPUT_BYTES)
            truncated = True
            reason = "output_byte_limit"

        end_line = first_line + len(selected) - 1 if selected else None
        if total_lines is not None and first_line + len(selected) - 1 < total_lines:
            truncated = True
            reason = reason or "line_limit"
        return {
            "path": canonical,
            "start_line": first_line,
            "end_line": end_line,
            "total_lines": total_lines,
            "truncated": truncated,
            "truncation_reason": reason,
            "text": text,
        }

    def directory_list(self, path: str) -> dict[str, object]:
        """List at most DIRECTORY_ENTRY_LIMIT immediate children of a directory."""
        canonical = self._validate_directory(path)
        entries: list[_Entry] = []
        truncated = False
        reason: str | None = None
        scanned_entries = 0
        deadline = time.monotonic() + self.DIRECTORY_TIME_SECONDS
        try:
            with os.scandir(canonical) as iterator:
                for entry in iterator:
                    scanned_entries += 1
                    if time.monotonic() >= deadline:
                        truncated = True
                        reason = "time_limit"
                        break
                    if scanned_entries > self.DIRECTORY_SCAN_LIMIT:
                        truncated = True
                        reason = "scan_entry_limit"
                        break
                    if len(entries) >= self.DIRECTORY_ENTRY_LIMIT:
                        truncated = True
                        reason = "entry_limit"
                        break
                    try:
                        target = Path(entry.path).resolve(strict=True)
                        target.stat()
                    except (OSError, RuntimeError):
                        continue
                    if not self._is_allowed(target, directory=Path(entry.path).is_dir()):
                        continue
                    if os.path.isdir(target):
                        kind: Literal["file", "directory"] = "directory"
                        size: int | None = None
                    elif os.path.isfile(target):
                        kind = "file"
                        try:
                            size = target.stat().st_size
                        except OSError:
                            size = None
                    else:
                        continue
                    item: _Entry = {
                        "name": entry.name,
                        "path": os.fspath(target),
                        "type": kind,
                        "size_bytes": size,
                    }
                    candidate = [*entries, item]
                    envelope = {
                        "path": canonical,
                        "entries": candidate,
                        "truncated": False,
                        "truncation_reason": None,
                    }
                    if self._encoded_size(envelope) > self.MAX_OUTPUT_BYTES:
                        truncated = True
                        reason = "output_byte_limit"
                        break
                    entries.append(item)
        except OSError as exc:
            raise FileToolError("directory cannot be listed") from exc
        entries.sort(key=lambda item: (item["name"].casefold(), item["name"]))
        return {
            "path": canonical,
            "entries": entries,
            "truncated": truncated,
            "truncation_reason": reason or ("entry_limit" if truncated else None),
        }

    def file_search(
        self,
        root: str,
        query: str,
        mode: Literal["name", "content"],
        max_results: int | None = None,
    ) -> dict[str, object]:
        """Search allowed paths by name or UTF-8 content with explicit work limits."""
        if not isinstance(query, str) or not query:
            raise FileToolError("query must not be empty")
        if mode not in {"name", "content"}:
            raise FileToolError("mode must be 'name' or 'content'")
        if max_results is not None and (
            isinstance(max_results, bool) or not isinstance(max_results, int) or max_results < 1
        ):
            raise FileToolError("max_results must be a positive integer")
        result_limit = min(max_results or self.SEARCH_RESULT_LIMIT, self.SEARCH_RESULT_LIMIT)
        canonical_root = self._validate_directory(root)
        results: list[dict[str, object]] = []
        stack = [Path(canonical_root)]
        seen_directories: set[str] = set()
        scanned_entries = 0
        scanned_bytes = 0
        skipped_unreadable = 0
        reason: str | None = None
        deadline = time.monotonic() + self.SEARCH_TIME_SECONDS

        while stack:
            if time.monotonic() >= deadline:
                reason = "time_limit"
                break
            directory = stack.pop()
            try:
                canonical_directory = self._validate_directory(os.fspath(directory))
            except FileToolError:
                continue
            if canonical_directory in seen_directories:
                continue
            seen_directories.add(canonical_directory)
            try:
                with os.scandir(canonical_directory) as iterator:
                    children = []
                    for entry in iterator:
                        scanned_entries += 1
                        if scanned_entries > self.SEARCH_ENTRY_LIMIT:
                            reason = "entry_limit"
                            break
                        children.append(entry)
                    children.sort(key=lambda entry: (entry.name.casefold(), entry.name))
            except OSError:
                skipped_unreadable += 1
                continue

            for entry in children:
                if time.monotonic() >= deadline:
                    reason = "time_limit"
                    break
                try:
                    target = Path(entry.path).resolve(strict=True)
                    target.stat()
                except (OSError, RuntimeError):
                    skipped_unreadable += 1
                    continue
                if os.path.isdir(target):
                    if self._is_allowed(target, directory=True):
                        stack.append(target)
                        if mode == "name" and query.casefold() in entry.name.casefold():
                            results.append({"path": os.fspath(target), "type": "directory"})
                            if not self._search_output_fits(canonical_root, query, mode, results):
                                results.pop()
                                reason = "output_byte_limit"
                                break
                elif os.path.isfile(target) and self._is_allowed(target, directory=False):
                    if mode == "name":
                        if query.casefold() in entry.name.casefold():
                            results.append({"path": os.fspath(target), "type": "file"})
                            if not self._search_output_fits(canonical_root, query, mode, results):
                                results.pop()
                                reason = "output_byte_limit"
                                break
                    else:
                        remaining = self.SEARCH_TOTAL_BYTES - scanned_bytes
                        if remaining <= 0:
                            reason = "scan_byte_limit"
                            break
                        try:
                            with open(target, "rb") as source:
                                raw, byte_limited, time_limited = self._read_bounded(
                                    source, min(self.SEARCH_FILE_BYTES, remaining), deadline
                                )
                        except OSError:
                            skipped_unreadable += 1
                            continue
                        byte_cap = min(self.SEARCH_FILE_BYTES, remaining)
                        over_limit = byte_limited
                        raw = raw[:byte_cap]
                        scanned_bytes += len(raw)
                        decoder = codecs.getincrementaldecoder("utf-8")("strict")
                        try:
                            content = decoder.decode(raw, final=not over_limit)
                        except UnicodeDecodeError:
                            skipped_unreadable += 1
                            continue
                        if over_limit:
                            reason = (
                                "scan_byte_limit"
                                if remaining <= self.SEARCH_FILE_BYTES
                                else "file_byte_limit"
                            )
                        if time_limited:
                            reason = "time_limit"
                        for line_number, line in enumerate(content.splitlines(), start=1):
                            if query.casefold() in line.casefold():
                                results.append(
                                    {
                                        "path": os.fspath(target),
                                        "line_number": line_number,
                                        "snippet": line[: self.SEARCH_SNIPPET_CHARS],
                                    }
                                )
                                if not self._search_output_fits(
                                    canonical_root, query, mode, results
                                ):
                                    results.pop()
                                    reason = "output_byte_limit"
                                    break
                                if len(results) > result_limit:
                                    results.pop()
                                    reason = "result_limit"
                                    break
                        if len(results) >= result_limit and reason is None:
                            reason = "result_limit"
                            break
                if len(results) > result_limit:
                    results.pop()
                    reason = "result_limit"
                    break
            if reason is not None:
                break

        return {
            "root": canonical_root,
            "query": query,
            "mode": mode,
            "results": results,
            "scanned_entries": scanned_entries,
            "scanned_bytes": scanned_bytes,
            "skipped_unreadable": skipped_unreadable,
            "truncated": reason is not None,
            "truncation_reason": reason,
        }

    def _validate_file(self, path: str) -> str:
        try:
            return self._policy.validate_file(path)
        except PathPolicyError as exc:
            raise FileToolError(self._safe_path_error(exc)) from None

    def _validate_directory(self, path: str) -> str:
        try:
            return self._policy.validate_cwd(path)
        except PathPolicyError as exc:
            raise FileToolError(self._safe_path_error(exc)) from None

    def _is_allowed(self, path: Path, *, directory: bool) -> bool:
        try:
            if directory:
                self._policy.validate_cwd(os.fspath(path))
            else:
                self._policy.validate_file(os.fspath(path))
        except PathPolicyError:
            return False
        return True

    @staticmethod
    def _safe_path_error(exc: PathPolicyError) -> str:
        if "outside" in str(exc):
            return "path is outside CODEX_BRIDGE_ALLOWED_ROOTS"
        if "regular file" in str(exc):
            return "path must be a regular file"
        if exc.category in {"attachment_missing_or_unavailable", "cwd_missing_or_unavailable"}:
            return "file or directory does not exist or cannot be accessed"
        if exc.category == "cwd_not_directory":
            return "path must be an existing directory"
        if "directory" in str(exc):
            return "path must be an existing allowed directory"
        if "absolute" in str(exc):
            return "path must be absolute"
        return "path cannot be resolved or accessed"

    @staticmethod
    def _truncate_utf8(value: str, byte_limit: int) -> str:
        encoded = value.encode("utf-8")[:byte_limit]
        return encoded.decode("utf-8", errors="ignore")

    @staticmethod
    def _encoded_size(value: object) -> int:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    def _search_output_fits(
        self,
        root: str,
        query: str,
        mode: Literal["name", "content"],
        results: list[dict[str, object]],
    ) -> bool:
        return (
            self._encoded_size(
                {
                    "root": root,
                    "query": query,
                    "mode": mode,
                    "results": results,
                    "scanned_entries": 0,
                    "scanned_bytes": 0,
                    "skipped_unreadable": 0,
                    "truncated": False,
                    "truncation_reason": None,
                }
            )
            <= self.MAX_OUTPUT_BYTES
        )

    @staticmethod
    def _read_bounded(
        source: BinaryIO, byte_limit: int, deadline: float
    ) -> tuple[bytes, bool, bool]:
        chunks: list[bytes] = []
        byte_count = 0
        while byte_count <= byte_limit:
            if time.monotonic() >= deadline:
                return b"".join(chunks), False, True
            chunk = source.read(min(64 * 1024, byte_limit + 1 - byte_count))
            if not chunk:
                return b"".join(chunks), False, False
            chunks.append(chunk)
            byte_count += len(chunk)
        return b"".join(chunks), True, False
