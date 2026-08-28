from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


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


def activity_row(activity: Mapping[str, object]) -> str:
    timestamp = _safe_text(activity.get("timestamp"), 128)
    activity_type = _safe_text(activity.get("type"), 128)
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

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._threads: list[dict[str, object]] = []
        title = QLabel("Threads")
        self.refresh_button = QPushButton("Refresh")
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter threads")
        self.list_widget = QListWidget()
        self.list_widget.setObjectName("threadList")
        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        self.filter_edit.textChanged.connect(self._render)
        self.list_widget.itemActivated.connect(self._emit_selected)
        self.list_widget.itemClicked.connect(self._emit_selected)
        header = QHBoxLayout()
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self.refresh_button)
        layout = QVBoxLayout(self)
        layout.addLayout(header)
        layout.addWidget(self.filter_edit)
        layout.addWidget(self.list_widget, 1)

    def set_threads(self, threads: Sequence[Mapping[str, object]]) -> None:
        self._threads = [dict(thread) for thread in threads]
        self._render()

    def _render(self) -> None:
        query = self.filter_edit.text().casefold()
        self.list_widget.clear()
        for thread in self._threads:
            thread_id = thread.get("id")
            if not isinstance(thread_id, str):
                continue
            searchable = " ".join(
                str(thread.get(key, "")) for key in ("id", "name", "preview", "cwd")
            )
            if query and query not in searchable.casefold():
                continue
            title = _safe_text(thread.get("name")) or _safe_text(thread.get("preview")) or thread_id
            preview = _safe_text(thread.get("preview"))
            text = title if not preview or title == preview else f"{title}\n{preview}"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, thread_id)
            item.setToolTip(thread_id)
            self.list_widget.addItem(item)

    def _emit_selected(self, item: QListWidgetItem) -> None:
        thread_id = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(thread_id, str):
            self.thread_selected.emit(thread_id)

    def set_empty_state(self, text: str) -> None:
        self.list_widget.clear()
        self.list_widget.addItem(text)


class HistoryPane(QWidget):
    older_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.load_older_button = QPushButton("Load older")
        self.load_older_button.clicked.connect(self.older_requested.emit)
        self.load_older_button.hide()
        self._empty_label = QLabel("Select a thread to view history.")
        self._empty_label.setWordWrap(True)
        self._content = QWidget()
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setWidget(self._content)
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
    ) -> None:
        self._clear_cards()
        previous_turn: str | None = None
        for entry in entries:
            if previous_turn is not None and previous_turn != entry.turn_id:
                turn_status = turn_statuses.get(entry.turn_id) if turn_statuses else None
                separator_text = "Turn" if not turn_status else f"Turn · {turn_status}"
                separator = QLabel(f"────────  {separator_text}  ────────")
                separator.setObjectName("turnSeparator")
                self._content_layout.addWidget(separator)
            self._content_layout.addWidget(self._card(entry))
            previous_turn = entry.turn_id
        self._empty_label.setVisible(not entries)
        self._scroll.setVisible(bool(entries))
        self.load_older_button.setVisible(has_older)

    def _card(self, entry: TimelineEntry) -> QWidget:
        card = QFrame()
        card.setObjectName("historyCard")
        card.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(card)
        header = entry.title if entry.status is None else f"{entry.title} · {entry.status}"
        layout.addWidget(QLabel(header))
        if entry.body:
            body = QTextEdit()
            body.setReadOnly(True)
            body.setPlainText(entry.body)
            body.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
                | Qt.TextInteractionFlag.TextSelectableByKeyboard
            )
            body.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
            body.setMinimumHeight(36)
            layout.addWidget(body)
        if entry.details:
            layout.addWidget(QLabel(" · ".join(entry.details)))
        return card

    def set_empty_state(self, text: str) -> None:
        self._clear_cards()
        self._empty_label.setText(text)
        self._empty_label.show()
        self._scroll.hide()
        self.load_older_button.hide()

    def set_error(self, text: str) -> None:
        self.set_empty_state(text)


class ActivityPane(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state_label = QLabel("Current state\nnot loaded")
        self.state_label.setWordWrap(True)
        self.pending_label = QLabel("")
        self.pending_label.setWordWrap(True)
        self.activity_list = QListWidget()
        self.activity_list.setObjectName("activityList")
        self._activity_ids: set[str] = set()
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Current state"))
        layout.addWidget(self.state_label)
        layout.addWidget(QLabel("Pending request summary"))
        layout.addWidget(self.pending_label)
        layout.addWidget(QLabel("Recent activities"))
        layout.addWidget(self.activity_list, 1)

    def set_snapshot(self, snapshot: Mapping[str, object]) -> None:
        state = _safe_text(snapshot.get("state"), 128) or "not_loaded"
        self.state_label.setText(state)
        pending = snapshot.get("pending_request")
        if isinstance(pending, Mapping):
            label = "Approval required" if state == "needs_approval" else "Input required"
            summary = _safe_text(pending.get("summary"), 2_000) or _safe_text(
                pending.get("reason"), 2_000
            )
            self.pending_label.setText(f"{label}\n{summary}".strip())
        else:
            self.pending_label.setText("")
        self.activity_list.clear()
        self._activity_ids.clear()
        recent = snapshot.get("recent_activities")
        if isinstance(recent, list):
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
        self.activity_list.clear()
        self._activity_ids.clear()
