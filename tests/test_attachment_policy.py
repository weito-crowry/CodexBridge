from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from codex_bridge.attachment_policy import (
    AttachmentPolicy,
    AttachmentPolicyError,
    ValidatedAttachment,
)
from codex_bridge.paths import AllowedPathPolicy

PNG_HEADER = b"\x89PNG\r\n\x1a\n"
JPEG_HEADER = b"\xff\xd8\xff"


def _policy(root: Path) -> AttachmentPolicy:
    return AttachmentPolicy(AllowedPathPolicy((str(root),)))


def test_classifies_images_and_other_files_in_selection_order(tmp_path: Path) -> None:
    image = tmp_path / "画像 sample.PNG"
    image.write_bytes(PNG_HEADER + b"image bytes")
    source = tmp_path / "main file.py"
    source.write_text("print('not read by attachment policy')", encoding="utf-8")
    jpeg = tmp_path / "photo.jpg"
    jpeg.write_bytes(JPEG_HEADER + b"image bytes")

    result = _policy(tmp_path).validate([str(image), str(source), str(jpeg)])

    assert result == (
        ValidatedAttachment(str(image.resolve()), "image", image.stat().st_size),
        ValidatedAttachment(str(source.resolve()), "file", source.stat().st_size),
        ValidatedAttachment(str(jpeg.resolve()), "image", jpeg.stat().st_size),
    )


def test_does_not_read_non_image_file_contents(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "config.json"
    source.write_text('{"value": 1}', encoding="utf-8")

    def fail_open(_self, *_args, **_kwargs):
        raise AssertionError("non-image attachment contents must not be read")

    monkeypatch.setattr(Path, "open", fail_open)

    assert _policy(tmp_path).validate([str(source)])[0].kind == "file"


def test_empty_attachment_list_is_valid(tmp_path: Path) -> None:
    assert _policy(tmp_path).validate([]) == ()


@pytest.mark.parametrize("path_kind", ["relative", "missing", "directory", "outside"])
def test_rejects_invalid_or_out_of_root_paths(tmp_path: Path, path_kind: str) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    file_path = root / "file.txt"
    file_path.write_text("data", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("data", encoding="utf-8")
    values = {
        "relative": "file.txt",
        "missing": str(root / "missing.txt"),
        "directory": str(root),
        "outside": str(outside),
    }

    with pytest.raises(AttachmentPolicyError) as error:
        _policy(root).validate([values[path_kind]])
    assert (
        error.value.category
        == {
            "relative": "attachment_not_absolute",
            "missing": "attachment_missing_or_unavailable",
            "directory": "attachment_not_regular_file",
            "outside": "attachment_outside_allowed_root",
        }[path_kind]
    )


@pytest.mark.parametrize(
    ("name", "header"),
    [("bad.png", b"not a png"), ("bad.jpeg", PNG_HEADER)],
)
def test_rejects_image_extension_and_header_mismatch(
    tmp_path: Path, name: str, header: bytes
) -> None:
    path = tmp_path / name
    path.write_bytes(header)

    with pytest.raises(AttachmentPolicyError) as error:
        _policy(tmp_path).validate([str(path)])
    assert error.value.category == "invalid_image_signature"


def test_rejects_files_larger_than_type_limit(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "large.txt"
    path.write_bytes(b"12345")
    monkeypatch.setattr("codex_bridge.attachment_policy.MAX_OTHER_FILE_BYTES", 4)

    with pytest.raises(AttachmentPolicyError) as error:
        _policy(tmp_path).validate([str(path)])
    assert error.value.category == "attachment_too_large"


def test_rejects_images_larger_than_image_limit(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "large.png"
    path.write_bytes(PNG_HEADER + b"image")
    monkeypatch.setattr("codex_bridge.attachment_policy.MAX_IMAGE_FILE_BYTES", 8)

    with pytest.raises(AttachmentPolicyError):
        _policy(tmp_path).validate([str(path)])


def test_rejects_combined_size_limit(tmp_path: Path, monkeypatch) -> None:
    files = [tmp_path / "a.txt", tmp_path / "b.txt"]
    for path in files:
        path.write_bytes(b"12345")
    monkeypatch.setattr("codex_bridge.attachment_policy.MAX_TOTAL_BYTES", 9)

    with pytest.raises(AttachmentPolicyError) as error:
        _policy(tmp_path).validate([str(path) for path in files])
    assert error.value.category == "total_attachment_size_exceeded"


def test_rejects_more_than_eight_attachments(tmp_path: Path) -> None:
    paths = []
    for index in range(9):
        path = tmp_path / f"{index}.txt"
        path.write_bytes(b"x")
        paths.append(str(path))

    with pytest.raises(AttachmentPolicyError) as error:
        _policy(tmp_path).validate(paths)
    assert error.value.category == "too_many_attachments"


def test_rejects_duplicate_canonical_paths(tmp_path: Path) -> None:
    original = tmp_path / "original.txt"
    original.write_bytes(b"x")
    alias = tmp_path / "alias.txt"
    try:
        alias.symlink_to(original)
    except OSError as exc:
        pytest.skip(f"file symlink unavailable: {exc}")

    with pytest.raises(AttachmentPolicyError) as error:
        _policy(tmp_path).validate([str(original), str(alias)])
    assert error.value.category == "duplicate_attachment"


def test_rejects_symlink_escape_and_allows_symlink_resolving_inside_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    inside = root / "inside.txt"
    inside.write_bytes(b"in")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"out")
    inside_link = root / "inside-link.txt"
    outside_link = root / "outside-link.txt"
    try:
        inside_link.symlink_to(inside)
        outside_link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"file symlink unavailable: {exc}")

    policy = _policy(root)
    result = policy.validate([str(inside_link)])
    assert result[0].canonical_path == str(inside.resolve())
    with pytest.raises(AttachmentPolicyError):
        policy.validate([str(outside_link)])


@pytest.mark.skipif(os.name != "nt", reason="Windows junction behavior")
def test_resolves_windows_junctions_before_allowed_root_check(tmp_path: Path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    inside_target = root / "target"
    inside_target.mkdir()
    inside_file = inside_target / "inside.txt"
    inside_file.write_text("inside", encoding="utf-8")
    outside_target = tmp_path / "outside"
    outside_target.mkdir()
    outside_file = outside_target / "outside.txt"
    outside_file.write_text("outside", encoding="utf-8")
    inside_junction = root / "inside-junction"
    outside_junction = root / "outside-junction"

    for junction, target in (
        (inside_junction, inside_target),
        (outside_junction, outside_target),
    ):
        environment = os.environ.copy()
        environment["CODEXBRIDGE_TEST_JUNCTION"] = str(junction)
        environment["CODEXBRIDGE_TEST_TARGET"] = str(target)
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "New-Item -ItemType Junction -Path $env:CODEXBRIDGE_TEST_JUNCTION "
                "-Target $env:CODEXBRIDGE_TEST_TARGET | Out-Null",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        if completed.returncode:
            pytest.skip(f"Windows junction creation unavailable: {completed.stderr.strip()}")

    policy = _policy(root)
    result = policy.validate([str(inside_junction / "inside.txt")])
    assert result[0].canonical_path == str(inside_file.resolve())
    with pytest.raises(AttachmentPolicyError):
        policy.validate([str(outside_junction / "outside.txt")])
