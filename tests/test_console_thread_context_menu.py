from __future__ import annotations

import os

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QTreeWidgetItem

from codex_bridge.console.widgets import ThreadListPane


def _thread_item(pane: ThreadListPane, thread_id: str) -> QTreeWidgetItem:
    items = (
        pane.list_widget.topLevelItem(group_index).child(child_index)
        for group_index in range(pane.list_widget.topLevelItemCount())
        for child_index in range(pane.list_widget.topLevelItem(group_index).childCount())
    )
    return next(item for item in items if item.data(0, Qt.ItemDataRole.UserRole) == thread_id)


def test_project_parent_has_folder_menu_and_empty_placeholder_has_no_menu(tmp_path) -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = ThreadListPane()
    pane.set_threads([{"id": "a", "cwd": str(tmp_path)}])

    parent = pane.list_widget.topLevelItem(0)
    assert parent is not None
    assert pane._context_menu_for_item(parent) is not None
    pane.set_empty_state("No threads found.")
    placeholder = pane.list_widget.topLevelItem(0)
    assert not placeholder.flags() & Qt.ItemFlag.ItemIsSelectable
    assert pane._context_menu_for_item(placeholder) is None


def test_running_section_parent_is_not_treated_as_project_context_menu(tmp_path) -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    cwd = str(tmp_path / "project")
    pane = ThreadListPane()
    pane.set_threads(
        [{"id": "running", "cwd": cwd}],
        thread_statuses={"running": "in_progress"},
    )

    running_section = pane.list_widget.topLevelItem(0)
    running_project = running_section.child(0)
    existing_project = pane.list_widget.topLevelItem(1)

    assert running_section.text(0) == "現在処理中"
    assert pane._context_menu_for_item(running_section) is None
    assert pane._context_menu_for_item(running_project) is None
    assert pane._context_menu_for_item(existing_project) is not None


def test_thread_context_menu_uses_right_clicked_thread_for_all_actions(
    tmp_path, monkeypatch
) -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = ThreadListPane()
    pane.set_threads(
        [
            {"id": "selected", "name": "Selected", "cwd": str(tmp_path / "selected")},
            {
                "id": "target",
                "name": "Target",
                "cwd": str(tmp_path),
                "preview": "must not copy",
            },
        ],
        active_thread_ids={"target"},
    )
    pane.list_widget.setCurrentItem(_thread_item(pane, "selected"))
    renamed: list[str] = []
    pane.thread_rename_requested.connect(renamed.append)
    opened: list[str] = []
    pane.thread_open_requested.connect(opened.append)
    copied_threads: list[str] = []
    pane.thread_copy_requested.connect(copied_threads.append)
    copied_texts: list[str] = []
    monkeypatch.setattr(pane, "_copy_text", copied_texts.append)

    menu = pane._context_menu_for_item(_thread_item(pane, "target"))
    actions = menu.actions()

    assert [action.text() for action in actions] == [
        "名前を変更...",
        "Open in Codex App",
        "スレッドIDをコピー",
        "スレッド情報をコピー",
        "Copy thread content",
        "",
        "作業フォルダを開く",
    ]
    actions[0].trigger()
    assert renamed == ["target"]

    actions[1].trigger()
    assert opened == ["target"]

    actions[2].trigger()
    assert copied_texts == ["target"]

    actions[3].trigger()
    assert copied_texts == [
        "target",
        f"Name: Target\nThread ID: target\nCWD: {tmp_path}\nStatus: active",
    ]
    assert "must not copy" not in copied_texts[1]
    assert pane.list_widget.currentItem().data(0, Qt.ItemDataRole.UserRole) == "selected"

    actions[4].trigger()
    assert copied_threads == ["target"]
    assert pane.list_widget.currentItem().data(0, Qt.ItemDataRole.UserRole) == "selected"


