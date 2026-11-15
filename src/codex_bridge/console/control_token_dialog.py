from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent, QKeyEvent, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .i18n import tr


class _GuardedTokenLineEdit(QLineEdit):
    def __init__(self, token: str, copy_token: Callable[[], None], parent: QWidget) -> None:
        super().__init__(token, parent)
        self._copy_token = copy_token
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.matches(QKeySequence.StandardKey.Copy):
            self._copy_token()
            event.accept()
            return
        super().keyPressEvent(event)


class ControlTokenDialog(QDialog):
    def __init__(
        self,
        token: str,
        is_token_current: Callable[[str], bool],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("controlTokenDialog")
        self.setWindowTitle(tr("Control API token"))
        self._token: str | None = token
        self._is_token_current = is_token_current

        layout = QVBoxLayout(self)
        self.warning_label = QLabel(
            tr(
                "This token grants Control API access. Do not share it with others. "
                "It may remain in clipboard history."
            ),
            self,
        )
        self.warning_label.setWordWrap(True)
        layout.addWidget(self.warning_label)

        self.token_edit = _GuardedTokenLineEdit(token, self._copy_token, self)
        self.token_edit.setObjectName("controlTokenValue")
        self.token_edit.setReadOnly(True)
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self.token_edit)

        buttons = QHBoxLayout()
        self.show_button = QPushButton(tr("Show"), self)
        self.show_button.setObjectName("showControlTokenButton")
        self.show_button.clicked.connect(self._toggle_visibility)
        buttons.addWidget(self.show_button)

        self.copy_button = QPushButton(tr("Copy"), self)
        self.copy_button.setObjectName("copyControlTokenButton")
        self.copy_button.clicked.connect(self._copy_token)
        buttons.addWidget(self.copy_button)

        self.close_button = QPushButton(tr("Close"), self)
        self.close_button.setObjectName("closeControlTokenButton")
        self.close_button.clicked.connect(self.close)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

        self._validation_timer = QTimer(self)
        self._validation_timer.setInterval(250)
        self._validation_timer.timeout.connect(self._refresh_token_state)
        self._validation_timer.start()
        self.resize(440, 130)

    def _refresh_token_state(self) -> bool:
        token = self._token
        if token is None or not self._is_token_current(token):
            self._clear_sensitive_value()
            return False
        return True

    def _toggle_visibility(self) -> None:
        if not self._refresh_token_state():
            return
        showing = self.token_edit.echoMode() == QLineEdit.EchoMode.Password
        self.token_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if showing else QLineEdit.EchoMode.Password
        )
        self.show_button.setText(tr("Hide") if showing else tr("Show"))

    def _copy_token(self) -> None:
        if not self._refresh_token_state():
            return
        token = self._token
        if token is None:
            return
        QApplication.clipboard().setText(token)
        self.copy_button.setText(tr("Copied"))

    def _clear_sensitive_value(self) -> None:
        self._token = None
        self._validation_timer.stop()
        self.token_edit.clear()
        self.show_button.setEnabled(False)
        self.copy_button.setEnabled(False)

    def done(self, result: int) -> None:
        self._clear_sensitive_value()
        super().done(result)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._clear_sensitive_value()
        super().closeEvent(event)
