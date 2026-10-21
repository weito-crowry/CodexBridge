from __future__ import annotations

import os
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QFileDialog, QToolButton

from codex_bridge.console import composer_attachments
from codex_bridge.console.appearance import stylesheet_for_theme, theme_for_scheme
from codex_bridge.console.composer_controller import ComposerController
from codex_bridge.console.composer_widget import ComposerWidget
from tests.test_console_composer_controller import _application, _controller


def test_add_button_selects_multiple_files_and_remove_keeps_file(
    tmp_path: Path, monkeypatch
) -> None:
    _application()
    first = tmp_path / "first file.txt"
    second = tmp_path / "日本語.png"
    first.write_text("one", encoding="utf-8")
    second.write_bytes(b"image placeholder")
    widget = ComposerWidget()
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileNames",
        lambda *_args, **_kwargs: ([str(first), str(second)], ""),
    )

    widget.attachment_add_button.click()

    assert widget.attachments == (str(first.resolve()), str(second.resolve()))
    chips = widget.findChildren(QToolButton, "composerAttachmentRemove")
    assert len(chips) == 2
    chips[0].click()
    assert widget.attachments == (str(second.resolve()),)
    assert first.read_text(encoding="utf-8") == "one"
    assert second.exists()


def test_explicit_external_selection_is_copied_into_persistent_allowed_root_staging(
    tmp_path: Path, monkeypatch
) -> None:
    _application()
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    external = tmp_path / "Desktop" / "日本語 report.txt"
    external.parent.mkdir()
    external.write_bytes(b"source bytes")
    widget = ComposerWidget([str(allowed)])
    notices: list[str] = []
    widget.attachment_notice.connect(notices.append)
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileNames",
        lambda *_args, **_kwargs: ([str(external)], ""),
    )

    widget.attachment_add_button.click()

    staged = Path(widget.attachments[0])
    assert staged == allowed / ".codexbridge-staging" / external.name
    assert staged.read_bytes() == b"source bytes"
    assert external.read_bytes() == b"source bytes"
    assert str(staged) in " ".join(notices)
    widget.set_attachments(())
    assert staged.exists()


def test_external_staging_preserves_collisions_and_handles_source_disappearance(
    tmp_path: Path,
) -> None:
    _application()
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    staging = allowed / ".codexbridge-staging"
    staging.mkdir()
    existing = staging / "report.txt"
    existing.write_text("keep existing", encoding="utf-8")
    external = tmp_path / "report.txt"
    external.write_text("new contents", encoding="utf-8")
    widget = ComposerWidget([str(allowed)])

    widget.add_attachments([str(external)])

    staged = Path(widget.attachments[0])
    assert staged.name == "report (1).txt"
    assert staged.read_text(encoding="utf-8") == "new contents"
    assert existing.read_text(encoding="utf-8") == "keep existing"

    missing = tmp_path / "disappeared.txt"
    widget.add_attachments([str(missing)])
    assert all("disappeared" not in path for path in widget.attachments)


def test_external_selection_uses_configured_staging_directory(
    tmp_path: Path,
) -> None:
    _application()
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    custom_staging = allowed / "managed" / "attachments"
    external = tmp_path / "external.txt"
    external.write_text("data", encoding="utf-8")
    widget = ComposerWidget([str(allowed)], attachment_staging_directory=str(custom_staging))

    widget.add_attachments([str(external)])

    assert widget.attachments == (str(custom_staging / external.name),)
    assert Path(widget.attachments[0]).read_text(encoding="utf-8") == "data"


def test_external_staging_rejects_image_over_limit_before_creating_staging(
    tmp_path: Path, monkeypatch
) -> None:
    _application()
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    external = tmp_path / "large.png"
    external.write_bytes(b"1234")
    monkeypatch.setattr(composer_attachments, "MAX_IMAGE_FILE_BYTES", 3)
    widget = ComposerWidget([str(allowed)])
    widget.input.setPlainText("keep draft")
    notices: list[str] = []
    widget.attachment_notice.connect(notices.append)

    widget.add_attachments([str(external)])

    assert widget.attachments == ()
    assert widget.prompt_text == "keep draft"
    assert "20 MiB" in widget.status_label.text()
    assert notices and "20 MiB" in notices[-1]
    assert not (allowed / ".codexbridge-staging").exists()
    assert external.read_bytes() == b"1234"


