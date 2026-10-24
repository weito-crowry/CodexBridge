from __future__ import annotations

import os
from collections.abc import Collection, Mapping, Sequence

from PySide6.QtCore import QDir, QPoint, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QDesktopServices,
    QFont,
    QHideEvent,
    QIcon,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
    QShowEvent,
)
from PySide6.QtWidgets import (
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .bulk_thread_naming import build_thread_list_header, loaded_untitled_thread_rows
from .external_running_threads import is_codexbridge_originator
from .project_names import normalize_cwd
from .widget_helpers import _safe_text, copy_to_clipboard

_MANAGED_RUNTIME_STATUSES = frozenset(
    {"in_progress", "needs_approval", "needs_input", "needs_user_input", "waiting"}
)
_SPINNER_INTERVAL_MS = 90
_SPINNER_ANGLE_STEP = 30
_SPINNER_SIZE = 16


def _thread_status_marker(status: str | None) -> str:
    if status is None:
        return ""
    return {
        "in_progress": "\u25cf ",
        "needs_approval": "\u23f8 ",
        "needs_input": "\u270e ",
        "needs_user_input": "\u270e ",
        "waiting": "\u23f8 ",
    }.get(status, "")


def _thread_status_label(status: str) -> str:
    return {
        "in_progress": "Running",
        "needs_approval": "Waiting for approval",
        "needs_input": "Waiting for input",
        "needs_user_input": "Waiting for input",
        "waiting": "Waiting",
    }.get(status, status)


class ThreadListPane(QWidget):
    refresh_requested = Signal()
    bulk_thread_naming_requested = Signal()
    thread_selected = Signal(str)
    thread_rename_requested = Signal(str)
    thread_open_requested = Signal(str)
    thread_copy_requested = Signal(str)
    new_thread_for_folder_requested = Signal(str)
    folder_explorer_requested = Signal(str)
    folder_terminal_requested = Signal(str)
    _GROUP_KEY_ROLE = Qt.ItemDataRole.UserRole + 1
    _GROUP_CWD_ROLE = Qt.ItemDataRole.UserRole + 2
    _RUNNING_SECTION_ROLE = Qt.ItemDataRole.UserRole + 3
    _RUNNING_PROJECT_ROLE = Qt.ItemDataRole.UserRole + 4
    _RUNNING_PROJECT_KEY_ROLE = Qt.ItemDataRole.UserRole + 5
    _RUNNING_MARKER_ROLE = Qt.ItemDataRole.UserRole + 6
    _OTHER_GROUP_KEY = "\x00other"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._threads: list[dict[str, object]] = []
        self._server_threads: list[dict[str, object]] = []
        self._external_running_threads: dict[str, dict[str, object]] = {}
        self._active_thread_ids: set[str] = set()
        self._thread_statuses: dict[str, str] = {}
        self._has_thread_statuses = False
        self._project_names: dict[str, str] = {}
        self._collapsed_group_keys: set[str] = set()
        self._collapsed_running_group_keys: set[str] = set()
        self._selected_thread_id: str | None = None
        self._running_section_expanded = True
        self._rendering = False
        self._spinner_angle = 0
        self._spinner_icons: dict[tuple[int, int], QIcon] = {}
        self._running_marker_count = 0
        self._spinner_timer = QTimer(self)
        self._spinner_timer.setInterval(_SPINNER_INTERVAL_MS)
        self._spinner_timer.timeout.connect(self._advance_running_spinner)
        title = QLabel("Threads")
        self.refresh_button = QPushButton("Refresh")
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter threads")
        self.list_widget = QTreeWidget()
        self.list_widget.setObjectName("threadList")
        self.list_widget.setHeaderHidden(True)
        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        self.filter_edit.textChanged.connect(self._render)
        self.list_widget.itemActivated.connect(self._emit_selected)
        self.list_widget.itemClicked.connect(self._emit_selected)
        self.list_widget.currentItemChanged.connect(self._remember_selected_thread)
        self.list_widget.itemCollapsed.connect(self._group_collapsed)
        self.list_widget.itemExpanded.connect(self._group_expanded)
        self.list_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(self._show_context_menu)
        header, self.name_untitled_button = build_thread_list_header(
            title, self.refresh_button, self.bulk_thread_naming_requested.emit
        )
        layout = QVBoxLayout(self)
        layout.addLayout(header)
        layout.addWidget(self.filter_edit)
        layout.addWidget(self.list_widget, 1)

    def set_threads(
        self,
        threads: Sequence[Mapping[str, object]],
        *,
        active_thread_ids: Collection[str] | None = None,
        thread_statuses: Mapping[str, str] | None = None,
        project_names: Mapping[str, str] | None = None,
    ) -> None:
        self._server_threads = [dict(thread) for thread in threads]
        self._rebuild_threads()
        self._project_names = dict(project_names or {})
        if self._selected_thread_id is not None and not any(
            thread.get("id") == self._selected_thread_id for thread in self._threads
        ):
            self._selected_thread_id = None
        if active_thread_ids is not None:
            self._active_thread_ids = set(active_thread_ids)
        if thread_statuses is not None:
            self._thread_statuses = dict(thread_statuses)
            self._has_thread_statuses = True
        self._render()

    def set_external_running_threads(self, threads: Mapping[str, Mapping[str, object]]) -> None:
        self._external_running_threads = {
            thread_id: dict(thread)
            for thread_id, thread in threads.items()
            if isinstance(thread_id, str) and isinstance(thread, Mapping)
        }
        self._rebuild_threads()
        if self._selected_thread_id is not None and not any(
            thread.get("id") == self._selected_thread_id for thread in self._threads
        ):
            self._selected_thread_id = None
        self._render()

    def _rebuild_threads(self) -> None:
        combined: list[dict[str, object]] = []
        included_ids: set[str] = set()
        for server_thread in self._server_threads:
            thread = dict(server_thread)
            thread_id = thread.get("id")
            if isinstance(thread_id, str):
                included_ids.add(thread_id)
                external = self._external_running_threads.get(thread_id)
                if external is not None:
                    for key, value in external.items():
                        current_value = thread.get(key)
                        if not isinstance(current_value, str) or not current_value.strip():
                            thread[key] = value
            combined.append(thread)
        for thread_id, external in self._external_running_threads.items():
            if thread_id in included_ids:
                continue
            thread = dict(external)
            thread["id"] = thread_id
            name = thread.get("name")
            if not isinstance(name, str) or not name.strip():
                thread["name"] = thread_id
            combined.append(thread)
        self._threads = combined

    def _is_external_running_candidate(self, thread_id: str) -> bool:
        external = self._external_running_threads.get(thread_id)
        if (
            external is None
            or self._thread_statuses.get(thread_id) in _MANAGED_RUNTIME_STATUSES
            or is_codexbridge_originator(external.get("originator"))
        ):
            return False
        return not any(
            thread.get("id") == thread_id and is_codexbridge_originator(thread.get("originator"))
            for thread in self._server_threads
        )

    def _running_marker_color(self, marker_kind: str) -> QColor:
        if marker_kind == "external":
            return QColor("#d93025")
        return self.list_widget.palette().color(QPalette.ColorRole.Link)

    def _running_spinner_icon(self, color: QColor, angle: int) -> QIcon | None:
        key = (color.rgba(), angle)
        cached = self._spinner_icons.get(key)
        if cached is not None:
            return cached

        pixmap = QPixmap(_SPINNER_SIZE, _SPINNER_SIZE)
        if pixmap.isNull():
            return None
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter()
        if not painter.begin(pixmap):
            return None
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(color)
        pen.setWidthF(2.0)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(QRectF(3.0, 3.0, 10.0, 10.0), angle * 16, 250 * 16)
        painter.end()
        icon = QIcon(pixmap)
        if icon.isNull():
            return None
        self._spinner_icons[key] = icon
        return icon

    def _sync_running_spinner_timer(self) -> None:
        should_run = self._running_marker_count > 0 and self.isVisible() and not self._rendering
        if should_run:
            if not self._spinner_timer.isActive():
                self._spinner_timer.start()
        else:
            self._spinner_timer.stop()

    def _refresh_running_spinner_item(self, item: QTreeWidgetItem) -> int:
        marker_kind = item.data(0, self._RUNNING_MARKER_ROLE)
        if marker_kind not in {"managed", "external"}:
            return 0
        color = self._running_marker_color(marker_kind)
        icon = self._running_spinner_icon(color, self._spinner_angle)
        if icon is None:
            title = item.text(0)
            if not title.startswith("\u25cf "):
                item.setText(0, f"\u25cf {title}")
            item.setIcon(0, QIcon())
            item.setData(0, self._RUNNING_MARKER_ROLE, None)
            item.setForeground(0, QBrush(color))
            return 0
        item.setIcon(0, icon)
        item.setForeground(0, QBrush(color))
        return 1

    def _refresh_running_spinner_items(self) -> None:
        marker_count = 0

        def visit(item: QTreeWidgetItem) -> None:
            nonlocal marker_count
            marker_count += self._refresh_running_spinner_item(item)
            for child_index in range(item.childCount()):
                child = item.child(child_index)
                if child is not None:
                    visit(child)

        for top_index in range(self.list_widget.topLevelItemCount()):
            item = self.list_widget.topLevelItem(top_index)
            if item is not None:
                visit(item)
        self._running_marker_count = marker_count

    def _advance_running_spinner(self) -> None:
        if self._rendering or not self.isVisible() or self._running_marker_count == 0:
            return
        self._spinner_angle = (self._spinner_angle + _SPINNER_ANGLE_STEP) % 360
        self._refresh_running_spinner_items()
        self._sync_running_spinner_timer()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._refresh_running_spinner_items()
        self._sync_running_spinner_timer()

    def hideEvent(self, event: QHideEvent) -> None:
        self._spinner_timer.stop()
        super().hideEvent(event)

    def set_active_thread_ids(self, thread_ids: Collection[str]) -> None:
        self._active_thread_ids = set(thread_ids)
        self._render()

    def set_thread_statuses(self, thread_statuses: Mapping[str, str]) -> None:
        self._thread_statuses = dict(thread_statuses)
        self._has_thread_statuses = True
        self._render()

    @property
    def thread_count(self) -> int:
        return len(self._threads)

    def loaded_untitled_threads(self) -> list[dict[str, object]]:
        return loaded_untitled_thread_rows(self._threads, self._project_names)

    def _render(self) -> None:
        query = self.filter_edit.text().casefold()
        scroll_value = self.list_widget.verticalScrollBar().value()
        current_item = self.list_widget.currentItem()
        selected_in_running_section = self._is_running_section_item(current_item)
        if current_item is not None:
            self._remember_selected_thread(current_item, None)
        selected_thread_id = self._selected_thread_id
        groups: dict[str | None, list[dict[str, object]]] = {}
        group_cwds: dict[str | None, str | None] = {}
        for thread in self._threads:
            thread_id = thread.get("id")
            if not isinstance(thread_id, str):
                continue
            raw_cwd = thread.get("cwd")
            cwd = raw_cwd if isinstance(raw_cwd, str) else None
            group_key = normalize_cwd(raw_cwd)
            if group_key not in groups:
                groups[group_key] = []
                group_cwds[group_key] = cwd if group_key is not None else None
            groups[group_key].append(thread)

        self._running_marker_count = 0
        self._rendering = True
        try:
            self.list_widget.clear()
            selected_item: QTreeWidgetItem | None = None
            fallback_selected_item: QTreeWidgetItem | None = None

            def project_label(group_key: str | None, group_cwd: str | None) -> str:
                friendly_name = (
                    self._project_names.get(group_key) if group_key is not None else None
                )
                basename = (
                    os.path.basename(os.path.normpath(group_cwd)) or group_cwd
                    if group_cwd is not None
                    else "Other"
                )
                if isinstance(friendly_name, str) and friendly_name.strip():
                    friendly_name = friendly_name.strip()
                    if friendly_name.casefold() == basename.casefold():
                        return friendly_name
                    return f"{friendly_name} — {basename}"
                return basename

            def matching_group_threads(
                group_key: str | None, group_threads: list[dict[str, object]]
            ) -> list[dict[str, object]]:
                label = project_label(group_key, group_cwds[group_key])
                return [
                    thread
                    for thread in group_threads
                    if not query
                    or query
                    in (
                        " ".join(
                            str(thread.get(key, "")) for key in ("id", "name", "preview", "cwd")
                        )
                        + " "
                        + label
                    ).casefold()
                ]

            def append_group(
                destination: QTreeWidgetItem | None,
                group_key: str | None,
                matching_threads: list[dict[str, object]],
                *,
                running: bool,
            ) -> None:
                nonlocal selected_item, fallback_selected_item
                group_cwd = group_cwds[group_key]
                state_key = group_key if group_key is not None else self._OTHER_GROUP_KEY
                parent = QTreeWidgetItem([project_label(group_key, group_cwd)])
                parent.setData(0, Qt.ItemDataRole.UserRole, None)
                if running:
                    parent.setData(0, self._RUNNING_PROJECT_ROLE, True)
                    parent.setData(0, self._RUNNING_PROJECT_KEY_ROLE, state_key)
                else:
                    parent.setData(0, self._GROUP_KEY_ROLE, state_key)
                    if group_cwd is not None:
                        parent.setToolTip(0, group_cwd)
                        parent.setData(0, self._GROUP_CWD_ROLE, group_cwd)
                if destination is None:
                    self.list_widget.addTopLevelItem(parent)
                else:
                    destination.addChild(parent)

                for thread in matching_threads:
                    thread_id = thread.get("id")
                    if not isinstance(thread_id, str):
                        continue
                    title = _safe_text(thread.get("name")) or "New スレッド"
                    status = self._thread_statuses.get(thread_id)
                    external_running = self._is_external_running_candidate(thread_id)
                    managed_running = status == "in_progress" or (
                        not self._has_thread_statuses and thread_id in self._active_thread_ids
                    )
                    marker_kind = (
                        "managed" if managed_running else "external" if external_running else None
                    )
                    status_marker = "" if marker_kind is not None else _thread_status_marker(status)
                    item = QTreeWidgetItem([f"{status_marker}{title}" if status_marker else title])
                    item.setData(0, Qt.ItemDataRole.UserRole, thread_id)
                    item.setToolTip(0, thread_id)
                    if marker_kind is not None:
                        color = self._running_marker_color(marker_kind)
                        icon = self._running_spinner_icon(color, self._spinner_angle)
                        if icon is None:
                            item.setText(0, f"\u25cf {title}")
                        else:
                            item.setIcon(0, icon)
                            item.setData(0, self._RUNNING_MARKER_ROLE, marker_kind)
                            self._running_marker_count += 1
                        font = QFont(item.font(0))
                        font.setWeight(QFont.Weight.DemiBold)
                        item.setFont(0, font)
                        item.setForeground(0, QBrush(color))
                    if marker_kind == "external":
                        item.setToolTip(0, f"{thread_id}\n推定実行中")
                    elif status_marker:
                        font = QFont(item.font(0))
                        font.setWeight(QFont.Weight.DemiBold)
                        item.setFont(0, font)
                        marker_color = self.list_widget.palette().color(QPalette.ColorRole.Link)
                        item.setForeground(0, QBrush(marker_color))
                    parent.addChild(item)
                    if thread_id == selected_thread_id:
                        if fallback_selected_item is None:
                            fallback_selected_item = item
                        if running == selected_in_running_section:
                            selected_item = item

                if running:
                    parent.setExpanded(
                        bool(query) or state_key not in self._collapsed_running_group_keys
                    )
                else:
                    parent.setExpanded(bool(query) or state_key not in self._collapsed_group_keys)

            running_groups: dict[str | None, list[dict[str, object]]] = {}
            for group_key, group_threads in groups.items():
                running_threads = [
                    thread
                    for thread in group_threads
                    if isinstance(thread.get("id"), str)
                    and (
                        self._thread_statuses.get(str(thread.get("id"))) == "in_progress"
                        or self._is_external_running_candidate(str(thread.get("id")))
                    )
                ]
                if running_threads:
                    running_groups[group_key] = running_threads

            matching_running_groups = [
                (group_key, matching_group_threads(group_key, group_threads))
                for group_key, group_threads in running_groups.items()
            ]
            matching_running_groups = [
                (group_key, matching_threads)
                for group_key, matching_threads in matching_running_groups
                if matching_threads
            ]
            if matching_running_groups:
                running_section = QTreeWidgetItem(["現在処理中"])
                running_section.setData(0, Qt.ItemDataRole.UserRole, None)
                running_section.setData(0, self._RUNNING_SECTION_ROLE, True)
                self.list_widget.addTopLevelItem(running_section)
                running_section.setExpanded(bool(query) or self._running_section_expanded)
                for group_key, matching_threads in matching_running_groups:
                    append_group(running_section, group_key, matching_threads, running=True)

            for group_key, group_threads in groups.items():
                matching_threads = matching_group_threads(group_key, group_threads)
                if not matching_threads:
                    continue
                append_group(None, group_key, matching_threads, running=False)
            if selected_item is None:
                selected_item = fallback_selected_item
            if selected_item is not None:
                self.list_widget.setCurrentItem(selected_item)
            self.list_widget.verticalScrollBar().setValue(scroll_value)
        finally:
            self._rendering = False
            self._sync_running_spinner_timer()

    def _remember_selected_thread(
        self, current: QTreeWidgetItem | None, _previous: QTreeWidgetItem | None
    ) -> None:
        if current is None:
            return
        thread_id = current.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(thread_id, str):
            self._selected_thread_id = (
                thread_id if self._thread_for_id(thread_id) is not None else None
            )

    def _group_collapsed(self, item: QTreeWidgetItem) -> None:
        if self._rendering:
            return
        if item.data(0, self._RUNNING_SECTION_ROLE):
            self._running_section_expanded = False
            return
        if item.data(0, self._RUNNING_PROJECT_ROLE):
            group_key = item.data(0, self._RUNNING_PROJECT_KEY_ROLE)
            if not isinstance(group_key, str):
                return
            if self.filter_edit.text():
                item.setExpanded(True)
                return
            self._collapsed_running_group_keys.add(group_key)
            return
        group_key = item.data(0, self._GROUP_KEY_ROLE)
        if not isinstance(group_key, str):
            return
        if self.filter_edit.text():
            item.setExpanded(True)
            return
        self._collapsed_group_keys.add(group_key)

    def _group_expanded(self, item: QTreeWidgetItem) -> None:
        if self._rendering or self.filter_edit.text():
            return
        if item.data(0, self._RUNNING_SECTION_ROLE):
            self._running_section_expanded = True
            return
        if item.data(0, self._RUNNING_PROJECT_ROLE):
            group_key = item.data(0, self._RUNNING_PROJECT_KEY_ROLE)
            if isinstance(group_key, str):
                self._collapsed_running_group_keys.discard(group_key)
            return
        group_key = item.data(0, self._GROUP_KEY_ROLE)
        if isinstance(group_key, str):
            self._collapsed_group_keys.discard(group_key)

    def _emit_selected(self, item: QTreeWidgetItem, _column: int = 0) -> None:
        thread_id = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(thread_id, str):
            self._selected_thread_id = thread_id
            self.thread_selected.emit(thread_id)

    def _thread_for_id(self, thread_id: str) -> dict[str, object] | None:
        for thread in self._threads:
            if thread.get("id") == thread_id:
                return thread
        return None

    def thread_name(self, thread_id: str) -> str:
        thread = self._thread_for_id(thread_id)
        return _safe_text(thread.get("name")) if thread is not None else ""

    def update_thread_name(self, thread_id: str, name: str) -> None:
        thread = self._thread_for_id(thread_id)
        if thread is None:
            return
        thread["name"] = name
        self._render()

    @staticmethod
    def _cached_cwd(value: object) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        return value

    @staticmethod
    def _openable_cwd(value: object) -> str | None:
        cwd = ThreadListPane._cached_cwd(value)
        if cwd is None:
            return None
        if not QDir.isAbsolutePath(cwd) or not QDir(cwd).exists():
            return None
        return cwd

    def _thread_info_text(self, thread_id: str) -> str | None:
        thread = self._thread_for_id(thread_id)
        if thread is None:
            return None
        lines = [
            f"Name: {_safe_text(thread.get('name')) or 'New スレッド'}",
            f"Thread ID: {thread_id}",
        ]
        cwd = self._cached_cwd(thread.get("cwd"))
        if cwd is not None:
            lines.append(f"CWD: {cwd}")
        status = self._thread_statuses.get(thread_id)
        if status is not None:
            lines.append(f"Turn status: {_thread_status_label(status)}")
        elif not self._has_thread_statuses:
            lines.append(
                f"Status: {'active' if thread_id in self._active_thread_ids else 'inactive'}"
            )
        return "\n".join(lines)

    @staticmethod
    def _copy_text(text: str) -> None:
        copy_to_clipboard(text)

    def _open_cwd(self, thread_id: str) -> None:
        thread = self._thread_for_id(thread_id)
        cwd = self._openable_cwd(thread.get("cwd") if thread is not None else None)
        if cwd is None:
            return
        try:
            QDesktopServices.openUrl(QUrl.fromLocalFile(cwd))
        except Exception:
            return

    def _context_menu_for_item(self, item: QTreeWidgetItem) -> QMenu | None:
        if item.data(0, self._RUNNING_SECTION_ROLE) or item.data(0, self._RUNNING_PROJECT_ROLE):
            return None
        thread_id = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(thread_id, str):
            cwd = item.data(0, self._GROUP_CWD_ROLE)
            if isinstance(cwd, str) and cwd.strip():
                return self._project_context_menu(cwd)
            return None
        menu = QMenu(self)

        rename_action = menu.addAction("名前を変更...")
        rename_action.triggered.connect(
            lambda _checked=False, thread_id=thread_id: self.thread_rename_requested.emit(thread_id)
        )
        open_codex_action = menu.addAction("Open in Codex App")
        open_codex_action.triggered.connect(
            lambda _checked=False, thread_id=thread_id: self.thread_open_requested.emit(thread_id)
        )
        copy_id_action = menu.addAction("スレッドIDをコピー")
        copy_id_action.triggered.connect(
            lambda _checked=False, thread_id=thread_id: self._copy_text(thread_id)
        )
        copy_info_action = menu.addAction("スレッド情報をコピー")
        copy_info_action.triggered.connect(
            lambda _checked=False, thread_id=thread_id: self._copy_text(
                self._thread_info_text(thread_id) or ""
            )
        )
        copy_content_action = menu.addAction("Copy thread content")
        copy_content_action.triggered.connect(
            lambda _checked=False, thread_id=thread_id: self.thread_copy_requested.emit(thread_id)
        )
        menu.addSeparator()
        open_cwd_action = menu.addAction("作業フォルダを開く")
        thread = self._thread_for_id(thread_id)
        open_cwd_action.setEnabled(
            self._openable_cwd(thread.get("cwd") if thread is not None else None) is not None
        )
        open_cwd_action.triggered.connect(
            lambda _checked=False, thread_id=thread_id: self._open_cwd(thread_id)
        )
        return menu

    @classmethod
    def _is_running_section_item(cls, item: QTreeWidgetItem | None) -> bool:
        current = item
        while current is not None:
            if current.data(0, cls._RUNNING_SECTION_ROLE):
                return True
            current = current.parent()
        return False

    def _project_context_menu(self, cwd: str) -> QMenu:
        menu = QMenu(self)
        openable = self._openable_cwd(cwd) is not None

        new_thread_action = menu.addAction("新規スレッドを開始...")
        new_thread_action.setEnabled(openable)
        new_thread_action.triggered.connect(
            lambda _checked=False, cwd=cwd: self.new_thread_for_folder_requested.emit(cwd)
        )
        menu.addSeparator()

        explorer_action = menu.addAction("エクスプローラーで開く")
        explorer_action.setEnabled(openable)
        explorer_action.triggered.connect(
            lambda _checked=False, cwd=cwd: self.folder_explorer_requested.emit(cwd)
        )
        copy_path_action = menu.addAction("フォルダパスをコピー")
        copy_path_action.triggered.connect(lambda _checked=False, cwd=cwd: self._copy_text(cwd))
        terminal_action = menu.addAction("ターミナルで開く")
        terminal_action.setEnabled(openable)
        terminal_action.triggered.connect(
            lambda _checked=False, cwd=cwd: self.folder_terminal_requested.emit(cwd)
        )
        return menu

    def _show_context_menu(self, position: QPoint) -> None:
        item = self.list_widget.itemAt(position)
        if item is None:
            return
        menu = self._context_menu_for_item(item)
        if menu is None:
            return
        menu.exec(self.list_widget.viewport().mapToGlobal(position))

    def set_empty_state(self, text: str) -> None:
        self._selected_thread_id = None
        self.list_widget.clear()
        item = QTreeWidgetItem([text])
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable & ~Qt.ItemFlag.ItemIsEnabled)
        self.list_widget.addTopLevelItem(item)
