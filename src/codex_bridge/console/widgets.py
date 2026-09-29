from __future__ import annotations

import os
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from math import ceil
from typing import Any

from PySide6.QtCore import QDir, QPoint, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QBrush, QDesktopServices, QFont, QPalette, QResizeEvent, QTextOption
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .project_names import normalize_cwd
from .usage import CodexUsageSnapshot, format_codex_usage_snapshot


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    turn_id: str
    item_id: str
    kind: str
    title: str
    body: str
    status: str | None
    details: tuple[str, ...]


def _safe_text(value: object, limit: int = 16_384) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _safe_status(value: object) -> str | None:
    return value if isinstance(value, str) and len(value) <= 128 else None


def _model_summary(metadata: Mapping[str, object] | None) -> str:
    if not isinstance(metadata, Mapping):
        return "Model: unavailable"
    raw_candidates = metadata.get("model_candidates")
    if not isinstance(raw_candidates, list):
        return "Model: unavailable"
    candidates: list[str] = []
    for raw_candidate in raw_candidates:
        if not isinstance(raw_candidate, Mapping):
            continue
        model = raw_candidate.get("model")
        if not isinstance(model, str) or not model:
            continue
        effort = raw_candidate.get("reasoning_effort")
        rendered = model[:512]
        if isinstance(effort, str) and effort:
            rendered += f" ({effort[:512]})"
        candidates.append(rendered)
    status = metadata.get("model_resolution_status")
    if status == "unavailable" or not candidates:
        return "Model: unavailable"
    label = "Models" if status == "multiple" or len(candidates) > 1 else "Model"
    return f"{label}: {' / '.join(candidates)}"


def _turn_header(status: object, metadata: Mapping[str, object] | None) -> str:
    parts = ["Turn"]
    safe_status = _safe_status(status)
    if safe_status:
        parts.append(safe_status)
    parts.append(_model_summary(metadata))
    return " · ".join(parts)


def _entry(turn_id: str, item: Mapping[str, Any]) -> TimelineEntry | None:
    item_id = item.get("id")
    item_type = item.get("type")
    if not isinstance(item_id, str) or not isinstance(item_type, str):
        return None
    status = _safe_status(item.get("status"))
    if item_type == "userMessage":
        return TimelineEntry(
            turn_id, item_id, "User", "User", _safe_text(item.get("text")), None, ()
        )
    if item_type == "agentMessage":
        if item.get("phase") == "commentary":
            return TimelineEntry(
                turn_id,
                item_id,
                "Commentary",
                "Commentary",
                _safe_text(item.get("text")),
                None,
                (),
            )
        return TimelineEntry(
            turn_id, item_id, "Agent", "Agent", _safe_text(item.get("text")), None, ()
        )
    if item_type == "plan":
        return TimelineEntry(
            turn_id, item_id, "Plan", "Plan", _safe_text(item.get("text")), None, ()
        )
    if item_type == "commandExecution":
        details: list[str] = []
        exit_code = item.get("exit_code")
        if isinstance(exit_code, int) and not isinstance(exit_code, bool):
            details.append(f"exit {exit_code}")
        duration = item.get("duration_ms")
        if isinstance(duration, int) and not isinstance(duration, bool):
            details.append(f"{duration}ms")
        return TimelineEntry(
            turn_id,
            item_id,
            "Command",
            "Command",
            _safe_text(item.get("command")),
            status,
            tuple(details),
        )
    if item_type == "fileChange":
        paths = item.get("paths")
        safe_paths = (
            tuple(path for path in paths[:100] if isinstance(path, str))
            if isinstance(paths, list)
            else ()
        )
        return TimelineEntry(turn_id, item_id, "Files", "Files", "\n".join(safe_paths), status, ())
    if item_type == "mcpToolCall":
        server = _safe_text(item.get("server"), 512)
        tool = _safe_text(item.get("tool"), 512)
        return TimelineEntry(
            turn_id, item_id, "MCP", "MCP", f"{server} / {tool}".strip(" /"), status, ()
        )
    if item_type == "dynamicToolCall":
        namespace = _safe_text(item.get("namespace"), 512)
        tool = _safe_text(item.get("tool"), 512)
        tool_details: tuple[str, ...] = (namespace,) if namespace else ()
        return TimelineEntry(
            turn_id, item_id, "Dynamic tool", "Dynamic tool", tool, status, tool_details
        )
    if item_type == "functionCallOutput":
        name = _safe_text(item.get("name"), 512) or _safe_text(item.get("namespace"), 512)
        return TimelineEntry(turn_id, item_id, "Function", "Function", name, None, ())
    if item_type == "collabAgentToolCall":
        return TimelineEntry(
            turn_id,
            item_id,
            "Collaboration",
            "Collaboration",
            _safe_text(item.get("tool")),
            status,
            (),
        )
    if item_type == "subAgentActivity":
        return TimelineEntry(
            turn_id, item_id, "Sub-agent", "Sub-agent", _safe_text(item.get("kind")), None, ()
        )
    if item_type == "imageView":
        return TimelineEntry(
            turn_id, item_id, "Image", "Image", _safe_text(item.get("path")), None, ()
        )
    if item_type in {"contextCompaction", "context_compaction"}:
        return TimelineEntry(turn_id, item_id, "Context", "Context", "Compaction", None, ())
    if item_type in {"enteredReviewMode", "exitedReviewMode"}:
        return TimelineEntry(turn_id, item_id, "Review", "Review", item_type, None, ())
    return None


