from __future__ import annotations

import os
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from math import ceil
from typing import Any

from PySide6.QtCore import QDir, QPoint, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QBrush,
    QDesktopServices,
    QFont,
    QHideEvent,
    QPainter,
    QPaintEvent,
    QPalette,
    QPen,
    QResizeEvent,
    QShowEvent,
    QTextDocument,
    QTextOption,
)
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
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

from .activity_pane import ActivityPane as ActivityPane
from .activity_pane import activity_row as activity_row
from .bulk_thread_naming import build_thread_list_header, loaded_untitled_thread_rows
from .conversation_widgets import build_conversation_row
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
    started_at_ms: int | None = None
    completed_at_ms: int | None = None


@dataclass(frozen=True, slots=True)
class _HistoryRenderRow:
    key: tuple[str, ...]
    kind: str
    value: TimelineEntry | str


def _safe_text(value: object, limit: int = 16_384) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _safe_status(value: object) -> str | None:
    return value if isinstance(value, str) and len(value) <= 128 else None


_MAX_EPOCH_MILLISECONDS = 253_402_300_799_999
_TERMINAL_ITEM_STATUSES = {"completed", "failed", "interrupted", "error"}
_EFFECTIVE_SANDBOX_MODES = {
    "danger-full-access",
    "external-sandbox",
    "read-only",
    "workspace-write",
}
_EFFECTIVE_APPROVAL_POLICIES = {"never", "on-request", "untrusted"}
_EFFECTIVE_APPROVALS_REVIEWERS = {"auto_review", "guardian_subagent", "user"}


def _safe_epoch_milliseconds(value: object) -> int | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > _MAX_EPOCH_MILLISECONDS
    ):
        return None
    return value


def _local_datetime(value: object) -> datetime | None:
    timestamp = _safe_epoch_milliseconds(value)
    if timestamp is None:
        return None
    try:
        return datetime.fromtimestamp(timestamp / 1_000)
    except (OSError, OverflowError, ValueError):
        return None


def format_history_timing(started_at_ms: object, completed_at_ms: object) -> str:
    started = _local_datetime(started_at_ms)
    completed = _local_datetime(completed_at_ms)
    show_date = started is not None and completed is not None and started.date() != completed.date()

    def format_one(value: datetime | None) -> str:
        if value is None:
            return "—"
        return value.strftime("%m/%d %H:%M:%S" if show_date else "%H:%M:%S")

    return f"Start {format_one(started)} · End {format_one(completed)}"


def _item_is_running(entry: TimelineEntry) -> bool:
    if entry.status in _TERMINAL_ITEM_STATUSES:
        return False
    return entry.status == "in_progress" or (
        entry.started_at_ms is not None and entry.completed_at_ms is None
    )


