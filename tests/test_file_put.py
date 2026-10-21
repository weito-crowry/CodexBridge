from __future__ import annotations

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from codex_bridge.file_put import MAX_FILE_PUT_BYTES, _valid_filename, file_put


def _put(inbox: Path, filename: str, content: str) -> dict[str, object]:
    return file_put(
        filename,
        content,
        inbox_dir=os.fspath(inbox),
        allowed_roots=(os.fspath(inbox.parent),),
    )


def test_file_put_preserves_utf8_bytes_and_returns_digest(tmp_path: Path) -> None:
    inbox = tmp_path / "専用 inbox"
    inbox.mkdir()
    content = "print('こんにちは')\r\n" + "x" * 10_000

    result = _put(inbox, "script_日本語.py", content)

    data = (inbox / "script_日本語.py").read_bytes()
    assert result["ok"] is True
    assert data == content.encode("utf-8")
    assert result["bytes"] == len(data)
    assert result["sha256"] == hashlib.sha256(data).hexdigest()
    assert result["path"] == os.fspath((inbox / "script_日本語.py").resolve())


def test_file_put_is_disabled_without_configured_inbox(tmp_path: Path) -> None:
    result = file_put("script.py", "pass", inbox_dir=None, allowed_roots=(os.fspath(tmp_path),))

    assert result == {"ok": False, "error": "file_put_disabled"}


@pytest.mark.parametrize(
    "filename",
    [
        "../script.py",
        "sub/script.py",
        "sub\\script.py",
        "C:script.py",
        "\\\\host\\file",
        "a:b",
        "CON",
        "NUL.txt",
        "name.",
        "name ",
    ],
)
def test_file_put_rejects_unsafe_windows_filenames(tmp_path: Path, filename: str) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()

    result = _put(inbox, filename, "payload")

    assert result["ok"] is False
    assert result["error"] == "invalid_filename"
    assert list(inbox.iterdir()) == []


@pytest.mark.parametrize("filename", ["CON .txt", "NUL  .py", "COM1 .log"])
def test_file_put_rejects_reserved_device_stem_with_spaces(filename: str) -> None:
    assert _valid_filename(filename) is False


def test_file_put_duplicate_does_not_overwrite_existing_file(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "script.py").write_bytes(b"original")

    result = _put(inbox, "script.py", "replacement")

    assert result["ok"] is False
    assert result["error"] == "file_exists"
    assert (inbox / "script.py").read_bytes() == b"original"


def test_file_put_concurrent_same_name_publishes_exactly_one_payload(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda index: _put(inbox, "same.py", f"payload {index}"), range(8)))

    assert sum(result["ok"] is True for result in results) == 1
    assert sum(result.get("error") == "file_exists" for result in results) == 7
    assert (inbox / "same.py").read_text(encoding="utf-8").startswith("payload ")
    assert len(list(inbox.iterdir())) == 1


def test_file_put_enforces_utf8_byte_limit_before_creating_file(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    content = "あ" * (MAX_FILE_PUT_BYTES // 3 + 1)

    result = _put(inbox, "too-large.py", content)

    assert result["ok"] is False
    assert result["error"] == "content_too_large"
    assert list(inbox.iterdir()) == []


def test_file_put_rejects_inbox_outside_allowed_roots(tmp_path: Path) -> None:
    root = tmp_path / "allowed"
    inbox = tmp_path / "outside"
    root.mkdir()
    inbox.mkdir()

    result = file_put(
        "script.py",
        "pass",
        inbox_dir=os.fspath(inbox),
        allowed_roots=(os.fspath(root),),
    )

    assert result == {"ok": False, "error": "file_put_inbox_invalid"}
    assert list(inbox.iterdir()) == []


def test_file_put_rejects_reparse_point_inbox(tmp_path: Path) -> None:
    root = tmp_path / "root"
    target = root / "target"
    inbox = root / "inbox-link"
    target.mkdir(parents=True)
    try:
        inbox.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlink unavailable: {exc}")

    result = file_put(
        "script.py",
        "pass",
        inbox_dir=os.fspath(inbox),
        allowed_roots=(os.fspath(root),),
    )

    assert result == {"ok": False, "error": "file_put_inbox_invalid"}
    assert list(target.iterdir()) == []


def test_file_put_cleans_temporary_file_when_publish_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import codex_bridge.file_put as file_put_module

    inbox = tmp_path / "inbox"
    inbox.mkdir()

    def fail_rename(source: str, destination: Path) -> None:
        raise OSError("simulated publish failure")

    monkeypatch.setattr(file_put_module.os, "rename", fail_rename)
    result = _put(inbox, "script.py", "pass")

    assert result == {"ok": False, "error": "file_put_write_failed"}
    assert list(inbox.iterdir()) == []