def timeline_entries(items_payload: Mapping[str, object]) -> tuple[TimelineEntry, ...]:
    raw_items = items_payload.get("items")
    if not isinstance(raw_items, list):
        return ()
    entries: list[TimelineEntry] = []
    seen: set[tuple[str, str]] = set()
    for raw_entry in reversed(raw_items):
        if not isinstance(raw_entry, Mapping) or not isinstance(raw_entry.get("turn_id"), str):
            continue
        item = raw_entry.get("item")
        if not isinstance(item, Mapping):
            continue
        projected = _entry(raw_entry["turn_id"], item)
        if projected is None or (projected.turn_id, projected.item_id) in seen:
            continue
        seen.add((projected.turn_id, projected.item_id))
        entries.append(projected)
    return tuple(entries)


def format_thread_content(entries: Sequence[TimelineEntry]) -> str:
    return "\n\n".join(f"{entry.title}:\n{entry.body}" for entry in entries)


def copy_to_clipboard(text: str) -> None:
    application = QApplication.instance()
    if isinstance(application, QApplication):
        application.clipboard().setText(text)


def activity_row(activity: Mapping[str, object]) -> str:
    timestamp = _safe_text(activity.get("timestamp"), 128)
    activity_type = _safe_text(activity.get("type"), 128)
    activity_type = {
        "agent_commentary": "Commentary",
        "agent_message": "Agent",
    }.get(activity_type, activity_type)
    status = _safe_text(activity.get("status"), 128)
    summary = _safe_text(activity.get("summary"), 2_000)
    parts = [part for part in (timestamp, activity_type, status, summary) if part]
    details = activity.get("details")
    if isinstance(details, Mapping):
        exit_code = details.get("exit_code")
        if isinstance(exit_code, int) and not isinstance(exit_code, bool):
            parts.append(f"exit {exit_code}")
        paths = details.get("paths")
        if isinstance(paths, list):
            safe_paths = [path for path in paths[:100] if isinstance(path, str)]
            if safe_paths:
                parts.append("paths: " + ", ".join(safe_paths))
        decision = details.get("decision")
        if isinstance(decision, str):
            parts.append(decision[:256])
    return " · ".join(parts)