def test_external_staging_rejects_other_file_over_limit_before_copy(
    tmp_path: Path, monkeypatch
) -> None:
    _application()
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    external = tmp_path / "large.txt"
    external.write_bytes(b"1234")
    monkeypatch.setattr(composer_attachments, "MAX_OTHER_FILE_BYTES", 3)
    widget = ComposerWidget([str(allowed)])

    widget.add_attachments([str(external)])

    assert widget.attachments == ()
    assert "50 MiB" in widget.status_label.text()
    assert not (allowed / ".codexbridge-staging").exists()
    assert external.read_bytes() == b"1234"


def test_allowed_root_selection_rejects_oversized_file_and_keeps_existing_draft(
    tmp_path: Path, monkeypatch
) -> None:
    _application()
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    existing = allowed / "existing.txt"
    existing.write_bytes(b"yes")
    oversized = allowed / "oversized.txt"
    oversized.write_bytes(b"1234")
    monkeypatch.setattr(composer_attachments, "MAX_OTHER_FILE_BYTES", 3)
    widget = ComposerWidget([str(allowed)])
    widget.input.setPlainText("keep this draft")
    widget.add_attachments([str(existing)])
    notices: list[str] = []
    widget.attachment_notice.connect(notices.append)

    widget.add_attachments([str(oversized)])

    assert widget.attachments == (str(existing.resolve()),)
    assert widget.prompt_text == "keep this draft"
    assert widget.status_label.text() == "その他の添付は50 MiB以下にしてください"
    assert notices[-1] == "その他の添付は50 MiB以下にしてください"


def test_external_oversized_file_is_rejected_before_staging(tmp_path: Path, monkeypatch) -> None:
    _application()
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    external = tmp_path / "oversized.txt"
    external.write_bytes(b"1234")
    monkeypatch.setattr(composer_attachments, "MAX_OTHER_FILE_BYTES", 3)
    widget = ComposerWidget([str(allowed)])
    notices: list[str] = []
    widget.attachment_notice.connect(notices.append)

    widget.add_attachments([str(external)])

    assert widget.attachments == ()
    assert widget.status_label.text() == "その他の添付は50 MiB以下にしてください"
    assert notices[-1] == "その他の添付は50 MiB以下にしてください"
    assert not (allowed / ".codexbridge-staging").exists()
    assert external.read_bytes() == b"1234"


def test_external_copy_success_does_not_hide_another_candidate_size_error(
    tmp_path: Path, monkeypatch
) -> None:
    _application()
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    valid = external / "valid.txt"
    valid.write_bytes(b"123")
    oversized = external / "oversized.txt"
    oversized.write_bytes(b"1234")
    monkeypatch.setattr(composer_attachments, "MAX_OTHER_FILE_BYTES", 3)
    widget = ComposerWidget([str(allowed)])

    widget.add_attachments([str(valid), str(oversized)])

    staging = allowed / ".codexbridge-staging"
    assert len(widget.attachments) == 1
    assert Path(widget.attachments[0]).name == valid.name
    assert (staging / valid.name).read_bytes() == b"123"
    assert not (staging / oversized.name).exists()
    assert widget.status_label.text() == "その他の添付は50 MiB以下にしてください"


@pytest.mark.parametrize("suffix", [".png", ".jpg", ".jpeg"])
@pytest.mark.parametrize("external", [False, True], ids=["allowed-root", "outside-root"])
def test_image_size_limit_is_inclusive_in_both_locations(
    tmp_path: Path, monkeypatch, suffix: str, external: bool
) -> None:
    _application()
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    parent = tmp_path / "outside" if external else allowed
    parent.mkdir(exist_ok=True)
    accepted = parent / f"accepted{suffix}"
    rejected = parent / f"rejected{suffix}"
    accepted.write_bytes(b"123")
    rejected.write_bytes(b"1234")
    monkeypatch.setattr(composer_attachments, "MAX_IMAGE_FILE_BYTES", 3)
    widget = ComposerWidget([str(allowed)])
    notices: list[str] = []
    widget.attachment_notice.connect(notices.append)

    widget.add_attachments([str(accepted)])
    widget.add_attachments([str(rejected)])

    assert len(widget.attachments) == 1
    assert Path(widget.attachments[0]).name == accepted.name
    assert widget.status_label.text() == "画像添付は20 MiB以下にしてください"
    assert notices[-1] == "画像添付は20 MiB以下にしてください"
    if external:
        assert not (allowed / ".codexbridge-staging" / rejected.name).exists()


