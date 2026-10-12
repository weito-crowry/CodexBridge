from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..thread_titles import derive_thread_title, normalize_explicit_title
from .project_names import normalize_cwd

if TYPE_CHECKING:
    from .main_window import MainWindow

_MAX_TITLE_LENGTH = 100
_COLUMN_CHECK = 0
_COLUMN_ID = 1
_COLUMN_CWD = 2
_COLUMN_TITLE = 3
_COLUMN_STATUS = 4


def build_thread_list_header(
    title: QLabel,
    refresh_button: QPushButton,
    on_clicked: Callable[[], None],
) -> tuple[QHBoxLayout, QPushButton]:
    header = QHBoxLayout()
    header.addWidget(title)
    header.addStretch(1)
    button = QPushButton("Name untitled threads…")
    button.setObjectName("nameUntitledThreadsButton")
    button.setToolTip("Preview names for untitled threads in the currently loaded list")
    button.clicked.connect(on_clicked)
    header.addWidget(button)
    header.addWidget(refresh_button)
    return header, button


def loaded_untitled_thread_rows(
    threads: Sequence[Mapping[str, object]], project_names: Mapping[str, str]
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for thread in threads:
        thread_id = thread.get("id")
        name = thread.get("name")
        if not isinstance(thread_id, str) or not thread_id:
            continue
        if isinstance(name, str) and name.strip():
            continue
        row = dict(thread)
        cwd = thread.get("cwd")
        project_key = normalize_cwd(cwd)
        row["project_name"] = project_names.get(project_key) if project_key else None
        rows.append(row)
    return rows


@dataclass(frozen=True, slots=True)
class UntitledThreadProposal:
    thread_id: str
    cwd_label: str
    title: str


@dataclass(frozen=True, slots=True)
class _RenameRequest:
    row: int
    thread_id: str
    title: str


def build_untitled_thread_proposals(
    threads: Sequence[Mapping[str, object]],
) -> list[UntitledThreadProposal]:
    titles = [
        derive_thread_title(
            preview if isinstance((preview := thread.get("preview")), str) else "",
            thread_id,
        )
        for thread in threads
        if isinstance((thread_id := thread.get("id")), str) and thread_id
    ]
    valid_threads = [
        thread
        for thread in threads
        if isinstance((thread_id := thread.get("id")), str) and thread_id
    ]
    counts = Counter(title.casefold() for title in titles)
    reserved = {title.casefold() for title in titles if counts[title.casefold()] == 1}
    used: set[str] = set()
    proposals: list[UntitledThreadProposal] = []
    title_index = 0
    for row_index, thread in enumerate(valid_threads):
        thread_id = thread.get("id")
        if not isinstance(thread_id, str):
            continue
        title = titles[title_index]
        title_index += 1
        key = title.casefold()
        if counts[key] > 1:
            suffix = thread_id[-8:]
            candidate = _append_id_suffix(title, suffix)
            if candidate.casefold() in reserved or candidate.casefold() in used:
                for width in (12, 16, 24, 32):
                    suffix = thread_id[-width:]
                    candidate = _append_id_suffix(title, suffix)
                    if candidate.casefold() not in (reserved | used):
                        break
                else:
                    disambiguator = 1
                    while True:
                        suffix = f"{thread_id[-8:]}-{row_index + 1}-{disambiguator}"
                        candidate = _append_id_suffix(title, suffix)
                        if candidate.casefold() not in (reserved | used):
                            break
                        disambiguator += 1
            title = candidate
        used.add(title.casefold())
        cwd = thread.get("cwd")
        project_name = thread.get("project_name")
        cwd_text = cwd.strip() if isinstance(cwd, str) else ""
        project_text = project_name.strip() if isinstance(project_name, str) else ""
        if project_text and cwd_text:
            cwd_label = f"{project_text} — {cwd_text}"
        else:
            cwd_label = project_text or cwd_text or "Other"
        proposals.append(UntitledThreadProposal(thread_id, cwd_label, title))
    return proposals


def _append_id_suffix(title: str, suffix: str) -> str:
    suffix_text = f" ({suffix})"
    available = max(1, _MAX_TITLE_LENGTH - len(suffix_text))
    return f"{title[:available].rstrip()}{suffix_text}"


def _abbreviated_id(thread_id: str) -> str:
    if len(thread_id) <= 20:
        return thread_id
    return f"{thread_id[:8]}…{thread_id[-8:]}"


class BulkThreadNamingDialog(QDialog):
    confirm_requested = Signal()
    cancel_requested = Signal()

    def __init__(
        self,
        proposals: Sequence[UntitledThreadProposal],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Name untitled threads")
        self.setMinimumSize(760, 420)
        self._busy = False
        self._completed = False
        self.scope_label = QLabel(
            f"Preview of {len(proposals)} currently loaded untitled threads. "
            "Only loaded rows are included; this does not scan all history."
        )
        self.scope_label.setWordWrap(True)
        self.table = QTableWidget(len(proposals), 5)
        self.table.setHorizontalHeaderLabels(
            ["Select", "Thread ID", "CWD / project", "Proposed title", "Result"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setColumnWidth(_COLUMN_CHECK, 58)
        self.table.setColumnWidth(_COLUMN_ID, 150)
        self.table.setColumnWidth(_COLUMN_CWD, 260)
        self.table.setColumnWidth(_COLUMN_STATUS, 90)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(_COLUMN_TITLE, QHeaderView.ResizeMode.Stretch)
        for row_index, proposal in enumerate(proposals):
            checked = QTableWidgetItem()
            checked.setFlags(
                (checked.flags() | Qt.ItemFlag.ItemIsUserCheckable) & ~Qt.ItemFlag.ItemIsEditable
            )
            checked.setCheckState(Qt.CheckState.Checked)
            self.table.setItem(row_index, _COLUMN_CHECK, checked)

            identifier = QTableWidgetItem(_abbreviated_id(proposal.thread_id))
            identifier.setToolTip(proposal.thread_id)
            identifier.setData(Qt.ItemDataRole.UserRole, proposal.thread_id)
            identifier.setFlags(identifier.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row_index, _COLUMN_ID, identifier)

            cwd = QTableWidgetItem(proposal.cwd_label)
            cwd.setToolTip(proposal.cwd_label)
            cwd.setFlags(cwd.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row_index, _COLUMN_CWD, cwd)

            title = QTableWidgetItem(proposal.title)
            self.table.setItem(row_index, _COLUMN_TITLE, title)

            status = QTableWidgetItem("")
            status.setFlags(status.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row_index, _COLUMN_STATUS, status)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, max(1, len(proposals)))
        self.progress_bar.setValue(0)
        self.progress_label = QLabel("No changes are made until you confirm selected rows.")
        self.confirm_button = QPushButton("Confirm selected")
        self.cancel_button = QPushButton("Cancel")
        self.confirm_button.clicked.connect(self._confirm_clicked)
        self.cancel_button.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.confirm_button)
        buttons.addWidget(self.cancel_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.scope_label)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.progress_label)
        layout.addLayout(buttons)

    def _confirm_clicked(self) -> None:
        if self._completed:
            self.accept()
        elif not self._busy:
            self.confirm_requested.emit()

    def selected_rows(self) -> list[tuple[int, str, str]]:
        selected: list[tuple[int, str, str]] = []
        for row in range(self.table.rowCount()):
            checkbox = self.table.item(row, _COLUMN_CHECK)
            identifier = self.table.item(row, _COLUMN_ID)
            title = self.table.item(row, _COLUMN_TITLE)
            if (
                checkbox is None
                or checkbox.checkState() != Qt.CheckState.Checked
                or identifier is None
                or title is None
            ):
                continue
            thread_id = identifier.data(Qt.ItemDataRole.UserRole)
            if isinstance(thread_id, str):
                selected.append((row, thread_id, title.text()))
        return selected

    def set_title(self, row: int, title: str) -> None:
        item = self.table.item(row, _COLUMN_TITLE)
        if item is not None:
            item.setText(title)

    def show_validation_error(self, rows: Sequence[int]) -> None:
        for row in rows:
            self.set_row_status(row, "Invalid title")
        self.progress_label.setText("Correct blank or unsupported titles before confirming.")

    def set_busy(self, total: int) -> None:
        self._busy = True
        self.table.setEnabled(False)
        self.confirm_button.setEnabled(False)
        self.cancel_button.setText("Cancel after current item")
        self.progress_bar.setRange(0, max(1, total))
        self.progress_bar.setValue(0)

    def set_row_status(self, row: int, status: str) -> None:
        item = self.table.item(row, _COLUMN_STATUS)
        if item is not None:
            item.setText(status)

    def set_progress(self, done: int, total: int, renamed: int, skipped: int, failed: int) -> None:
        self.progress_bar.setValue(done)
        self.progress_label.setText(
            f"Processed {done}/{total} · Renamed {renamed} · Skipped {skipped} · Failed {failed}"
        )

    def set_completed(self) -> None:
        self._busy = False
        self._completed = True
        self.table.setEnabled(False)
        self.confirm_button.setEnabled(True)
        self.confirm_button.setText("Done")
        self.cancel_button.setText("Close")

    def reject(self) -> None:
        if self._busy:
            self.cancel_requested.emit()
            return
        super().reject()


class BulkThreadNamingController:
    def __init__(self, window: MainWindow) -> None:
        self._window = window
        self._client: Any = window._client
        self._dialog: BulkThreadNamingDialog | None = None
        self._sequence = 0
        self._requests: list[_RenameRequest] = []
        self._next_request = 0
        self._active_key: str | None = None
        self._cancel_requested = False
        self._finished = False
        self._attempted = 0
        self._done = 0
        self._renamed = 0
        self._skipped = 0
        self._failed = 0
        window.thread_pane.bulk_thread_naming_requested.connect(self.open_preview)

    @property
    def dialog(self) -> BulkThreadNamingDialog | None:
        return self._dialog

    def open_preview(self) -> None:
        if self._dialog is not None and self._dialog.isVisible():
            self._dialog.raise_()
            self._dialog.activateWindow()
            return
        self._finished = False
        proposals = build_untitled_thread_proposals(
            self._window.thread_pane.loaded_untitled_threads()
        )
        parent = self._window if isinstance(self._window, QWidget) else None
        dialog = BulkThreadNamingDialog(proposals, parent)
        self._dialog = dialog
        dialog.confirm_requested.connect(self._confirm)
        dialog.cancel_requested.connect(self._cancel)
        dialog.finished.connect(self._dialog_finished)
        dialog.show()

    def _dialog_finished(self, _result: int) -> None:
        if self._active_key is None:
            self._dialog = None

    def _confirm(self) -> None:
        dialog = self._dialog
        if dialog is None or self._active_key is not None or self._finished:
            return
        selected = dialog.selected_rows()
        if not selected:
            dialog.accept()
            return
        requests: list[_RenameRequest] = []
        invalid_rows: list[int] = []
        for row, thread_id, edited_title in selected:
            try:
                title = normalize_explicit_title(edited_title)
            except ValueError:
                invalid_rows.append(row)
                continue
            dialog.set_title(row, title)
            requests.append(_RenameRequest(row, thread_id, title))
        if invalid_rows:
            dialog.show_validation_error(invalid_rows)
            return
        self._requests = requests
        self._next_request = 0
        self._cancel_requested = False
        self._finished = False
        self._attempted = 0
        self._done = 0
        self._renamed = 0
        self._skipped = 0
        self._failed = 0
        dialog.set_busy(len(requests))
        dialog.set_progress(0, len(requests), 0, 0, 0)
        QTimer.singleShot(0, self._post_next)

    def _post_next(self) -> None:
        dialog = self._dialog
        if dialog is None or self._finished or self._active_key is not None:
            return
        if self._cancel_requested or self._next_request >= len(self._requests):
            self._finish()
            return
        current = self._requests[self._next_request]
        self._sequence += 1
        key = f"bulk-rename:{self._sequence}"
        self._active_key = key
        self._attempted += 1
        accepted = self._client.post_json(
            f"/ui-api/threads/{quote(current.thread_id, safe='')}/name",
            {"name": current.title, "only_if_untitled": True},
            key=key,
        )
        if accepted is False and self._active_key == key:
            self.handle_error(key, "")

    def handle_result(self, key: str, payload: object) -> bool:
        if key != self._active_key:
            return False
        current = self._requests[self._next_request]
        if isinstance(payload, Mapping) and payload.get("thread_id") == current.thread_id:
            status = payload.get("status")
            if status == "renamed":
                self._renamed += 1
                self._dialog_row_status(current, "Renamed")
            elif status == "skipped":
                self._skipped += 1
                self._dialog_row_status(current, "Skipped")
            else:
                self._failed += 1
                self._dialog_row_status(current, "Failed")
        else:
            self._failed += 1
            self._dialog_row_status(current, "Failed")
        self._advance()
        return True

    def handle_error(self, key: str, _message: str) -> bool:
        if key != self._active_key:
            return False
        current = self._requests[self._next_request]
        self._failed += 1
        self._dialog_row_status(current, "Failed")
        self._advance()
        return True

    def _dialog_row_status(self, request: _RenameRequest, status: str) -> None:
        if self._dialog is not None:
            self._dialog.set_row_status(request.row, status)

    def _advance(self) -> None:
        self._active_key = None
        self._next_request += 1
        self._done += 1
        if self._dialog is not None:
            self._dialog.set_progress(
                self._done,
                len(self._requests),
                self._renamed,
                self._skipped,
                self._failed,
            )
        QTimer.singleShot(0, self._post_next)

    def _cancel(self) -> None:
        if self._finished:
            return
        self._cancel_requested = True
        if self._dialog is not None:
            self._dialog.progress_label.setText("Cancellation requested; current item will finish.")
        if self._active_key is None:
            self._finish()

    def _finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        self._active_key = None
        if self._cancel_requested:
            for request in self._requests[self._next_request :]:
                self._dialog_row_status(request, "Not processed")
        if self._attempted:
            self._window._request_threads()
        if self._dialog is not None:
            self._dialog.set_completed()
