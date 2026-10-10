from __future__ import annotations

import json
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
    started_at_ms: int | None = None
    completed_at_ms: int | None = None


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
        self._user_scrolling = False
        self._render_generation = 0
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
                widget.hide()
                widget.setParent(None)
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
        should_follow = self._follow_newest and not self._user_scrolling
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
        elif should_follow and not self._user_scrolling:
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
        if not prepend and should_follow and has_entries and not self._user_scrolling:
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
        header.addWidget(header_label)
        spinner = _HistoryRunningSpinner(card)
        header.addWidget(spinner)
        spinner.setVisible(_item_is_running(entry))
        if entry.kind in {"User", "Agent"}:
            copy_button = QPushButton("Copy")
            copy_button.setObjectName("copyMessageButton")
            copy_button.clicked.connect(
                lambda _checked=False, text=entry.body: copy_to_clipboard(text)
            )
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
    approval_decision_requested = Signal(object, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state_label = QLabel("not_loaded")
        self.state_label.setWordWrap(True)
        state_header = QLabel("Current state")
        self.sandbox_mode_label = QLabel("Sandbox: Unknown")
        self.sandbox_mode_label.setObjectName("effectiveSandboxMode")
        self.approval_policy_label = QLabel("Approval policy: Unknown")
        self.approval_policy_label.setObjectName("effectiveApprovalPolicy")
        self.approvals_reviewer_label = QLabel("Approvals reviewer: Unknown")
        self.approvals_reviewer_label.setObjectName("effectiveApprovalsReviewer")
        self._effective_access_section = QWidget()
        self._effective_access_section.setObjectName("effectiveThreadAccess")
        effective_access_layout = QVBoxLayout(self._effective_access_section)
        effective_access_layout.setContentsMargins(0, 0, 0, 0)
        effective_access_layout.setSpacing(1)
        for access_label in (
            self.sandbox_mode_label,
            self.approval_policy_label,
            self.approvals_reviewer_label,
        ):
            access_label.setTextFormat(Qt.TextFormat.PlainText)
            access_label.setWordWrap(True)
            effective_access_layout.addWidget(access_label)
        self._sandbox_mode_default_font = QFont(self.sandbox_mode_label.font())
        self.pending_label = QLabel("")
        self.pending_label.setWordWrap(True)
        self._pending_header = QLabel("Pending request")
        self.approval_details_label = QLabel("")
        self.approval_details_label.setObjectName("approvalDetails")
        self.approval_details_label.setWordWrap(True)
        self.approval_control_message = QLabel("")
        self.approval_control_message.setObjectName("approvalControlMessage")
        self.approval_control_message.setWordWrap(True)
        self._approval_feedback = QLabel("")
        self._approval_feedback.setObjectName("approvalFeedback")
        self._approval_feedback.setWordWrap(True)
        self.approval_buttons: dict[str, QPushButton] = {}
        button_layout = QHBoxLayout()
        for label, decision, name in (
            ("Allow once", "accept", "allowOnce"),
            ("Allow for session", "acceptForSession", "allowForSession"),
            ("Decline", "decline", "decline"),
            ("Cancel", "cancel", "cancel"),
        ):
            button = QPushButton(label)
            button.setObjectName(name)
            button.clicked.connect(
                lambda _checked=False, decision=decision: self._request_approval_decision(decision)
            )
            self.approval_buttons[decision] = button
            button_layout.addWidget(button)
        self.approval_buttons_widget = QWidget()
        self.approval_buttons_widget.setLayout(button_layout)
        self.allow_once_button = self.approval_buttons["accept"]
        self.allow_for_session_button = self.approval_buttons["acceptForSession"]
        self.decline_button = self.approval_buttons["decline"]
        self.cancel_button = self.approval_buttons["cancel"]
        self._pending_approval_id: int | str | None = None
        self._has_approval = False
        self._control_available = False
        self._approval_busy = False
        self._approval_resolved = False
        self._state_section = QWidget()
        state_layout = QVBoxLayout(self._state_section)
        state_layout.setContentsMargins(0, 0, 0, 0)
        state_layout.addWidget(state_header)
        state_layout.addWidget(self.state_label)
        state_layout.addWidget(self._effective_access_section)
        state_layout.addWidget(self._pending_header)
        state_layout.addWidget(self.pending_label)
        state_layout.addWidget(self.approval_details_label)
        state_layout.addWidget(self.approval_buttons_widget)
        state_layout.addWidget(self.approval_control_message)
        state_layout.addWidget(self._approval_feedback)
        self.approval_details_label.hide()
        self.approval_buttons_widget.hide()
        self.approval_control_message.hide()
        self._approval_feedback.hide()
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
        self._set_effective_access(snapshot, state)
        pending = snapshot.get("pending_request")
        if isinstance(pending, Mapping):
            self._pending_header.show()
            self.pending_label.show()
            label = "Approval required" if state == "needs_approval" else "Input required"
            summary = _safe_text(pending.get("summary"), 2_000) or _safe_text(
                pending.get("reason"), 2_000
            )
            self.pending_label.setText(f"{label}\n{summary}".strip())
            method = pending.get("method")
            request_id = pending.get("request_id")
            self._has_approval = (
                state == "needs_approval"
                and method
                in {
                    "item/commandExecution/requestApproval",
                    "item/fileChange/requestApproval",
                    "item/permissions/requestApproval",
                }
                and not isinstance(request_id, bool)
                and isinstance(request_id, (int, str))
            )
            self._pending_approval_id = request_id if self._has_approval else None
            self._approval_busy = False
            self._approval_resolved = False
            self._approval_feedback.clear()
            if self._has_approval:
                self.approval_details_label.setText(self._approval_details(pending, snapshot))
                self.approval_details_label.show()
                self._approval_feedback.hide()
            else:
                self.approval_details_label.clear()
                self.approval_details_label.hide()
            self._sync_approval_controls()
        else:
            self._pending_header.hide()
            self.pending_label.hide()
            self.pending_label.setText("")
            self._has_approval = False
            self._pending_approval_id = None
            self._approval_busy = False
            self._approval_resolved = False
            self.approval_details_label.clear()
            self.approval_details_label.hide()
            self._approval_feedback.clear()
            self._approval_feedback.hide()
            self._sync_approval_controls()
        self._state_section.show()
        if reset:
            self.activity_list.clear()
            self._activity_ids.clear()
        self.merge_activities(snapshot.get("recent_activities"))

    @property
    def approval_request_id(self) -> int | str | None:
        return self._pending_approval_id

    def set_control_available(self, available: bool) -> None:
        self._control_available = available
        self._sync_approval_controls()

    def set_approval_busy(self, busy: bool) -> None:
        self._approval_busy = busy
        if busy:
            self._approval_feedback.setText("Resolving approval…")
            self._approval_feedback.show()
        self._sync_approval_controls()

    def set_approval_feedback(self, message: str) -> None:
        self._approval_feedback.setText(message)
        self._approval_feedback.setVisible(bool(message) and self._has_approval)

    def mark_approval_resolved(self) -> None:
        self._approval_resolved = True
        self._approval_busy = False
        self._approval_feedback.setText("Approval resolved. Refreshing status…")
        self._approval_feedback.show()
        self._sync_approval_controls()

    def _sync_approval_controls(self) -> None:
        active = self._has_approval
        self.approval_buttons_widget.setVisible(active)
        for button in self.approval_buttons.values():
            button.setEnabled(
                active
                and self._control_available
                and not self._approval_busy
                and not self._approval_resolved
            )
        self.approval_control_message.setText(
            "Resolve from MCP client; Console control is unavailable for this Bridge."
        )
        self.approval_control_message.setVisible(active and not self._control_available)

    def _request_approval_decision(self, decision: str) -> None:
        if (
            self._pending_approval_id is not None
            and self._control_available
            and not self._approval_busy
            and not self._approval_resolved
        ):
            self.approval_decision_requested.emit(self._pending_approval_id, decision)

    @staticmethod
    def _approval_details(pending: Mapping[str, object], snapshot: Mapping[str, object]) -> str:
        method = pending.get("method")
        titles = {
            "item/commandExecution/requestApproval": "Command approval",
            "item/fileChange/requestApproval": "File change approval",
            "item/permissions/requestApproval": "Permission approval",
        }
        parts = [titles.get(method if isinstance(method, str) else "", "Approval request")]
        for key, label in (("thread_id", "Thread"), ("turn_id", "Turn")):
            value = _safe_text(pending.get(key), 512)
            if value:
                parts.append(f"{label}: {value}")
        summary = _safe_text(pending.get("summary"), 2_000)
        if summary:
            parts.append(f"Summary / command: {summary}")

        if method == "item/fileChange/requestApproval":
            diff = _safe_text(snapshot.get("current_diff"), 16_000)
            if diff:
                parts.append(f"Current diff:\n{diff}")
        elif method == "item/permissions/requestApproval":
            permission = pending.get("permission")
            if isinstance(permission, Mapping):
                reason = _safe_text(permission.get("reason"), 2_000)
                cwd = _safe_text(permission.get("cwd"), 2_000)
                if reason:
                    parts.append(f"Reason: {reason}")
                if cwd:
                    parts.append(f"Working directory: {cwd}")
                requested = permission.get("requested_permissions")
                if isinstance(requested, Mapping):
                    for key, label in (
                        ("fileSystem", "Requested filesystem permissions"),
                        ("network", "Requested network permissions"),
                    ):
                        if key in requested:
                            rendered = json.dumps(
                                requested[key], ensure_ascii=False, sort_keys=True
                            )[:2_000]
                            parts.append(f"{label}: {rendered}")
                scopes = permission.get("allowed_scopes")
                if isinstance(scopes, list):
                    safe_scopes = [scope for scope in scopes if scope in {"turn", "session"}]
                    if safe_scopes:
                        parts.append(f"Allowed scopes: {', '.join(safe_scopes)}")
        return "\n".join(parts)[:8_000]

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
        self._set_effective_access_unavailable("Unknown")
        self.pending_label.clear()
        self._has_approval = False
        self._pending_approval_id = None
        self._approval_busy = False
        self._approval_resolved = False
        self.approval_details_label.clear()
        self.approval_details_label.hide()
        self._approval_feedback.clear()
        self._approval_feedback.hide()
        self._sync_approval_controls()
        self._pending_header.hide()
        self.pending_label.hide()
        self._state_section.hide()
        self.activity_list.clear()
        self._activity_ids.clear()

    def set_loading_state(self, text: str) -> None:
        self.set_empty_state(text)
        self._state_section.show()

    def set_error(self, text: str) -> None:
        self.state_label.setText(text)
        self._set_effective_access_unavailable("Unavailable")
        self.pending_label.clear()
        self._pending_header.hide()
        self.pending_label.hide()
        self._state_section.show()

    def _set_effective_access(self, snapshot: Mapping[str, object], state: str) -> None:
        metadata: object = snapshot.get("thread_metadata")
        if state == "not_loaded":
            metadata = None
        sandbox_mode = self._effective_metadata_value(
            metadata, "sandbox_mode", _EFFECTIVE_SANDBOX_MODES
        )
        approval_policy = self._effective_metadata_value(
            metadata, "approval_policy", _EFFECTIVE_APPROVAL_POLICIES
        )
        approvals_reviewer = self._effective_metadata_value(
            metadata, "approvals_reviewer", _EFFECTIVE_APPROVALS_REVIEWERS
        )

        if sandbox_mode == "danger-full-access":
            self.sandbox_mode_label.setText("Sandbox: WARNING — Full Access (danger-full-access)")
            palette = QPalette(self.sandbox_mode_label.palette())
            palette.setColor(
                QPalette.ColorRole.WindowText,
                palette.color(QPalette.ColorRole.BrightText),
            )
            self.sandbox_mode_label.setPalette(palette)
            font = QFont(self._sandbox_mode_default_font)
            font.setBold(True)
            self.sandbox_mode_label.setFont(font)
        else:
            self.sandbox_mode_label.setText(f"Sandbox: {sandbox_mode}")
            self.sandbox_mode_label.setPalette(QPalette())
            self.sandbox_mode_label.setFont(self._sandbox_mode_default_font)
        self.approval_policy_label.setText(f"Approval policy: {approval_policy}")
        self.approvals_reviewer_label.setText(f"Approvals reviewer: {approvals_reviewer}")

    @staticmethod
    def _effective_metadata_value(
        metadata: object, key: str, allowed_values: Collection[str]
    ) -> str:
        if not isinstance(metadata, Mapping):
            return "Unknown"
        value = metadata.get(key)
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 128
            or value not in allowed_values
        ):
            return "Unknown"
        return value

    def _set_effective_access_unavailable(self, value: str) -> None:
        self.sandbox_mode_label.setText(f"Sandbox: {value}")
        self.sandbox_mode_label.setPalette(QPalette())
        self.sandbox_mode_label.setFont(self._sandbox_mode_default_font)
        self.approval_policy_label.setText(f"Approval policy: {value}")
        self.approvals_reviewer_label.setText(f"Approvals reviewer: {value}")