def test_thread_context_menu_disables_folder_action_without_valid_cwd(tmp_path) -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = ThreadListPane()
    pane.set_threads([{"id": "target", "name": "Target"}])

    menu = pane._context_menu_for_item(_thread_item(pane, "target"))

    assert menu.actions()[-1].text() == "作業フォルダを開く"
    assert not menu.actions()[-1].isEnabled()


def test_thread_context_menu_opens_existing_absolute_cwd(tmp_path, monkeypatch) -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = ThreadListPane()
    pane.set_threads([{"id": "target", "cwd": str(tmp_path)}])
    opened: list[QUrl] = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url) or True)

    menu = pane._context_menu_for_item(_thread_item(pane, "target"))
    menu.actions()[-1].trigger()

    assert len(opened) == 1
    assert os.path.normcase(os.path.normpath(opened[0].toLocalFile())) == os.path.normcase(
        os.path.normpath(str(tmp_path))
    )


def test_thread_context_menu_copies_cached_nonexistent_cwd_but_disables_open(
    tmp_path, monkeypatch
) -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = ThreadListPane()
    missing_cwd = str(tmp_path / "not-created")
    pane.set_threads([{"id": "target", "name": "Target", "cwd": missing_cwd}])

    menu = pane._context_menu_for_item(_thread_item(pane, "target"))
    copied: list[str] = []
    monkeypatch.setattr(pane, "_copy_text", copied.append)
    menu.actions()[3].trigger()

    assert len(copied) == 1
    assert f"CWD: {missing_cwd}" in copied[0]
    assert not menu.actions()[-1].isEnabled()


def test_project_parent_context_menu_uses_group_path_and_keeps_action_order(
    tmp_path, monkeypatch
) -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    cwd = tmp_path / "日本語 project with spaces"
    cwd.mkdir()
    pane = ThreadListPane()
    pane.set_threads(
        [
            {"id": "a", "cwd": str(cwd)},
            {"id": "b", "cwd": str(cwd)},
        ]
    )
    parent = pane.list_widget.topLevelItem(0)
    assert parent is not None

    menu = pane._context_menu_for_item(parent)

    assert menu is not None
    assert [action.text() for action in menu.actions()] == [
        "新規スレッドを開始...",
        "",
        "エクスプローラーで開く",
        "フォルダパスをコピー",
        "ターミナルで開く",
    ]
    new_drafts: list[str] = []
    explorer_paths: list[str] = []
    terminal_paths: list[str] = []
    copied_paths: list[str] = []
    pane.new_thread_for_folder_requested.connect(new_drafts.append)
    pane.folder_explorer_requested.connect(explorer_paths.append)
    pane.folder_terminal_requested.connect(terminal_paths.append)
    monkeypatch.setattr(pane, "_copy_text", copied_paths.append)

    menu.actions()[0].trigger()
    menu.actions()[2].trigger()
    menu.actions()[3].trigger()
    menu.actions()[4].trigger()

    assert new_drafts == [str(cwd)]
    assert explorer_paths == [str(cwd)]
    assert copied_paths == [str(cwd)]
    assert terminal_paths == [str(cwd)]


def test_project_parent_context_menu_disables_open_actions_for_missing_folder(
    tmp_path, monkeypatch
) -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    missing_cwd = str(tmp_path / "not-created")
    pane = ThreadListPane()
    pane.set_threads([{"id": "a", "cwd": missing_cwd}])
    parent = pane.list_widget.topLevelItem(0)
    assert parent is not None

    menu = pane._context_menu_for_item(parent)

    assert menu is not None
    assert menu.actions()[0].isEnabled() is False
    assert menu.actions()[2].isEnabled() is False
    assert menu.actions()[3].isEnabled()
    assert menu.actions()[4].isEnabled() is False
    copied: list[str] = []
    monkeypatch.setattr(pane, "_copy_text", copied.append)
    menu.actions()[3].trigger()
    assert copied == [missing_cwd]