class ThreadListPane(QWidget):
    refresh_requested = Signal()
    thread_selected = Signal(str)
    thread_rename_requested = Signal(str)
    thread_open_requested = Signal(str)
    thread_copy_requested = Signal(str)
    _GROUP_KEY_ROLE = Qt.ItemDataRole.UserRole + 1
    _OTHER_GROUP_KEY = "\x00other"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._threads: list[dict[str, object]] = []
        self._active_thread_ids: set[str] = set()
        self._project_names: dict[str, str] = {}
        self._collapsed_group_keys: set[str] = set()
        self._selected_thread_id: str | None = None
        self._rendering = False
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
        header = QHBoxLayout()
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self.refresh_button)
        layout = QVBoxLayout(self)
        layout.addLayout(header)
        layout.addWidget(self.filter_edit)
        layout.addWidget(self.list_widget, 1)

    def set_threads(
        self,
        threads: Sequence[Mapping[str, object]],
        *,
        active_thread_ids: Collection[str] | None = None,
        project_names: Mapping[str, str] | None = None,
    ) -> None:
        self._threads = [dict(thread) for thread in threads]
        self._project_names = dict(project_names or {})
        if self._selected_thread_id is not None and not any(
            thread.get("id") == self._selected_thread_id for thread in self._threads
        ):
            self._selected_thread_id = None
        if active_thread_ids is not None:
            self._active_thread_ids = set(active_thread_ids)
        self._render()

    def set_active_thread_ids(self, thread_ids: Collection[str]) -> None:
        self._active_thread_ids = set(thread_ids)
        self._render()

    @property
    def thread_count(self) -> int:
        return len(self._threads)

    def _render(self) -> None:
        query = self.filter_edit.text().casefold()
        current_item = self.list_widget.currentItem()
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
                group_cwds[group_key] = cwd
            groups[group_key].append(thread)

        self._rendering = True
        try:
            self.list_widget.clear()
            selected_item: QTreeWidgetItem | None = None
            for group_key, group_threads in groups.items():
                group_cwd = group_cwds[group_key]
                friendly_name = (
                    self._project_names.get(group_key) if group_key is not None else None
                )
                basename = (
                    os.path.basename(os.path.normpath(group_cwd)) or group_cwd
                    if group_cwd is not None
                    else "Other"
                )
                label = basename
                if isinstance(friendly_name, str) and friendly_name.strip():
                    friendly_name = friendly_name.strip()
                    if friendly_name.casefold() == basename.casefold():
                        label = friendly_name
                    else:
                        label = f"{friendly_name} — {basename}"

                matching_threads: list[dict[str, object]] = []
                for thread in group_threads:
                    searchable = " ".join(
                        str(thread.get(key, "")) for key in ("id", "name", "preview", "cwd")
                    )
                    if query and query not in f"{searchable} {label}".casefold():
                        continue
                    matching_threads.append(thread)
                if not matching_threads:
                    continue

                parent = QTreeWidgetItem([label])
                parent.setData(0, Qt.ItemDataRole.UserRole, None)
                state_key = group_key if group_key is not None else self._OTHER_GROUP_KEY
                parent.setData(0, self._GROUP_KEY_ROLE, state_key)
                if group_cwd is not None:
                    parent.setToolTip(0, group_cwd)
                self.list_widget.addTopLevelItem(parent)

                for thread in matching_threads:
                    thread_id = thread.get("id")
                    if not isinstance(thread_id, str):
                        continue
                    title = _safe_text(thread.get("name")) or "New スレッド"
                    is_active = thread_id in self._active_thread_ids
                    item = QTreeWidgetItem([f"\u25cf {title}" if is_active else title])
                    item.setData(0, Qt.ItemDataRole.UserRole, thread_id)
                    item.setToolTip(0, thread_id)
                    if is_active:
                        font = QFont(item.font(0))
                        font.setWeight(QFont.Weight.DemiBold)
                        item.setFont(0, font)
                        item.setForeground(
                            0,
                            QBrush(self.list_widget.palette().color(QPalette.ColorRole.Link)),
                        )
                    parent.addChild(item)
                    if thread_id == selected_thread_id:
                        selected_item = item

                collapsed = state_key in self._collapsed_group_keys
                parent.setExpanded(bool(query) or not collapsed)
            if selected_item is not None:
                self.list_widget.setCurrentItem(selected_item)
        finally:
            self._rendering = False

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
        lines.append(f"Status: {'active' if thread_id in self._active_thread_ids else 'inactive'}")
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
        thread_id = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(thread_id, str):
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


