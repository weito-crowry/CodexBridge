from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QInputMethodEvent, QKeyEvent, QResizeEvent, QTextOption
from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .composer_attachments import ComposerAttachmentBar


class ComposerInput(QPlainTextEdit):
    send_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("composerInput")
        self.setPlaceholderText("Codexと一緒に作業する")
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        option = self.document().defaultTextOption()
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.document().setDefaultTextOption(option)
        self.setMinimumHeight(70)
        self.setMaximumHeight(150)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._preedit_active = False
        self._ime_commit_pending = False

    def inputMethodEvent(self, event: QInputMethodEvent) -> None:
        preedit = event.preeditString()
        if preedit:
            self._preedit_active = True
        else:
            self._preedit_active = False
            if event.commitString():
                self._ime_commit_pending = True
                QTimer.singleShot(0, self._clear_ime_commit_pending)
        super().inputMethodEvent(event)

    def _clear_ime_commit_pending(self) -> None:
        self._ime_commit_pending = False

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter}:
            if event.isAutoRepeat():
                event.accept()
                return
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                super().keyPressEvent(event)
                return
            if self._preedit_active or self._ime_commit_pending:
                self._ime_commit_pending = False
                event.accept()
                return
            self.send_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class ComposerWidget(QWidget):
    send_requested = Signal()
    stop_requested = Signal()
    new_thread_requested = Signal()
    cwd_changed = Signal(str)
    model_changed = Signal()
    effort_changed = Signal()
    access_changed = Signal()
    attachments_changed = Signal(object)
    attachment_notice = Signal(str)

    def __init__(
        self,
        allowed_roots: Sequence[str] = (),
        parent: QWidget | None = None,
        *,
        attachment_staging_directory: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("composerRoot")
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self._new_draft = False
        self._rendering = False
        self._allowed_roots = tuple(allowed_roots)

        self.target_label = QLabel("スレッドを選択するか、新しいスレッドを開始してください")
        self.target_label.setObjectName("composerTarget")
        self.target_label.setWordWrap(True)
        self.target_label.setMinimumWidth(0)
        self.target_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.new_thread_button = QPushButton("New thread")
        self.new_thread_button.setObjectName("composerNewThread")
        self.new_thread_button.clicked.connect(self.new_thread_requested.emit)

        target_row = QHBoxLayout()
        target_row.addWidget(self.target_label, 1)
        target_row.addWidget(self.new_thread_button)

        self.cwd_label = QLabel("CWD")
        self.cwd_combo = QComboBox()
        self.cwd_combo.setObjectName("composerCwd")
        self.cwd_combo.setEditable(True)
        self.cwd_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.cwd_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.cwd_combo.setMinimumContentsLength(8)
        self.cwd_combo.setMinimumWidth(0)
        cwd_line_edit = self.cwd_combo.lineEdit()
        if cwd_line_edit is not None:
            cwd_line_edit.setPlaceholderText("作業フォルダを選択")
        self.cwd_combo.currentTextChanged.connect(self._emit_cwd_changed)
        self.cwd_container = QWidget(self)
        cwd_row = QHBoxLayout(self.cwd_container)
        cwd_row.setContentsMargins(0, 0, 0, 0)
        cwd_row.addWidget(self.cwd_label)
        cwd_row.addWidget(self.cwd_combo, 1)
        self.cwd_container.hide()
        self.set_cwd_options(allowed_roots, None)

        self.input = ComposerInput(self)
        self.input.send_requested.connect(self.send_requested.emit)
        self.attachment_bar = ComposerAttachmentBar(
            self,
            allowed_roots=self._allowed_roots,
            staging_directory=attachment_staging_directory,
        )
        self.attachment_bar.attachments_changed.connect(self.attachments_changed.emit)
        self.attachment_bar.validation_notice.connect(self._on_attachment_notice)
        self.attachment_add_button = self.attachment_bar.add_button

        self.access_combo = QComboBox()
        self.access_combo.setObjectName("composerAccess")
        self.access_combo.addItem("Access: Default", "inherit")
        self.access_combo.addItem("Access: Full Access", "danger-full-access")
        self.access_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContentsOnFirstShow
        )
        self.access_combo.currentIndexChanged.connect(self._emit_access_changed)

        self.model_combo = QComboBox()
        self.model_combo.setObjectName("composerModel")
        self.model_combo.addItem("Model: Default", None)
        self.model_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContentsOnFirstShow)
        self.model_combo.currentIndexChanged.connect(self._emit_model_changed)

        self.effort_combo = QComboBox()
        self.effort_combo.setObjectName("composerEffort")
        self.effort_combo.addItem("Effort: Default", None)
        self.effort_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContentsOnFirstShow
        )
        for combo in (self.access_combo, self.model_combo, self.effort_combo):
            combo.setMinimumWidth(0)
            combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.effort_combo.currentIndexChanged.connect(self._emit_effort_changed)

        self.send_button = QPushButton("Send")
        self.send_button.setObjectName("composerSend")
        self.send_button.setEnabled(False)
        self.send_button.clicked.connect(self.send_requested.emit)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setObjectName("composerStop")
        self.stop_button.setEnabled(False)
        self.stop_button.setVisible(False)
        self.stop_button.clicked.connect(self.stop_requested.emit)

        self.controls_container = QWidget(self)
        self.controls_grid = QGridLayout(self.controls_container)
        self.controls_grid.setContentsMargins(0, 0, 0, 0)
        self.controls_grid.setHorizontalSpacing(6)
        self.controls_grid.setVerticalSpacing(6)
        self._compact_controls: bool | None = None
        self._set_controls_compact(False)

        self.status_label = QLabel("")
        self.status_label.setObjectName("composerStatus")
        self.status_label.setWordWrap(True)
        self.status_label.setMinimumWidth(0)

        layout = QVBoxLayout(self)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)
        layout.addLayout(target_row)
        layout.addWidget(self.cwd_container)
        layout.addWidget(self.input)
        layout.addWidget(self.attachment_bar)
        layout.addWidget(self.controls_container)
        layout.addWidget(self.status_label)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._update_controls_layout(event.size().width())

    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        return QSize(0, hint.height())

    @staticmethod
    def _refresh_combo_size_hint(combo: QComboBox) -> None:
        # Invalidate the cached hint while retaining expansion into the stretched cell.
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContentsOnFirstShow)
        combo.updateGeometry()

    def _update_controls_layout(self, width: int | None = None) -> None:
        available_width = self.width() if width is None else width
        outer_layout = self.layout()
        margins = outer_layout.contentsMargins() if outer_layout is not None else None
        horizontal_margins = margins.left() + margins.right() if margins is not None else 0
        spacing = max(self.controls_grid.horizontalSpacing(), 0)
        controls: list[QWidget] = [self.access_combo, self.model_combo, self.effort_combo]
        if not self.stop_button.isHidden():
            controls.append(self.stop_button)
        controls.append(self.send_button)
        single_row_width = sum(control.sizeHint().width() for control in controls)
        single_row_width += 4 * spacing
        single_row_width += horizontal_margins
        self._set_controls_compact(available_width < single_row_width)

    def _set_controls_compact(self, compact: bool) -> None:
        if compact == self._compact_controls:
            return
        self._compact_controls = compact
        while self.controls_grid.count():
            self.controls_grid.takeAt(0)
        for column in range(5):
            self.controls_grid.setColumnStretch(column, 0)
        if compact:
            self.controls_grid.addWidget(self.access_combo, 0, 0, 1, 2)
            self.controls_grid.addWidget(self.model_combo, 1, 0, 1, 2)
            self.controls_grid.addWidget(self.effort_combo, 2, 0, 1, 2)
            button_row = 3
        else:
            self.controls_grid.addWidget(self.access_combo, 0, 0)
            self.controls_grid.addWidget(self.model_combo, 0, 1)
            self.controls_grid.addWidget(self.effort_combo, 0, 2)
            self.controls_grid.setColumnStretch(0, 1)
            self.controls_grid.setColumnStretch(1, 2)
            self.controls_grid.setColumnStretch(2, 1)
            button_row = 0
        self.controls_grid.addWidget(self.stop_button, button_row, 3 if not compact else 0)
        self.controls_grid.addWidget(self.send_button, button_row, 4 if not compact else 1)

    @property
    def is_new_draft(self) -> bool:
        return self._new_draft

    @property
    def prompt_text(self) -> str:
        return self.input.toPlainText()

    @property
    def attachments(self) -> tuple[str, ...]:
        return self.attachment_bar.attachments

    @property
    def cwd_value(self) -> str:
        return self.cwd_combo.currentText()

    @property
    def selected_model(self) -> str | None:
        value = self.model_combo.currentData()
        return value if isinstance(value, str) else None

    @property
    def selected_effort(self) -> str | None:
        value = self.effort_combo.currentData()
        return value if isinstance(value, str) else None

    @property
    def selected_access(self) -> str:
        value = self.access_combo.currentData()
        return value if isinstance(value, str) else "inherit"

    def set_prompt_text(self, value: str) -> None:
        if self.input.toPlainText() == value:
            return
        self.input.blockSignals(True)
        self.input.setPlainText(value)
        self.input.blockSignals(False)

    def set_attachments(self, paths: Sequence[str]) -> None:
        self.attachment_bar.set_attachments(paths)

    def add_attachments(self, paths: Sequence[str]) -> None:
        self.attachment_bar.add_paths(paths)

    def set_target(self, text: str) -> None:
        self.target_label.setText(text)

    def set_draft_mode(self, enabled: bool) -> None:
        self._new_draft = enabled
        self.cwd_container.setVisible(enabled)
        self.access_combo.setEnabled(enabled)
        self.model_combo.setEnabled(enabled and self.model_combo.count() > 1)
        self.effort_combo.setEnabled(enabled and self.selected_model is not None)

    def set_cwd_options(self, values: Sequence[str], selected: str | None) -> None:
        self._rendering = True
        self.cwd_combo.blockSignals(True)
        self.cwd_combo.clear()
        self.cwd_combo.addItem("", None)
        for value in dict.fromkeys((*self._allowed_roots, *values)):
            if value:
                self.cwd_combo.addItem(value, value)
        if selected:
            index = self.cwd_combo.findText(selected)
            if index < 0:
                self.cwd_combo.addItem(selected, selected)
                index = self.cwd_combo.count() - 1
            self.cwd_combo.setCurrentIndex(index)
            self.cwd_combo.setEditText(selected)
        else:
            self.cwd_combo.setCurrentIndex(0)
            self.cwd_combo.setEditText("")
        self.cwd_combo.blockSignals(False)
        self._rendering = False

    def set_model_options(
        self, models: Sequence[tuple[str, str]], selected: str | None, *, available: bool
    ) -> None:
        self._rendering = True
        self.model_combo.setToolTip("")
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        self.model_combo.addItem(
            "Model: Default" if available else "Model: Default (catalog unavailable)", None
        )
        for label, model_id in models:
            self.model_combo.addItem(label, model_id)
        index = self.model_combo.findData(selected)
        self.model_combo.setCurrentIndex(max(index, 0))
        self.model_combo.setEnabled(self._new_draft and available)
        self.model_combo.blockSignals(False)
        self._rendering = False
        self._refresh_combo_size_hint(self.model_combo)
        self._update_controls_layout()

    def set_effort_options(self, efforts: Sequence[tuple[str, str]], selected: str | None) -> None:
        self._rendering = True
        self.effort_combo.setToolTip("")
        self.effort_combo.blockSignals(True)
        self.effort_combo.clear()
        self.effort_combo.addItem("Effort: Default", None)
        for label, effort_id in efforts:
            self.effort_combo.addItem(label, effort_id)
        index = self.effort_combo.findData(selected)
        self.effort_combo.setCurrentIndex(max(index, 0))
        self.effort_combo.setEnabled(self._new_draft and self.selected_model is not None)
        self.effort_combo.blockSignals(False)
        self._rendering = False
        self._refresh_combo_size_hint(self.effort_combo)
        self._update_controls_layout()

    def set_access(self, mode: str) -> None:
        self._rendering = True
        self.access_combo.blockSignals(True)
        self.access_combo.setToolTip("")
        if mode not in {"inherit", "danger-full-access"}:
            self.access_combo.clear()
            self.access_combo.addItem(f"Access: {mode or 'Unknown'}", None)
            self.access_combo.setEnabled(False)
        else:
            self.access_combo.setItemText(0, "Access: Default")
            self.access_combo.setItemData(0, "inherit")
            if self.access_combo.count() == 1:
                self.access_combo.addItem("Access: Full Access", "danger-full-access")
            self.access_combo.setEnabled(self._new_draft)
            self.access_combo.setCurrentIndex(0 if mode == "inherit" else 1)
        self.access_combo.blockSignals(False)
        self._rendering = False
        self._refresh_combo_size_hint(self.access_combo)
        self._update_controls_layout()

    def set_readonly_metadata(
        self,
        model: str | None,
        effort: str | None,
        sandbox_mode: str | None,
        *,
        model_tooltip: str | None = None,
        effort_tooltip: str | None = None,
        access_tooltip: str | None = None,
    ) -> None:
        self.set_draft_mode(False)
        self.set_model_options([], None, available=False)
        self.model_combo.setItemText(0, f"Model: {model or 'Unknown'}")
        self.model_combo.setToolTip(model_tooltip or "")
        self._refresh_combo_size_hint(self.model_combo)
        self.effort_combo.blockSignals(True)
        self.effort_combo.clear()
        self.effort_combo.addItem(f"Effort: {effort or 'Unknown'}", None)
        self.effort_combo.setToolTip(effort_tooltip or "")
        self.effort_combo.setEnabled(False)
        self.effort_combo.blockSignals(False)
        self._refresh_combo_size_hint(self.effort_combo)
        self._update_controls_layout()
        self.set_access(sandbox_mode or "Unknown")
        self.access_combo.setToolTip(access_tooltip or "")

    def set_status(self, text: str, *, error: bool = False) -> None:
        self.status_label.setText(text)
        self.status_label.setProperty("error", error)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def _on_attachment_notice(self, message: str) -> None:
        self.set_status(message, error=True)
        self.attachment_notice.emit(message)

    def set_send_enabled(self, enabled: bool) -> None:
        self.send_button.setEnabled(enabled)

    def set_stop_state(self, *, visible: bool, enabled: bool) -> None:
        self.stop_button.setVisible(visible)
        self.stop_button.setEnabled(enabled)
        self._update_controls_layout()

    def _emit_cwd_changed(self, value: str) -> None:
        if not self._rendering:
            self.cwd_changed.emit(value)

    def _emit_model_changed(self, _index: int) -> None:
        if not self._rendering:
            self.model_changed.emit()

    def _emit_effort_changed(self, _index: int) -> None:
        if not self._rendering:
            self.effort_changed.emit()

    def _emit_access_changed(self, _index: int) -> None:
        if not self._rendering:
            self.access_changed.emit()
