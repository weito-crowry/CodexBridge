from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from ..tunnel_profile import TunnelProfileError


class _ResultSignals(QObject):
    completed = Signal(object)


class _ProfileJob(QRunnable):
    def __init__(self, generate: Callable[[], object], signals: _ResultSignals) -> None:
        super().__init__()
        self._generate = generate
        self._signals = signals

    def run(self) -> None:
        error = None
        try:
            self._generate()
        except TunnelProfileError as exc:
            error = exc
        except Exception:
            error = TunnelProfileError(
                "Managed Tunnel profile generation or doctor validation failed"
            )
        self._signals.completed.emit(error)


class TunnelProfileJob(QObject):
    """Keep staged profile validation off the GUI; deliver its result on the GUI thread."""

    def __init__(
        self,
        generate: Callable[[], object],
        on_completed: Callable[[TunnelProfileError | None], None],
        *,
        parent: QObject,
    ) -> None:
        super().__init__(parent)
        self._on_completed = on_completed
        # The runnable retains this parentless emitter until the bounded doctor
        # exits, including when the window is destroyed before completion.
        self._signals = _ResultSignals()
        self._signals.completed.connect(self._completed)
        self._job = _ProfileJob(generate, self._signals)

    def start(self) -> None:
        QThreadPool.globalInstance().start(self._job)

    @Slot(object)
    def _completed(self, error: object) -> None:
        try:
            self._on_completed(error if isinstance(error, TunnelProfileError) else None)
        finally:
            self.deleteLater()