class _HistoryContent(QWidget):
    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        layout = self.layout()
        if layout is None or self.width() <= 0:
            return QSize(0, hint.height())
        height = (
            layout.heightForWidth(self.width()) if layout.hasHeightForWidth() else hint.height()
        )
        return QSize(0, height)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.updateGeometry()


class _HistoryBody(QTextBrowser):
    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setReadOnly(True)
        self.setPlainText(text)
        self.document().setDocumentMargin(0)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAnywhere)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        policy = self.sizePolicy()
        policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
        policy.setVerticalPolicy(QSizePolicy.Policy.Minimum)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        available_width = max(1, width - self.frameWidth() * 2)
        self.document().setTextWidth(available_width)
        document_height = self.document().documentLayout().documentSize().height()
        return max(1, ceil(document_height) + self.frameWidth() * 2)


class HistoryPane(QWidget):
    older_requested = Signal()
    _BOTTOM_FOLLOW_THRESHOLD = 20
    _TOP_THRESHOLD = 2

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.load_older_button = QPushButton("Load older")
        self.load_older_button.clicked.connect(self.older_requested.emit)
        self.load_older_button.hide()
        self._empty_label = QLabel("Select a thread to view history.")
        self._empty_label.setWordWrap(True)
        self._content = _HistoryContent()
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setWidget(self._content)
        self._follow_newest = True
        self._has_older = False
        self._scroll_update_pending = False
        self._programmatic_scroll = False
        self._render_generation = 0
        self._scroll_restore_state: tuple[int, int, int, bool, bool, bool] | None = None
        self._scroll_restore_stage = 0
        self._scroll_restore_timer = QTimer(self)
        self._scroll_restore_timer.setSingleShot(True)
        self._scroll_restore_timer.timeout.connect(self._advance_timeline_scroll_restore)
        self._scroll.verticalScrollBar().valueChanged.connect(self._on_scroll_value_changed)
        layout = QVBoxLayout(self)
        layout.addWidget(self.load_older_button)
        layout.addWidget(self._empty_label)
        layout.addWidget(self._scroll, 1)

    def _clear_cards(self) -> None:
        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            if item is None:
                continue
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def set_timeline(
        self,
        entries: Sequence[TimelineEntry],
        *,
        has_older: bool = False,
        turn_statuses: Mapping[str, str] | None = None,
        turn_model_metadata: Mapping[str, Mapping[str, object]] | None = None,
        turn_usage_snapshots: Mapping[str, CodexUsageSnapshot] | None = None,
        prepend: bool = False,
    ) -> None:
        scrollbar = self._scroll.verticalScrollBar()
        old_value = scrollbar.value()
        old_maximum = scrollbar.maximum()
        should_follow = self._follow_newest
        self._render_generation += 1
        generation = self._render_generation
        self._scroll_update_pending = True
        self._has_older = has_older
        if not prepend:
            self._update_load_older_visibility()

        self._clear_cards()
        previous_turn: str | None = None
        for entry in entries:
            if previous_turn is None or previous_turn != entry.turn_id:
                turn_status = turn_statuses.get(entry.turn_id) if turn_statuses else None
                metadata = turn_model_metadata.get(entry.turn_id) if turn_model_metadata else None
                separator = QLabel(_turn_header(turn_status, metadata))
                separator.setObjectName("turnSeparator")
                self._content_layout.addWidget(separator)
                snapshot = turn_usage_snapshots.get(entry.turn_id) if turn_usage_snapshots else None
                if snapshot is not None:
                    snapshot_label = QLabel(format_codex_usage_snapshot(snapshot))
                    snapshot_label.setObjectName("turnUsageSnapshot")
                    snapshot_label.setWordWrap(True)
                    snapshot_policy = snapshot_label.sizePolicy()
                    snapshot_policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
                    snapshot_label.setSizePolicy(snapshot_policy)
                    self._content_layout.addWidget(snapshot_label)
            self._content_layout.addWidget(self._card(entry))
            previous_turn = entry.turn_id
        self._empty_label.setVisible(not entries)
        self._scroll.setVisible(bool(entries))
        if not entries:
            should_follow = True
            self._follow_newest = True
        self._content.updateGeometry()
        self._scroll_restore_state = (
            generation,
            old_value,
            old_maximum,
            should_follow,
            prepend,
            bool(entries),
        )
        self._scroll_restore_stage = 0
        self._scroll_restore_timer.start(0)

    def _advance_timeline_scroll_restore(self) -> None:
        state = self._scroll_restore_state
        if state is None:
            return
        generation, old_value, old_maximum, should_follow, prepend, has_entries = state
        if generation != self._render_generation:
            return
        if self._scroll_restore_stage == 0:
            self._scroll_restore_stage = 1
            self._scroll_restore_timer.start(0)
            return
        if self._scroll_restore_stage == 1:
            self._restore_timeline_scroll(
                generation,
                old_value,
                old_maximum,
                should_follow,
                prepend,
                has_entries,
            )
            self._scroll_restore_stage = 2
            self._scroll_restore_timer.start(0)
            return
        self._finish_timeline_scroll(generation, should_follow, prepend, has_entries)
        self._scroll_restore_state = None
        self._scroll_restore_stage = 0

    def _restore_timeline_scroll(
        self,
        generation: int,
        old_value: int,
        old_maximum: int,
        should_follow: bool,
        prepend: bool,
        has_entries: bool,
    ) -> None:
        if generation != self._render_generation:
            return
        scrollbar = self._scroll.verticalScrollBar()
        if prepend:
            if old_value <= self._TOP_THRESHOLD and old_maximum <= self._TOP_THRESHOLD:
                target = 0
            else:
                target = old_value + (scrollbar.maximum() - old_maximum)
        elif should_follow:
            target = scrollbar.maximum()
        else:
            target = old_value
        self._set_programmatic_scroll(target)
        if prepend:
            self._update_load_older_visibility()
        QTimer.singleShot(
            0,
            lambda: self._finish_timeline_scroll(generation, should_follow, prepend, has_entries),
        )

    def _finish_timeline_scroll(
        self,
        generation: int,
        should_follow: bool,
        prepend: bool,
        has_entries: bool,
    ) -> None:
        if generation != self._render_generation:
            return
        if not prepend and should_follow and has_entries:
            self._set_programmatic_scroll(self._scroll.verticalScrollBar().maximum())
            self._follow_newest = True
        elif not has_entries:
            self._follow_newest = True
        self._update_load_older_visibility()
        self._scroll_update_pending = False

    def _set_programmatic_scroll(self, value: int) -> None:
        scrollbar = self._scroll.verticalScrollBar()
        self._programmatic_scroll = True
        scrollbar.setValue(max(scrollbar.minimum(), min(value, scrollbar.maximum())))
        self._programmatic_scroll = False

    def _on_scroll_value_changed(self, value: int) -> None:
        if self._scroll_update_pending or self._programmatic_scroll:
            return
        scrollbar = self._scroll.verticalScrollBar()
        self._follow_newest = scrollbar.maximum() - value <= self._BOTTOM_FOLLOW_THRESHOLD
        self._update_load_older_visibility()

    def _update_load_older_visibility(self) -> None:
        scrollbar = self._scroll.verticalScrollBar()
        at_top = scrollbar.value() <= self._TOP_THRESHOLD
        self.load_older_button.setVisible(self._has_older and at_top)

    def _card(self, entry: TimelineEntry) -> QWidget:
        card = QFrame()
        card.setObjectName("historyCard")
        card.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(card)
        header_parts = [entry.title]
        if entry.status is not None:
            header_parts.append(entry.status)
        header = QHBoxLayout()
        header_label = QLabel(" · ".join(header_parts))
        header_label.setWordWrap(True)
        header_policy = header_label.sizePolicy()
        header_policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
        header_label.setSizePolicy(header_policy)
        header.addWidget(header_label)
        if entry.kind in {"User", "Agent"}:
            header.addStretch(1)
            copy_button = QPushButton("Copy")
            copy_button.setObjectName("copyMessageButton")
            copy_button.clicked.connect(
                lambda _checked=False, text=entry.body: copy_to_clipboard(text)
            )
            header.addWidget(copy_button)
        layout.addLayout(header)
        if entry.body:
            layout.addWidget(_HistoryBody(entry.body))
        if entry.details:
            layout.addWidget(_HistoryBody(" · ".join(entry.details)))
        return card

    def set_empty_state(self, text: str) -> None:
        self._render_generation += 1
        self._scroll_restore_timer.stop()
        self._scroll_restore_state = None
        self._scroll_restore_stage = 0
        self._scroll_update_pending = False
        self._follow_newest = True
        self._has_older = False
        self._set_programmatic_scroll(0)
        self._clear_cards()
        self._empty_label.setText(text)
        self._empty_label.show()
        self._scroll.hide()
        self.load_older_button.hide()
        self._content.updateGeometry()

    def set_error(self, text: str) -> None:
        self.set_empty_state(text)


