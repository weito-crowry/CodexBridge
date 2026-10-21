from __future__ import annotations

import re
from collections.abc import Callable
from math import ceil
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QSize, Qt, QUrl
from PySide6.QtGui import (
    QDesktopServices,
    QImageReader,
    QPixmap,
    QResizeEvent,
    QTextCursor,
    QTextDocument,
    QTextOption,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from .widgets import TimelineEntry

_SAFE_MARKDOWN_FEATURES = (
    QTextDocument.MarkdownFeature.MarkdownDialectGitHub
    | QTextDocument.MarkdownFeature.MarkdownNoHTML
)
_STATUS_LABELS = {
    "in_progress": "Running",
    "completed": "Completed",
    "failed": "Failed",
    "interrupted": "Interrupted",
    "error": "Error",
}
_MESSAGE_OBJECT_NAMES = {
    "User": "conversationUser",
    "Agent": "conversationAgent",
    "Commentary": "conversationCommentary",
    "Plan": "conversationPlan",
}
_MAX_IMAGE_ATTACHMENTS = 8
_MAX_IMAGE_PREVIEW_BYTES = 128 * 1024 * 1024
_MAX_IMAGE_PREVIEW_PIXELS = 40_000_000
_IMAGE_PREVIEW_SIZE = QSize(320, 200)


class ConversationTextBody(QTextBrowser):
    def __init__(
        self,
        text: str,
        *,
        markdown: bool,
        object_name: str = "conversationTextBody",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName(object_name)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setReadOnly(True)
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.setWordWrapMode(QTextOption.WrapMode.WrapAnywhere)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.document().setDocumentMargin(0)
        self._set_document_text(self.document(), text, markdown)
        self._measurement_document = QTextDocument(self)
        self._measurement_document.setDocumentMargin(0)
        self._measurement_document.setDefaultFont(self.document().defaultFont())
        self._measurement_document.setDefaultTextOption(self.document().defaultTextOption())
        self._set_document_text(self._measurement_document, text, markdown)
        policy = self.sizePolicy()
        policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
        policy.setVerticalPolicy(QSizePolicy.Policy.Minimum)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    @staticmethod
    def _set_document_text(document: QTextDocument, text: str, markdown: bool) -> None:
        if markdown:
            document.setMarkdown(text, _SAFE_MARKDOWN_FEATURES)
            block = document.begin()
            while block.isValid():
                block_format = block.blockFormat()
                if block_format.nonBreakableLines():
                    block_format.setNonBreakableLines(False)
                    QTextCursor(block).setBlockFormat(block_format)
                block = block.next()
        else:
            document.setPlainText(text)

    def loadResource(self, resource_type: int, name: QUrl | str) -> object:  # noqa: N802
        del resource_type, name
        return None

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
        content_height = ceil(document.documentLayout().documentSize().height())
        if self.minimumHeight() != content_height:
            self.setMinimumHeight(max(1, content_height))


def _status_label(status: str | None) -> str:
    if status is None:
        return ""
    return _STATUS_LABELS.get(status, status)


def _short_summary(text: str) -> str:
    summary = " ".join(text.split())
    summary = re.sub(r"(\S{28})(?=\S)", lambda match: f"{match.group(1)}\u200b", summary)
    if len(summary) > 180:
        return summary[:177].rstrip() + "…"
    return summary


def _metadata_text(entry: TimelineEntry, timing: str | None) -> str:
    parts = [part for part in (_status_label(entry.status), *entry.details, timing) if part]
    return " · ".join(parts)


def _open_local_image(path: str) -> None:
    try:
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))
    except Exception:
        return