@pytest.mark.parametrize("external", [False, True], ids=["allowed-root", "outside-root"])
def test_other_file_size_limit_is_inclusive_in_both_locations(
    tmp_path: Path, monkeypatch, external: bool
) -> None:
    _application()
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    parent = tmp_path / "outside" if external else allowed
    parent.mkdir(exist_ok=True)
    accepted = parent / "accepted.txt"
    rejected = parent / "rejected.txt"
    accepted.write_bytes(b"123")
    rejected.write_bytes(b"1234")
    monkeypatch.setattr(composer_attachments, "MAX_OTHER_FILE_BYTES", 3)
    widget = ComposerWidget([str(allowed)])
    notices: list[str] = []
    widget.attachment_notice.connect(notices.append)

    widget.add_attachments([str(accepted)])
    widget.add_attachments([str(rejected)])

    assert len(widget.attachments) == 1
    assert Path(widget.attachments[0]).name == accepted.name
    assert widget.status_label.text() == "その他の添付は50 MiB以下にしてください"
    assert notices[-1] == "その他の添付は50 MiB以下にしてください"
    if external:
        assert not (allowed / ".codexbridge-staging" / rejected.name).exists()


@pytest.mark.parametrize("has_existing", [False, True], ids=["same-batch", "existing-chip"])
def test_aggregate_size_limit_includes_existing_and_same_batch_candidates(
    tmp_path: Path, monkeypatch, has_existing: bool
) -> None:
    _application()
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setattr(composer_attachments, "MAX_OTHER_FILE_BYTES", 20)
    monkeypatch.setattr(composer_attachments, "MAX_TOTAL_BYTES", 8)
    widget = ComposerWidget([str(allowed)])
    widget.input.setPlainText("preserve draft")
    expected: list[str] = []
    three = allowed / "three.txt"
    five = allowed / "five.txt"
    one = allowed / "one.txt"
    three.write_bytes(b"123")
    five.write_bytes(b"12345")
    one.write_bytes(b"1")
    if has_existing:
        existing = allowed / "existing.txt"
        existing.write_bytes(b"12345")
        widget.add_attachments([str(existing)])
        expected.append(str(existing.resolve()))
        widget.add_attachments([str(three), str(one)])
        expected.append(str(three.resolve()))
    else:
        widget.add_attachments([str(three), str(five), str(one)])
        expected.extend([str(three.resolve()), str(five.resolve())])

    assert widget.attachments == tuple(expected)
    assert widget.prompt_text == "preserve draft"
    assert widget.status_label.text() == "添付ファイルの合計は100 MiB以下にしてください"


def test_allowed_root_batch_keeps_valid_candidate_when_later_file_exceeds_total(
    tmp_path: Path, monkeypatch
) -> None:
    _application()
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setattr(composer_attachments, "MAX_OTHER_FILE_BYTES", 20)
    monkeypatch.setattr(composer_attachments, "MAX_TOTAL_BYTES", 4)
    valid = allowed / "valid.txt"
    valid.write_bytes(b"123")
    oversized_total = allowed / "over.txt"
    oversized_total.write_bytes(b"12")
    widget = ComposerWidget([str(allowed)])
    widget.input.setPlainText("draft stays")
    notices: list[str] = []
    widget.attachment_notice.connect(notices.append)

    widget.add_attachments([str(valid), str(oversized_total)])

    assert widget.attachments == (str(valid.resolve()),)
    assert widget.prompt_text == "draft stays"
    assert widget.status_label.text() == "添付ファイルの合計は100 MiB以下にしてください"
    assert notices[-1] == "添付ファイルの合計は100 MiB以下にしてください"


def test_external_staging_enforces_aggregate_attachment_size(tmp_path: Path, monkeypatch) -> None:
    _application()
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    existing = allowed / "existing.txt"
    existing.write_bytes(b"12345")
    external = tmp_path / "external.txt"
    external.write_bytes(b"1234")
    monkeypatch.setattr(composer_attachments, "MAX_TOTAL_BYTES", 8)
    widget = ComposerWidget([str(allowed)])
    widget.add_attachments([str(existing)])

    widget.add_attachments([str(external)])

    assert widget.attachments == (str(existing.resolve()),)
    assert "100 MiB" in widget.status_label.text()
    assert not (allowed / ".codexbridge-staging").exists()
    assert existing.read_bytes() == b"12345"
    assert external.read_bytes() == b"1234"


