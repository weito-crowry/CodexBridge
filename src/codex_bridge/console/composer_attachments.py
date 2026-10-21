from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLayoutItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QWidget,
)

from ..attachment_policy import MAX_IMAGE_FILE_BYTES, MAX_OTHER_FILE_BYTES, MAX_TOTAL_BYTES

_MAX_ATTACHMENTS = 8
_CHIP_LABEL_WIDTH = 180
_COPY_CHUNK_BYTES = 1024 * 1024
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg"})


class _FlowLayout(QLayout):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setContentsMargins(2, 2, 2, 2)
        self.setSpacing(5)
        self._items: list[QLayoutItem] = []

    def addItem(self, item: QLayoutItem) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> QLayoutItem | None:
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientation:
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(margins.left() + margins.right(), margins.top() + margins.bottom())
        return size

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        margins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        x = area.x()
        y = area.y()
        line_height = 0
        for item in self._items:
            size = item.sizeHint()
            next_x = x + size.width() + self.spacing()
            if next_x - self.spacing() > area.right() and line_height > 0:
                x = area.x()
                y += line_height + self.spacing()
                next_x = x + size.width() + self.spacing()
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(x, y, size.width(), size.height()))
            x = next_x
            line_height = max(line_height, size.height())
        return y + line_height - rect.y() + margins.bottom()