class _HistoryRunningSpinner(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("historyRunningSpinner")
        self.setFixedSize(16, 16)
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.setInterval(90)
        self._timer.timeout.connect(self._advance)

    def _advance(self) -> None:
        self._angle = (self._angle + 30) % 360
        self.update()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if not self._timer.isActive():
            self._timer.start()

    def hideEvent(self, event: QHideEvent) -> None:
        self._timer.stop()
        super().hideEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(self.palette().color(QPalette.ColorRole.Highlight))
        pen.setWidthF(2.0)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(QRectF(3.0, 3.0, 10.0, 10.0), self._angle * 16, 250 * 16)
        painter.end()


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
        projected = replace(
            projected,
            started_at_ms=_safe_epoch_milliseconds(item.get("started_at_ms")),
            completed_at_ms=_safe_epoch_milliseconds(item.get("completed_at_ms")),
        )
        seen.add((projected.turn_id, projected.item_id))
        entries.append(projected)
    return tuple(entries)


def format_thread_content(entries: Sequence[TimelineEntry]) -> str:
    return "\n\n".join(f"{entry.title}:\n{entry.body}" for entry in entries)


def copy_to_clipboard(text: str) -> None:
    application = QApplication.instance()
    if isinstance(application, QApplication):
        application.clipboard().setText(text)


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
    _GROUP_KEY_ROLE = Qt.ItemDataRole.UserRole + 1
    _OTHER_GROUP_KEY = "\x00other"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._threads: list[dict[str, object]] = []
        self._active_thread_ids: set[str] = set()
        self._thread_statuses: dict[str, str] = {}
        self._has_thread_statuses = False
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
        self._threads = [dict(thread) for thread in threads]
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
                    status = self._thread_statuses.get(thread_id)
                    status_marker = _thread_status_marker(status)
                    if not self._has_thread_statuses and thread_id in self._active_thread_ids:
                        status_marker = "\u25cf "
                    item = QTreeWidgetItem([f"{status_marker}{title}" if status_marker else title])
                    item.setData(0, Qt.ItemDataRole.UserRole, thread_id)
                    item.setToolTip(0, thread_id)
                    if status_marker:
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
            self.list_widget.verticalScrollBar().setValue(scroll_value)
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
        self._measurement_document = QTextDocument(self)
        self._measurement_document.setDocumentMargin(0)
        self._measurement_document.setDefaultFont(self.document().defaultFont())
        self._measurement_document.setDefaultTextOption(self.document().defaultTextOption())
        self._measurement_document.setPlainText(text)
        policy = self.sizePolicy()
        policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
        policy.setVerticalPolicy(QSizePolicy.Policy.Minimum)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def hasHeightForWidth(self) -> bool:
        return True

    def minimumSizeHint(self) -> QSize:
        if self.width() <= 0:
            return QSize(0, 0)
        return QSize(0, self.heightForWidth(self.width()))

    def heightForWidth(self, width: int) -> int:
        available_width = max(1, width - self.frameWidth() * 2)
        self._measurement_document.setTextWidth(available_width)
        document_height = self._measurement_document.documentLayout().documentSize().height()
        return max(1, ceil(document_height) + self.frameWidth() * 2)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        available_width = max(1, self.width() - self.frameWidth() * 2)
        document = self.document()
        if document.textWidth() != available_width:
            document.setTextWidth(available_width)
        document_height = document.documentLayout().documentSize().height()
        content_height = max(1, ceil(document_height) + self.frameWidth() * 2)
        if self.minimumHeight() != content_height:
            self.setMinimumHeight(content_height)


class HistoryPane(QWidget):
    older_requested = Signal()
    activity_visibility_requested = Signal()
    _BOTTOM_FOLLOW_THRESHOLD = 20
    _TOP_THRESHOLD = 2

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._mode = "Conversation"
        self._expanded_conversation_items: set[tuple[str, str]] = set()
        self._timeline_turn_ids: set[str] = set()
        self._mode_scroll_anchor: tuple[tuple[str, ...], int] | None = None
        self._activity_splitter_sizes: list[int] | None = None
        self._activity_visible = True
        self._pending_request_label = ""
        self.conversation_mode_button = QPushButton("Conversation")
        self.conversation_mode_button.setObjectName("historyModeButton")
        self.conversation_mode_button.setCheckable(True)
        self.details_mode_button = QPushButton("Details")
        self.details_mode_button.setObjectName("historyModeButton")
        self.details_mode_button.setCheckable(True)
        self.messages_mode_button = QPushButton("Messages")
        self.messages_mode_button.setObjectName("historyModeButton")
        self.messages_mode_button.setCheckable(True)
        mode_group = QButtonGroup(self)
        mode_group.setExclusive(True)
        mode_group.addButton(self.conversation_mode_button)
        mode_group.addButton(self.details_mode_button)
        mode_group.addButton(self.messages_mode_button)
        self.conversation_mode_button.setChecked(True)
        self.conversation_mode_button.clicked.connect(lambda: self.set_mode("Conversation"))
        self.details_mode_button.clicked.connect(lambda: self.set_mode("Details"))
        self.messages_mode_button.clicked.connect(lambda: self.set_mode("Messages"))
        self.activity_toggle_button = QPushButton("Hide Activity")
        self.activity_toggle_button.setObjectName("activityVisibilityButton")
        self.activity_toggle_button.clicked.connect(self.activity_visibility_requested.emit)
        self.pending_notice_label = QLabel("")
        self.pending_notice_label.setObjectName("activityPendingNotice")
        self.pending_notice_label.setWordWrap(True)
        self.pending_notice_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed
        )
        mode_bar = QHBoxLayout()
        mode_bar.setContentsMargins(0, 0, 0, 0)
        mode_bar.setSpacing(4)
        mode_bar.addWidget(self.conversation_mode_button)
        mode_bar.addWidget(self.details_mode_button)
        mode_bar.addWidget(self.messages_mode_button)
        mode_bar.addStretch(1)
        mode_bar.addWidget(self.activity_toggle_button)
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
        self._user_scrolling = False
        self._render_generation = 0
        self._timeline_rows: list[_HistoryRenderRow] = []
        self._timeline_row_widgets: dict[tuple[str, ...], QWidget] = {}
        self._timeline_render_signature: tuple[object, ...] | None = None
        self._scroll_restore_state: tuple[int, int, int, bool, bool, bool] | None = None
        self._scroll_restore_stage = 0
        self._scroll_restore_timer = QTimer(self)
        self._scroll_restore_timer.setSingleShot(True)
        self._scroll_restore_timer.timeout.connect(self._advance_timeline_scroll_restore)
        scrollbar = self._scroll.verticalScrollBar()
        scrollbar.valueChanged.connect(self._on_scroll_value_changed)
        scrollbar.sliderPressed.connect(self._on_slider_pressed)
        scrollbar.sliderReleased.connect(self._on_slider_released)
        layout = QVBoxLayout(self)
        layout.addLayout(mode_bar)
        layout.addWidget(self.pending_notice_label)
        layout.addWidget(self.load_older_button)
        layout.addWidget(self._empty_label)
        layout.addWidget(self._scroll, 1)

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        if mode not in {"Conversation", "Details", "Messages"}:
            raise ValueError(f"Unsupported history mode: {mode}")
        if mode == self._mode:
            return
        scrollbar = self._scroll.verticalScrollBar()
        old_value = scrollbar.value()
        old_maximum = scrollbar.maximum()
        should_follow = self._follow_newest and not self._user_scrolling
        self._mode_scroll_anchor = None if should_follow else self._visible_scroll_anchor()
        self._mode = mode
        self.conversation_mode_button.setChecked(mode == "Conversation")
        self.details_mode_button.setChecked(mode == "Details")
        self.messages_mode_button.setChecked(mode == "Messages")
        self._render_generation += 1
        generation = self._render_generation
        self._scroll_update_pending = True
        self._render_timeline_rows(tuple(self._timeline_rows))
        has_entries = self._has_visible_timeline_entries()
        self._empty_label.setVisible(not has_entries)
        self._scroll.setVisible(has_entries)
        self._content.updateGeometry()
        self._scroll_restore_state = (
            generation,
            old_value,
            old_maximum,
            should_follow,
            False,
            has_entries,
        )
        self._scroll_restore_stage = 0
        self._scroll_restore_timer.start(0)

    def _visible_scroll_anchor(self) -> tuple[tuple[str, ...], int] | None:
        scroll_value = self._scroll.verticalScrollBar().value()
        for row in self._timeline_rows:
            widget = self._timeline_row_widgets.get(row.key)
            if (
                widget is not None
                and not widget.isHidden()
                and row.kind == "card"
                and widget.geometry().bottom() >= scroll_value
            ):
                return row.key, widget.geometry().top() - scroll_value
        return None

    def set_activity_visible(self, visible: bool) -> None:
        self._activity_visible = visible
        self._refresh_activity_toggle_label()

    def remember_activity_splitter_sizes(self, sizes: list[int]) -> None:
        self._activity_splitter_sizes = list(sizes)

    def saved_activity_splitter_sizes(self) -> list[int] | None:
        return list(self._activity_splitter_sizes) if self._activity_splitter_sizes else None

    def set_pending_request_state(self, pending: bool, label: str = "") -> None:
        self._pending_request_label = label if pending else ""
        self._refresh_activity_toggle_label()

    def _refresh_activity_toggle_label(self) -> None:
        text = "Hide Activity" if self._activity_visible else "Show Activity"
        self.activity_toggle_button.setText(text)
        accessible_name = (
            f"{self._pending_request_label} — {text}"
            if not self._activity_visible and self._pending_request_label
            else text
        )
        self.activity_toggle_button.setAccessibleName(accessible_name)
        self.pending_notice_label.setText(
            self._pending_request_label if not self._activity_visible else ""
        )
        self.pending_notice_label.setVisible(
            not self._activity_visible and bool(self._pending_request_label)
        )
        self.activity_toggle_button.setToolTip(
            self._pending_request_label or "Show or hide the Activity pane"
        )

    def _clear_cards(self) -> None:
        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            if item is None:
                continue
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        self._timeline_rows.clear()
        self._timeline_row_widgets.clear()

    def _timeline_row_widget(self, row: _HistoryRenderRow) -> QWidget:
        if row.kind == "card":
            assert isinstance(row.value, TimelineEntry)
            if self._mode in {"Conversation", "Messages"}:
                entry = row.value
                item_key = (entry.turn_id, entry.item_id)
                timing = (
                    format_history_timing(entry.started_at_ms, entry.completed_at_ms)
                    if entry.started_at_ms is not None or entry.completed_at_ms is not None
                    else None
                )

                def on_expanded_changed(expanded: bool) -> None:
                    self._set_conversation_item_expanded(item_key, expanded)

                return build_conversation_row(
                    entry,
                    expanded=item_key in self._expanded_conversation_items,
                    is_running=_item_is_running(entry),
                    timing=timing,
                    spinner_factory=_HistoryRunningSpinner,
                    copy_text=copy_to_clipboard,
                    on_expanded_changed=on_expanded_changed,
                )
            return self._card(row.value)
        assert isinstance(row.value, str)
        label = QLabel(row.value)
        if row.kind == "header":
            label.setObjectName("turnSeparator")
        else:
            label.setObjectName("turnUsageSnapshot")
            label.setWordWrap(True)
            snapshot_policy = label.sizePolicy()
            snapshot_policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
            label.setSizePolicy(snapshot_policy)
        return label

    def _set_conversation_item_expanded(self, item_key: tuple[str, str], expanded: bool) -> None:
        if expanded:
            self._expanded_conversation_items.add(item_key)
        else:
            self._expanded_conversation_items.discard(item_key)

    def _can_reconcile_timeline_rows(self, rows: Sequence[_HistoryRenderRow]) -> bool:
        old_keys = [row.key for row in self._timeline_rows]
        new_keys = [row.key for row in rows]
        if len(set(old_keys)) != len(old_keys) or len(set(new_keys)) != len(new_keys):
            return False
        old_key_set = set(old_keys)
        new_key_set = set(new_keys)
        if any(row.kind == "card" and row.key not in new_key_set for row in self._timeline_rows):
            return False
        if old_keys and new_keys and not old_key_set.intersection(new_key_set):
            return False
        return [key for key in old_keys if key in new_key_set] == [
            key for key in new_keys if key in old_key_set
        ]

    def _render_timeline_rows(self, rows: Sequence[_HistoryRenderRow]) -> None:
        self._clear_cards()
        for row in rows:
            widget = self._timeline_row_widget(row)
            self._content_layout.addWidget(widget)
            self._timeline_row_widgets[row.key] = widget
        self._timeline_rows = list(rows)
        self._update_mode_visibility()

    def _row_is_visible_in_mode(self, row: _HistoryRenderRow) -> bool:
        if self._mode != "Messages":
            return True
        return (
            row.kind == "card"
            and isinstance(row.value, TimelineEntry)
            and row.value.kind in {"User", "Commentary", "Agent"}
        )

    def _has_visible_timeline_entries(self) -> bool:
        return any(
            row.kind == "card" and self._row_is_visible_in_mode(row) for row in self._timeline_rows
        )

    def _update_mode_visibility(self) -> None:
        for row in self._timeline_rows:
            widget = self._timeline_row_widgets.get(row.key)
            if widget is not None:
                widget.setVisible(self._row_is_visible_in_mode(row))

    def _layout_index(self, widget: QWidget) -> int | None:
        for index in range(self._content_layout.count()):
            item = self._content_layout.itemAt(index)
            if item is not None and item.widget() is widget:
                return index
        return None

    def _discard_timeline_widget(self, widget: QWidget) -> None:
        widget.hide()
        widget.setParent(None)
        widget.deleteLater()

    def _reconcile_timeline_rows(self, rows: Sequence[_HistoryRenderRow]) -> None:
        old_rows_by_key = {row.key: row for row in self._timeline_rows}
        new_rows_by_key = {row.key: row for row in rows}

        for row in reversed(self._timeline_rows):
            if row.key in new_rows_by_key:
                continue
            widget = self._timeline_row_widgets.pop(row.key)
            index = self._layout_index(widget)
            if index is not None:
                item = self._content_layout.takeAt(index)
                del item
            self._discard_timeline_widget(widget)

        for row in rows:
            previous = old_rows_by_key.get(row.key)
            if previous is None or previous.value == row.value:
                continue
            widget = self._timeline_row_widgets[row.key]
            if row.kind in {"header", "usage"}:
                assert isinstance(widget, QLabel)
                assert isinstance(row.value, str)
                widget.setText(row.value)
                continue
            index = self._layout_index(widget)
            assert index is not None
            item = self._content_layout.takeAt(index)
            del item
            self._discard_timeline_widget(widget)
            replacement = self._timeline_row_widget(row)
            self._content_layout.insertWidget(index, replacement)
            self._timeline_row_widgets[row.key] = replacement

        for index, row in enumerate(rows):
            if row.key in self._timeline_row_widgets:
                continue
            widget = self._timeline_row_widget(row)
            self._content_layout.insertWidget(index, widget)
            self._timeline_row_widgets[row.key] = widget
        self._timeline_rows = list(rows)
        self._update_mode_visibility()

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
        render_entries = tuple(entries)
        new_turn_ids = {entry.turn_id for entry in render_entries}
        if (
            self._timeline_turn_ids
            and new_turn_ids
            and not self._timeline_turn_ids.intersection(new_turn_ids)
        ):
            self._expanded_conversation_items.clear()
        if new_turn_ids:
            self._timeline_turn_ids = new_turn_ids
        turn_headers: list[tuple[str, str]] = []
        usage_labels: list[tuple[str, str]] = []
        for turn_id in dict.fromkeys(entry.turn_id for entry in render_entries):
            status = turn_statuses.get(turn_id) if turn_statuses else None
            metadata = turn_model_metadata.get(turn_id) if turn_model_metadata else None
            turn_headers.append((turn_id, _turn_header(status, metadata)))
            snapshot = turn_usage_snapshots.get(turn_id) if turn_usage_snapshots else None
            if snapshot is not None:
                usage_labels.append((turn_id, format_codex_usage_snapshot(snapshot)))
        header_texts = dict(turn_headers)
        usage_texts = dict(usage_labels)
        render_rows: list[_HistoryRenderRow] = []
        seen_turn_ids: set[str] = set()
        seen_item_ids: set[str] = set()
        previous_turn: str | None = None
        unique_row_keys = True
        for entry in render_entries:
            if entry.turn_id != previous_turn:
                if entry.turn_id in seen_turn_ids:
                    unique_row_keys = False
                else:
                    seen_turn_ids.add(entry.turn_id)
                render_rows.append(
                    _HistoryRenderRow(
                        ("header", entry.turn_id), "header", header_texts[entry.turn_id]
                    )
                )
                usage_text = usage_texts.get(entry.turn_id)
                if usage_text is not None:
                    render_rows.append(
                        _HistoryRenderRow(("usage", entry.turn_id), "usage", usage_text)
                    )
                previous_turn = entry.turn_id
            if entry.item_id in seen_item_ids:
                unique_row_keys = False
            else:
                seen_item_ids.add(entry.item_id)
            render_rows.append(
                _HistoryRenderRow(("card", entry.turn_id, entry.item_id), "card", entry)
            )
        render_signature = (
            render_entries,
            has_older,
            tuple(turn_headers),
            tuple(usage_labels),
        )
        if self._timeline_render_signature == render_signature:
            return

        self._mode_scroll_anchor = None
        scrollbar = self._scroll.verticalScrollBar()
        old_value = scrollbar.value()
        old_maximum = scrollbar.maximum()
        should_follow = self._follow_newest and not self._user_scrolling
        self._render_generation += 1
        generation = self._render_generation
        self._scroll_update_pending = True
        self._has_older = has_older
        self._update_load_older_visibility()
        if (
            unique_row_keys
            and self._timeline_render_signature is not None
            and self._can_reconcile_timeline_rows(render_rows)
        ):
            self._reconcile_timeline_rows(render_rows)
        else:
            self._render_timeline_rows(render_rows)
        has_visible_entries = self._has_visible_timeline_entries()
        self._empty_label.setVisible(not has_visible_entries)
        self._scroll.setVisible(has_visible_entries)
        if not has_visible_entries:
            should_follow = True
            self._follow_newest = True
        self._timeline_render_signature = render_signature
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
        elif should_follow and not self._user_scrolling:
            target = scrollbar.maximum()
        elif self._mode_scroll_anchor is not None:
            anchor_key, offset = self._mode_scroll_anchor
            anchor_widget = self._timeline_row_widgets.get(anchor_key)
            target = (
                anchor_widget.geometry().top() - offset if anchor_widget is not None else old_value
            )
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
        if not prepend and should_follow and has_entries and not self._user_scrolling:
            self._set_programmatic_scroll(self._scroll.verticalScrollBar().maximum())
            self._follow_newest = True
        elif not has_entries:
            self._follow_newest = True
        self._update_load_older_visibility()
        self._scroll_update_pending = False
        self._mode_scroll_anchor = None

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

    def _on_slider_pressed(self) -> None:
        self._user_scrolling = True

    def _on_slider_released(self) -> None:
        self._user_scrolling = False
        scrollbar = self._scroll.verticalScrollBar()
        self._follow_newest = (
            scrollbar.maximum() - scrollbar.value() <= self._BOTTOM_FOLLOW_THRESHOLD
        )
        state = self._scroll_restore_state
        if self._scroll_update_pending and state is not None:
            generation, _, _, _, prepend, has_entries = state
            self._scroll_restore_state = (
                generation,
                scrollbar.value(),
                scrollbar.maximum(),
                self._follow_newest,
                prepend,
                has_entries,
            )
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
        header.addWidget(header_label, 1)
        spinner = _HistoryRunningSpinner(card)
        header.addWidget(spinner)
        spinner.setVisible(_item_is_running(entry))
        if entry.kind in {"User", "Agent"}:
            copy_button = QPushButton("Copy")
            copy_button.setObjectName("copyMessageButton")
            copy_button.clicked.connect(
                lambda _checked=False, text=entry.body: copy_to_clipboard(text)
            )
            copy_policy = copy_button.sizePolicy()
            copy_policy.setHorizontalPolicy(QSizePolicy.Policy.Fixed)
            copy_button.setSizePolicy(copy_policy)
            header.addWidget(copy_button)
        layout.addLayout(header)
        timing = QLabel(format_history_timing(entry.started_at_ms, entry.completed_at_ms))
        timing.setObjectName("historyTiming")
        layout.addWidget(timing)
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
        self._user_scrolling = False
        self._follow_newest = True
        self._has_older = False
        self._timeline_render_signature = None
        self._timeline_turn_ids.clear()
        self._expanded_conversation_items.clear()
        self._mode_scroll_anchor = None
        self._set_programmatic_scroll(0)
        self._clear_cards()
        self._empty_label.setText(text)
        self._empty_label.show()
        self._scroll.hide()
        self.load_older_button.hide()
        self._content.updateGeometry()

    def set_error(self, text: str) -> None:
        self.set_empty_state(text)