def test_external_staging_aborts_and_removes_temporary_copy_when_source_grows(
    tmp_path: Path, monkeypatch
) -> None:
    _application()
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    external = tmp_path / "growing.txt"
    external.write_bytes(b"abcd")
    monkeypatch.setattr(composer_attachments, "MAX_OTHER_FILE_BYTES", 5)
    monkeypatch.setattr(composer_attachments, "MAX_TOTAL_BYTES", 20)
    original_open = Path.open
    grew = False

    def grow_before_copy(path: Path, *args, **kwargs):
        nonlocal grew
        mode = args[0] if args else kwargs.get("mode", "r")
        if path == external and mode == "rb" and not grew:
            with original_open(path, "ab") as source:
                source.write(b"ef")
            grew = True
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", grow_before_copy)
    widget = ComposerWidget([str(allowed)])
    widget.input.setPlainText("keep draft")

    widget.add_attachments([str(external)])

    staging = allowed / ".codexbridge-staging"
    assert widget.attachments == ()
    assert widget.prompt_text == "keep draft"
    assert "50 MiB" in widget.status_label.text()
    assert list(staging.iterdir()) == []
    assert external.read_bytes() == b"abcdef"


def test_file_selection_rejects_missing_directories_duplicates_and_extra_files(
    tmp_path: Path,
) -> None:
    _application()
    files = []
    for index in range(9):
        path = tmp_path / f"{index}.txt"
        path.write_text(str(index), encoding="utf-8")
        files.append(path)
    widget = ComposerWidget()

    widget.add_attachments(
        [str(files[0]), str(files[0]), str(tmp_path), str(tmp_path / "missing.txt")]
        + [str(path) for path in files[1:]]
    )

    assert widget.attachments == tuple(str(path.resolve()) for path in files[:8])
    assert "8" in widget.status_label.text() or "上限" in widget.status_label.text()
    assert all(path.exists() for path in files)


def test_attachment_chips_wrap_inside_the_composer_width(tmp_path: Path) -> None:
    app = _application()
    widget = ComposerWidget()
    paths = []
    for index in range(8):
        path = tmp_path / f"long attachment name {index}.txt"
        path.write_text(str(index), encoding="utf-8")
        paths.append(str(path))
    widget.resize(360, 240)
    widget.add_attachments(paths)
    widget.show()
    app.processEvents()

    assert widget.attachment_bar.scroll_area.width() <= widget.width()
    assert widget.attachment_bar.scroll_area.maximumHeight() <= 96
    assert len(widget.findChildren(QToolButton, "composerAttachmentRemove")) == 8


def test_attachment_chips_use_semantic_colors_in_light_and_dark_themes() -> None:
    for scheme in ("light", "dark"):
        theme = theme_for_scheme(scheme)
        stylesheet = stylesheet_for_theme(theme)
        assert "QFrame#composerAttachmentChip" in stylesheet
        assert f"background-color: {theme.tokens.alternate_surface.name()}" in stylesheet
        assert f"color: {theme.tokens.text.name()}" in stylesheet


def test_thread_and_cwd_drafts_keep_separate_attachment_lists(tmp_path: Path) -> None:
    _application()
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    file_a = root_a / "a.txt"
    file_b = root_b / "b.txt"
    file_a.write_text("a", encoding="utf-8")
    file_b.write_text("b", encoding="utf-8")
    controller, window, widget, _client, _root = _controller(
        tmp_path, allowed_roots=(str(root_a), str(root_b))
    )

    window.thread_pane.thread_selected.emit("thread-a")
    widget.add_attachments([str(file_a)])
    window.thread_pane.thread_selected.emit("thread-b")
    assert widget.attachments == ()
    widget.add_attachments([str(file_b)])
    window.thread_pane.thread_selected.emit("thread-a")
    assert widget.attachments == (str(file_a.resolve()),)
    window.thread_pane.thread_selected.emit("thread-b")
    assert widget.attachments == (str(file_b.resolve()),)

    controller.open_new_thread_draft(str(root_a))
    widget.add_attachments([str(file_a)])
    widget.cwd_combo.setCurrentIndex(widget.cwd_combo.findData(str(root_b)))
    assert widget.attachments == ()
    widget.add_attachments([str(file_b)])
    widget.cwd_combo.setCurrentIndex(widget.cwd_combo.findData(str(root_a)))
    assert widget.attachments == (str(file_a.resolve()),)
    QCoreApplication.processEvents()


def test_new_draft_key_uses_canonical_cwd_aliases(tmp_path: Path, monkeypatch) -> None:
    alias = str(tmp_path / "alias")
    canonical = str(tmp_path / "canonical")
    original_realpath = os.path.realpath
    normalized_alias = os.path.normcase(os.path.abspath(alias))

    def realpath(path: str, *args, **kwargs) -> str:
        if os.path.normcase(os.path.abspath(path)) == normalized_alias:
            return canonical
        return original_realpath(path, *args, **kwargs)

    monkeypatch.setattr(os.path, "realpath", realpath)

    assert ComposerController._new_draft_key(alias) == ComposerController._new_draft_key(canonical)