def build_image_attachment_row(paths: tuple[str, ...], parent: QWidget) -> QWidget | None:
    existing_paths: list[Path] = []
    for raw_path in paths[:_MAX_IMAGE_ATTACHMENTS]:
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            continue
        try:
            if candidate.is_file():
                existing_paths.append(candidate)
        except (OSError, RuntimeError, ValueError):
            continue
    if not existing_paths:
        return None

    row = QWidget(parent)
    row.setObjectName("conversationImageAttachments")
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 2, 0, 2)
    layout.setSpacing(8)
    for image_path in existing_paths:
        attachment = QWidget(row)
        attachment_layout = QVBoxLayout(attachment)
        attachment_layout.setContentsMargins(0, 0, 0, 0)
        attachment_layout.setSpacing(2)
        try:
            file_size = image_path.stat().st_size
            if file_size <= _MAX_IMAGE_PREVIEW_BYTES:
                reader = QImageReader(str(image_path))
                reader.setAutoTransform(True)
                image_size = reader.size()
                if (
                    image_size.isValid()
                    and image_size.width() > 0
                    and image_size.height() > 0
                    and image_size.width() * image_size.height() <= _MAX_IMAGE_PREVIEW_PIXELS
                ):
                    reader.setScaledSize(
                        image_size.scaled(_IMAGE_PREVIEW_SIZE, Qt.AspectRatioMode.KeepAspectRatio)
                    )
                    image = reader.read()
                    if not image.isNull():
                        preview = QLabel(attachment)
                        preview.setObjectName("conversationImagePreview")
                        preview.setAccessibleName(f"Image preview: {image_path.name[:160]}")
                        preview.setAlignment(
                            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                        )
                        preview.setPixmap(QPixmap.fromImage(image))
                        attachment_layout.addWidget(preview)
        except (OSError, RuntimeError, ValueError):
            pass

        open_button = QToolButton(attachment)
        open_button.setObjectName("conversationImageOpenButton")
        file_name = image_path.name[:160] or "Open image"
        open_button.setText(file_name)
        open_button.setAccessibleName(f"Open image: {file_name}")
        open_button.setToolTip("Open local image file")
        open_button.clicked.connect(
            lambda _checked=False, path=str(image_path): _open_local_image(path)
        )
        attachment_layout.addWidget(open_button)
        layout.addWidget(attachment, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
    layout.addStretch(1)
    return row


def build_conversation_row(
    entry: TimelineEntry,
    *,
    expanded: bool,
    is_running: bool,
    timing: str | None,
    spinner_factory: Callable[[QWidget], QWidget],
    copy_text: Callable[[str], None],
    on_expanded_changed: Callable[[bool], None],
) -> QWidget:
    if entry.kind in _MESSAGE_OBJECT_NAMES:
        frame = QFrame()
        frame.setObjectName(_MESSAGE_OBJECT_NAMES[entry.kind])
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(9, 7, 9, 7)
        layout.setSpacing(3)
        header = QHBoxLayout()
        label = QLabel(entry.title)
        label.setObjectName("conversationMessageKind")
        label.setTextFormat(Qt.TextFormat.PlainText)
        header.addWidget(label, 1)
        if is_running:
            spinner = spinner_factory(frame)
            header.addWidget(spinner)
        if entry.kind in {"User", "Agent", "Commentary", "Plan"}:
            button = QPushButton("Copy")
            button.setObjectName("conversationCopyMessageButton")
            button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            button.clicked.connect(lambda _checked=False, text=entry.body: copy_text(text))
            header.addWidget(button)
        layout.addLayout(header)
        if entry.body:
            layout.addWidget(ConversationTextBody(entry.body, markdown=True, parent=frame))
        image_row = build_image_attachment_row(entry.images, frame)
        if image_row is not None:
            image_row.setProperty("turnId", entry.turn_id)
            image_row.setProperty("itemId", entry.item_id)
            layout.addWidget(image_row)
        metadata = _metadata_text(entry, timing)
        if metadata:
            metadata_label = QLabel(metadata)
            metadata_label.setObjectName("conversationMetadata")
            metadata_label.setWordWrap(True)
            metadata_label.setTextFormat(Qt.TextFormat.PlainText)
            layout.addWidget(metadata_label)
        frame.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Maximum)
        return frame

    frame = QFrame()
    frame.setObjectName("conversationEvent")
    frame.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Maximum)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(5, 3, 5, 3)
    layout.setSpacing(1)
    header = QHBoxLayout()
    title = QLabel(entry.title)
    title.setObjectName("conversationEventTitle")
    title.setTextFormat(Qt.TextFormat.PlainText)
    title.setWordWrap(True)
    title_policy = title.sizePolicy()
    title_policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
    title.setSizePolicy(title_policy)
    header.addWidget(title, 0)
    summary = QLabel(_short_summary(entry.body))
    summary.setObjectName("conversationEventSummary")
    summary.setTextFormat(Qt.TextFormat.PlainText)
    summary.setWordWrap(True)
    summary.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    header.addWidget(summary, 1)
    if is_running:
        header.addWidget(spinner_factory(frame))
    layout.addLayout(header)

    metadata = _metadata_text(entry, timing)
    metadata_label = QLabel(metadata)
    metadata_label.setObjectName("conversationEventMetadata")
    metadata_label.setTextFormat(Qt.TextFormat.PlainText)
    metadata_label.setWordWrap(True)
    metadata_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    metadata_label.setVisible(bool(metadata))
    layout.addWidget(metadata_label)

    if entry.body or entry.details:
        disclosure = QPushButton("Hide details" if expanded else "Show details")
        disclosure.setObjectName("conversationDisclosure")
        disclosure.setCheckable(True)
        disclosure.setChecked(expanded)
        disclosure.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        details = QWidget(frame)
        details.setObjectName("conversationDisclosureContent")
        details_layout = QVBoxLayout(details)
        details_layout.setContentsMargins(0, 0, 0, 0)
        details_layout.setSpacing(2)
        if entry.body:
            details_layout.addWidget(
                ConversationTextBody(
                    entry.body,
                    markdown=False,
                    object_name="conversationDetailsBody",
                    parent=details,
                )
            )
        if entry.details:
            details_layout.addWidget(
                ConversationTextBody(
                    "\n".join(entry.details),
                    markdown=False,
                    object_name="conversationDetailsMetadataBody",
                    parent=details,
                )
            )
        details.setVisible(expanded)

        def set_expanded(value: bool) -> None:
            details.setVisible(value)
            disclosure.setText("Hide details" if value else "Show details")
            on_expanded_changed(value)

        disclosure.toggled.connect(set_expanded)
        layout.addWidget(disclosure)
        layout.addWidget(details)
    frame.setProperty("timelineKey", f"{entry.turn_id}:{entry.item_id}")
    return frame