class _AttachmentChip(QFrame):
    remove_requested = Signal(str)

    def __init__(self, path: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("composerAttachmentChip")
        self.setToolTip(path)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(7, 2, 3, 2)
        layout.setSpacing(3)
        name = Path(path).name or path
        self.name_label = QLabel(
            QFontMetrics(self.font()).elidedText(
                name, Qt.TextElideMode.ElideMiddle, _CHIP_LABEL_WIDTH
            )
        )
        self.name_label.setObjectName("composerAttachmentName")
        self.name_label.setToolTip(path)
        self.name_label.setMinimumWidth(0)
        self.name_label.setMaximumWidth(_CHIP_LABEL_WIDTH)
        self.name_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.remove_button = QToolButton(self)
        self.remove_button.setObjectName("composerAttachmentRemove")
        self.remove_button.setText("×")
        self.remove_button.setToolTip("下書きから削除")
        self.remove_button.setAccessibleName(f"{name}を添付から削除")
        self.remove_button.clicked.connect(lambda: self.remove_requested.emit(path))
        layout.addWidget(self.name_label)
        layout.addWidget(self.remove_button)


class ComposerAttachmentBar(QWidget):
    attachments_changed = Signal(object)
    validation_notice = Signal(str)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        allowed_roots: Sequence[str] = (),
        staging_directory: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("composerAttachmentBar")
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._attachments: tuple[str, ...] = ()
        self._chips: list[_AttachmentChip] = []
        self._allowed_roots = tuple(allowed_roots)
        self._staging_directory = staging_directory

        self.add_button = QPushButton("+")
        self.add_button.setObjectName("composerAttachmentAdd")
        self.add_button.setToolTip("ローカルファイルを添付")
        self.add_button.setAccessibleName("ファイルを添付")
        self.add_button.clicked.connect(self._open_file_dialog)

        self.scroll_area = QScrollArea(self)
        self.scroll_area.setObjectName("composerAttachmentScroll")
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll_area.setMaximumHeight(86)
        self.scroll_area.setMinimumHeight(0)
        self.chip_container = QWidget(self.scroll_area)
        self.chip_layout = _FlowLayout(self.chip_container)
        self.scroll_area.setWidget(self.chip_container)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self.add_button)
        layout.addWidget(self.scroll_area, 1)
        self.scroll_area.hide()

    @property
    def attachments(self) -> tuple[str, ...]:
        return self._attachments

    def set_attachments(self, paths: Sequence[str]) -> None:
        self._attachments = tuple(paths)
        self._render_chips()

    def add_paths(self, paths: Sequence[str]) -> None:
        existing = list(self._attachments)
        seen = {os.path.normcase(path) for path in existing}
        invalid = duplicate = overflow = False
        size_errors: list[str] = []
        staged: list[str] = []
        stage_errors: list[str] = []
        staged_sources: set[str] = set()
        total_size = 0
        for path in existing:
            try:
                total_size += Path(path).stat().st_size
            except OSError:
                continue
        for raw_path in paths:
            if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
                invalid = True
                continue
            try:
                candidate = Path(raw_path).resolve(strict=True)
                candidate_stat = candidate.stat()
                if not stat.S_ISREG(candidate_stat.st_mode):
                    invalid = True
                    continue
            except (OSError, RuntimeError):
                invalid = True
                continue
            canonical = os.fspath(candidate)
            if len(existing) >= _MAX_ATTACHMENTS:
                overflow = True
                continue
            external = bool(self._allowed_roots) and not self._is_under_allowed_root(canonical)
            if external:
                source_key = os.path.normcase(canonical)
                if source_key in staged_sources:
                    duplicate = True
                    continue
                staged_sources.add(source_key)
            elif os.path.normcase(canonical) in seen:
                duplicate = True
                continue
            is_image = candidate.suffix.casefold() in _IMAGE_SUFFIXES
            file_limit = MAX_IMAGE_FILE_BYTES if is_image else MAX_OTHER_FILE_BYTES
            file_limit_message = (
                "画像添付は20 MiB以下にしてください"
                if is_image
                else "その他の添付は50 MiB以下にしてください"
            )
            if candidate_stat.st_size > file_limit:
                size_errors.append(file_limit_message)
                continue
            if total_size + candidate_stat.st_size > MAX_TOTAL_BYTES:
                size_errors.append("添付ファイルの合計は100 MiB以下にしてください")
                continue
            if external:
                try:
                    canonical = self._stage_external_file(
                        candidate, existing_total_bytes=total_size
                    )
                    staged.append(canonical)
                except (OSError, RuntimeError, ValueError) as exc:
                    message = str(exc)
                    if message in {
                        "画像添付は20 MiB以下にしてください",
                        "その他の添付は50 MiB以下にしてください",
                        "添付ファイルの合計は100 MiB以下にしてください",
                    }:
                        size_errors.append(message)
                        continue
                    invalid = True
                    stage_errors.append(f"添付ファイルをステージングできません: {message}")
                    continue
            normalized = os.path.normcase(canonical)
            if normalized in seen:
                duplicate = True
                continue
            if len(existing) >= _MAX_ATTACHMENTS:
                overflow = True
                continue
            seen.add(normalized)
            existing.append(canonical)
            try:
                total_size += Path(canonical).stat().st_size
            except OSError:
                total_size += candidate.stat().st_size

        updated = tuple(existing)
        if updated != self._attachments:
            self._attachments = updated
            self._render_chips()
            self.attachments_changed.emit(updated)
        messages = []
        if invalid or duplicate or overflow or size_errors:
            if invalid:
                if stage_errors:
                    messages.extend(stage_errors)
                else:
                    messages.append("存在しないファイルまたはフォルダは添付できません")
            if duplicate:
                messages.append("同じファイルは重複して添付できません")
            if overflow:
                messages.append("添付は最大8件です")
            messages.extend(dict.fromkeys(size_errors))
            self.validation_notice.emit("。".join(messages))
        if staged and not messages:
            self.validation_notice.emit(
                "許可されたフォルダーへコピーしました: " + "、".join(staged)
            )

    def _is_under_allowed_root(self, path: str) -> bool:
        normalized = os.path.normcase(path)
        for value in self._allowed_roots:
            try:
                root = os.path.normcase(os.fspath(Path(value).resolve(strict=True)))
                if os.path.commonpath((root, normalized)) == root:
                    return True
            except (OSError, ValueError):
                continue
        return False

    def _stage_external_file(self, source: Path, *, existing_total_bytes: int = 0) -> str:
        image = source.suffix.casefold() in _IMAGE_SUFFIXES
        file_limit = MAX_IMAGE_FILE_BYTES if image else MAX_OTHER_FILE_BYTES
        file_limit_message = (
            "画像添付は20 MiB以下にしてください"
            if image
            else "その他の添付は50 MiB以下にしてください"
        )
        source_size = source.stat().st_size
        if source_size > file_limit:
            raise ValueError(file_limit_message)
        total_limit = MAX_TOTAL_BYTES - existing_total_bytes
        if total_limit < 0 or source_size > total_limit:
            raise ValueError("添付ファイルの合計は100 MiB以下にしてください")
        copy_limit = min(file_limit, total_limit)
        over_limit_message = (
            file_limit_message
            if file_limit <= total_limit
            else "添付ファイルの合計は100 MiB以下にしてください"
        )
        roots = tuple(Path(value).resolve(strict=True) for value in self._allowed_roots)
        if not roots or any(not root.is_dir() for root in roots):
            raise ValueError("allowed root is unavailable")
        staging = (
            Path(self._staging_directory)
            if self._staging_directory is not None
            else roots[0] / ".codexbridge-staging"
        )
        prospective = os.path.normcase(os.fspath(staging.resolve(strict=False)))
        allowed = False
        for root in roots:
            canonical_root = os.path.normcase(os.fspath(root))
            try:
                if os.path.commonpath((canonical_root, prospective)) == canonical_root:
                    allowed = True
                    break
            except ValueError:
                continue
        if not allowed:
            raise ValueError("staging directory is outside allowed roots")
        staging.mkdir(parents=True, exist_ok=True)
        canonical_staging = staging.resolve(strict=True)
        canonical_target = os.path.normcase(os.fspath(canonical_staging))
        if not any(
            os.path.commonpath((os.path.normcase(os.fspath(root)), canonical_target))
            == os.path.normcase(os.fspath(root))
            for root in roots
        ):
            raise ValueError("staging directory escaped the allowed root")

        descriptor, temporary_name = tempfile.mkstemp(prefix=".incoming-", dir=canonical_staging)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as destination, source.open("rb") as origin:
                copied = 0
                while True:
                    remaining = copy_limit - copied
                    chunk = origin.read(min(_COPY_CHUNK_BYTES, remaining + 1))
                    if not chunk:
                        break
                    if len(chunk) > remaining:
                        raise ValueError(over_limit_message)
                    destination.write(chunk)
                    copied += len(chunk)
                destination.flush()
                os.fsync(destination.fileno())
            stem = source.stem
            suffix = source.suffix
            index = 0
            while True:
                name = source.name if index == 0 else f"{stem} ({index}){suffix}"
                target = canonical_staging / name
                try:
                    os.link(temporary, target)
                    return os.fspath(target.resolve(strict=True))
                except FileExistsError:
                    index += 1
        finally:
            temporary.unlink(missing_ok=True)

    def _open_file_dialog(self) -> None:
        remaining = _MAX_ATTACHMENTS - len(self._attachments)
        if remaining <= 0:
            self.validation_notice.emit("添付は最大8件です")
            return
        paths, _selected_filter = QFileDialog.getOpenFileNames(
            self, "ファイルを添付", "", "すべてのファイル (*)"
        )
        self.add_paths(paths)

    def _remove(self, path: str) -> None:
        updated = tuple(item for item in self._attachments if item != path)
        if updated == self._attachments:
            return
        self._attachments = updated
        self._render_chips()
        self.attachments_changed.emit(updated)

    def _render_chips(self) -> None:
        while self.chip_layout.count():
            item = self.chip_layout.takeAt(0)
            if item is None:
                continue
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._chips = []
        for path in self._attachments:
            chip = _AttachmentChip(path, self.chip_container)
            chip.remove_requested.connect(self._remove)
            self.chip_layout.addWidget(chip)
            self._chips.append(chip)
        visible = bool(self._attachments)
        self.scroll_area.setVisible(visible)
        self.updateGeometry()