def test_send_snapshots_attachments_and_clears_only_unchanged_draft_on_success(
    tmp_path: Path,
) -> None:
    _application()
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("one", encoding="utf-8")
    second.write_text("two", encoding="utf-8")
    controller, window, widget, client, _root = _controller(tmp_path)
    window.thread_pane.thread_selected.emit("thread-a")
    widget.input.setPlainText("review files")
    widget.add_attachments([str(first)])

    assert controller.send()
    _path, payload, _token, key = client.post_calls[-1]
    assert payload["attachments"] == [str(first.resolve())]
    assert controller._send_contexts[key].attachments == (str(first.resolve()),)
    widget.add_attachments([str(second)])
    client.succeed(
        key,
        {"thread_id": "thread-a", "turn_id": "turn-a", "state": "in_progress"},
    )

    assert widget.prompt_text == ""
    assert widget.attachments == (str(first.resolve()), str(second.resolve()))


def test_success_clears_unchanged_attachments_and_does_not_touch_other_thread_draft(
    tmp_path: Path,
) -> None:
    _application()
    first = tmp_path / "thread-a.txt"
    second = tmp_path / "thread-b.txt"
    first.write_text("a", encoding="utf-8")
    second.write_text("b", encoding="utf-8")
    controller, window, widget, client, _root = _controller(tmp_path)
    window.thread_pane.thread_selected.emit("thread-a")
    widget.input.setPlainText("send to A")
    widget.add_attachments([str(first)])
    assert controller.send()
    key = client.post_calls[-1][3]

    window.thread_pane.thread_selected.emit("thread-b")
    widget.input.setPlainText("draft B")
    widget.add_attachments([str(second)])
    client.succeed(
        key,
        {"thread_id": "thread-a", "turn_id": "turn-a", "state": "in_progress"},
    )

    assert widget.prompt_text == "draft B"
    assert widget.attachments == (str(second.resolve()),)
    window.thread_pane.thread_selected.emit("thread-a")
    assert widget.prompt_text == ""
    assert widget.attachments == ()


def test_new_thread_post_includes_its_cwd_draft_attachments(tmp_path: Path) -> None:
    _application()
    root = tmp_path / "project"
    root.mkdir()
    path = root / "source.py"
    path.write_text("print(1)", encoding="utf-8")
    controller, _window, widget, client, _root = _controller(tmp_path, allowed_roots=(str(root),))
    controller.open_new_thread_draft(str(root))
    widget.input.setPlainText("review")
    widget.add_attachments([str(path)])

    assert controller.send()

    assert client.post_calls[-1][1]["attachments"] == [str(path.resolve())]


def test_attachment_failure_and_unknown_result_keep_draft_without_retry(tmp_path: Path) -> None:
    _application()
    path = tmp_path / "keep.txt"
    path.write_text("data", encoding="utf-8")
    controller, window, widget, client, _root = _controller(tmp_path)
    window.thread_pane.thread_selected.emit("thread-a")
    widget.input.setPlainText("keep this")
    widget.add_attachments([str(path)])

    assert controller.send()
    key = client.post_calls[-1][3]
    client.fail(key, "http_400")
    assert widget.prompt_text == "keep this"
    assert widget.attachments == (str(path.resolve()),)

    assert controller.send()
    second_key = client.post_calls[-1][3]
    client.fail(second_key, "http_409")
    assert widget.attachments == (str(path.resolve()),)
    assert len(client.post_calls) == 2
    assert not widget.send_button.isEnabled()


def test_out_of_root_attachment_failure_shows_specific_guidance_and_keeps_draft(
    tmp_path: Path,
) -> None:
    _application()
    path = tmp_path / "outside.txt"
    path.write_text("data", encoding="utf-8")
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    controller, window, widget, client, _root = _controller(tmp_path, allowed_roots=(str(allowed),))
    window.thread_pane.thread_selected.emit("thread-a")
    widget.input.setPlainText("keep this")
    widget.add_attachments([str(path)])

    assert controller.send()
    key = client.post_calls[-1][3]
    client.fail(key, "attachment_outside_allowed_root")

    assert widget.prompt_text == "keep this"
    assert widget.attachments == (str(allowed / ".codexbridge-staging" / path.name),)
    assert "許可されたフォルダー内" in widget.status_label.text()
    assert "outside.txt" not in widget.status_label.text()
