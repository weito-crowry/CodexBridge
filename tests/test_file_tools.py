from __future__ import annotations

from pathlib import Path

import pytest

from codex_bridge.file_tools import FileToolError, FileTools


def test_file_read_preserves_utf8_and_returns_numbered_range(tmp_path: Path) -> None:
    source = tmp_path / "日本語 source.txt"
    source.write_bytes("one\r\n日本語\nthree\n".encode())
    tools = FileTools((str(tmp_path),))

    result = tools.file_read(str(source), start_line=2, max_lines=2)

    assert result["text"] == "日本語\nthree\n"
    assert result["start_line"] == 2
    assert result["end_line"] == 3
    assert result["total_lines"] == 3
    assert result["truncated"] is False


def test_file_read_rejects_paths_outside_allowed_roots(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside.txt"
    allowed.mkdir()
    outside.write_text("secret", encoding="utf-8")
    tools = FileTools((str(allowed),))

    with pytest.raises(FileToolError, match="outside CODEX_BRIDGE_ALLOWED_ROOTS"):
        tools.file_read(str(outside))


def test_directory_list_returns_bounded_typed_entries(tmp_path: Path) -> None:
    (tmp_path / "child").mkdir()
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    tools = FileTools((str(tmp_path),))

    result = tools.directory_list(str(tmp_path))

    by_name = {entry["name"]: entry for entry in result["entries"]}
    assert by_name["child"]["type"] == "directory"
    assert by_name["note.txt"]["type"] == "file"
    assert by_name["note.txt"]["size_bytes"] == 5
    assert result["truncated"] is False


def test_file_search_finds_names_and_content_lines(tmp_path: Path) -> None:
    source = tmp_path / "日本語 note.txt"
    source.write_bytes("first\r\nneedle 日本語\nlast\n".encode())
    tools = FileTools((str(tmp_path),))

    name_result = tools.file_search(str(tmp_path), "日本語", mode="name")
    content_result = tools.file_search(str(tmp_path), "needle", mode="content")

    assert name_result["results"][0]["path"] == str(source.resolve())
    assert content_result["results"] == [
        {"path": str(source.resolve()), "line_number": 2, "snippet": "needle 日本語"}
    ]


def test_file_read_rejects_non_utf8_and_invalid_ranges(tmp_path: Path) -> None:
    source = tmp_path / "binary.txt"
    source.write_bytes(b"ok\xffbad")
    tools = FileTools((str(tmp_path),))

    with pytest.raises(FileToolError, match="UTF-8"):
        tools.file_read(str(source))
    with pytest.raises(FileToolError, match="start_line"):
        tools.file_read(str(source), start_line=0)


def test_file_search_skips_symlink_targets_outside_allowed_roots(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret needle", encoding="utf-8")
    link = allowed / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are unavailable")
    tools = FileTools((str(allowed),))

    result = tools.file_search(str(allowed), "needle", mode="content")

    assert result["results"] == []


def test_directory_list_reports_truncation_at_entry_limit(tmp_path: Path) -> None:
    for index in range(205):
        (tmp_path / f"entry-{index:03}.txt").touch()
    tools = FileTools((str(tmp_path),))

    result = tools.directory_list(str(tmp_path))

    assert len(result["entries"]) == 200
    assert result["truncated"] is True
    assert result["truncation_reason"] == "entry_limit"


def test_directory_list_also_bounds_serialized_output(tmp_path: Path, monkeypatch) -> None:
    for index in range(5):
        (tmp_path / f"entry-{index}.txt").touch()
    monkeypatch.setattr(FileTools, "MAX_OUTPUT_BYTES", 100, raising=False)
    tools = FileTools((str(tmp_path),))

    result = tools.directory_list(str(tmp_path))

    assert result["truncated"] is True
    assert result["truncation_reason"] == "output_byte_limit"