class ActivityPane(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state_label = QLabel("not_loaded")
        self.state_label.setWordWrap(True)
        state_header = QLabel("Current state")
        self.pending_label = QLabel("")
        self.pending_label.setWordWrap(True)
        self._pending_header = QLabel("Pending request summary")
        self._state_section = QWidget()
        state_layout = QVBoxLayout(self._state_section)
        state_layout.setContentsMargins(0, 0, 0, 0)
        state_layout.addWidget(state_header)
        state_layout.addWidget(self.state_label)
        state_layout.addWidget(self._pending_header)
        state_layout.addWidget(self.pending_label)
        self._state_section.hide()
        self.activity_list = QListWidget()
        self.activity_list.setObjectName("activityList")
        self._activity_header = QLabel("Recent activities")
        self._activity_header.setObjectName("recentActivitiesHeader")
        self._activity_ids: set[str] = set()
        layout = QVBoxLayout(self)
        layout.addWidget(self._state_section)
        layout.addWidget(self._activity_header)
        layout.addWidget(self.activity_list, 1)

    def set_snapshot(self, snapshot: Mapping[str, object], *, reset: bool = False) -> None:
        state = _safe_text(snapshot.get("state"), 128) or "not_loaded"
        self.state_label.setText(state)
        pending = snapshot.get("pending_request")
        if isinstance(pending, Mapping):
            has_pending = True
            self._pending_header.show()
            self.pending_label.show()
            label = "Approval required" if state == "needs_approval" else "Input required"
            summary = _safe_text(pending.get("summary"), 2_000) or _safe_text(
                pending.get("reason"), 2_000
            )
            self.pending_label.setText(f"{label}\n{summary}".strip())
        else:
            has_pending = False
            self._pending_header.hide()
            self.pending_label.hide()
            self.pending_label.setText("")
        error = _safe_text(snapshot.get("error"), 2_000)
        self._state_section.setVisible(state != "not_loaded" or has_pending or bool(error))
        if reset:
            self.activity_list.clear()
            self._activity_ids.clear()
        self.merge_activities(snapshot.get("recent_activities"))

    def merge_activities(self, recent: object) -> None:
        if not isinstance(recent, list):
            return
        for activity in recent:
            if isinstance(activity, Mapping):
                self.append_activity(activity)

    def append_activity(self, activity: Mapping[str, object]) -> None:
        activity_id = activity.get("activity_id")
        if not isinstance(activity_id, str) or activity_id in self._activity_ids:
            return
        self._activity_ids.add(activity_id)
        row = QListWidgetItem(activity_row(activity))
        row.setData(Qt.ItemDataRole.UserRole, activity_id)
        self.activity_list.addItem(row)
        while self.activity_list.count() > 200:
            removed = self.activity_list.takeItem(0)
            if removed is not None:
                removed_id = removed.data(Qt.ItemDataRole.UserRole)
                if isinstance(removed_id, str):
                    self._activity_ids.discard(removed_id)

    def set_empty_state(self, text: str) -> None:
        self.state_label.setText(text)
        self.pending_label.clear()
        self._pending_header.hide()
        self.pending_label.hide()
        self._state_section.hide()
        self.activity_list.clear()
        self._activity_ids.clear()

    def set_error(self, text: str) -> None:
        self.state_label.setText(text)
        self.pending_label.clear()
        self._pending_header.hide()
        self.pending_label.hide()
        self._state_section.show()
