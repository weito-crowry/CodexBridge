from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QFileDialog, QToolButton

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
    controller, window, widget, client, _root = _controller(
        tmp_path, allowed_roots=(str(tmp_path / "allowed"),)
    )
    window.thread_pane.thread_selected.emit("thread-a")
    widget.input.setPlainText("keep this")
    widget.add_attachments([str(path)])

    assert controller.send()
    key = client.post_calls[-1][3]
    client.fail(key, "attachment_outside_allowed_root")

    assert widget.prompt_text == "keep this"
    assert widget.attachments == (str(path.resolve()),)
    assert "許可されたフォルダー内" in widget.status_label.text()
    assert "outside.txt" not in widget.status_label.text()
