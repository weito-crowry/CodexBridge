"""Qt wrapper for the shared read-only Codex session scanner."""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from ..external_sessions import CodexSessionScanner, _Scanner, is_codexbridge_originator

__all__ = ["CodexSessionScanner", "ExternalThreadMonitor", "is_codexbridge_originator"]


class _ScanSignals(QObject):
    completed = Signal(object)


class _ScanJob(QRunnable):
    def __init__(
        self, scanner: _Scanner, stop_event: threading.Event, signals: _ScanSignals
    ) -> None:
        super().__init__()
        self._scanner = scanner
        self._stop_event = stop_event
        self._signals = signals

    def run(self) -> None:
        try:
            result = self._scanner.scan(self._stop_event)
        except Exception:
            result = {}
        self._signals.completed.emit(result)


class ExternalThreadMonitor(QObject):
    """Run session scans away from the GUI and suppress results after shutdown."""

    updated = Signal(object)

    def __init__(
        self,
        *,
        allowed_roots: Sequence[str],
        codex_home: Path | str | None = None,
        scanner: _Scanner | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._scanner = (
            scanner
            if scanner is not None
            else CodexSessionScanner(allowed_roots=allowed_roots, codex_home=codex_home)
        )
        self._stop_event = threading.Event()
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._signals = _ScanSignals(self)
        self._signals.completed.connect(self._scan_completed)
        self._busy = False
        self._stopped = False

    @property
    def is_running(self) -> bool:
        return not self._stopped

    @Slot()
    def request_scan(self) -> None:
        if self._stopped or self._busy:
            return
        self._busy = True
        self._pool.start(_ScanJob(self._scanner, self._stop_event, self._signals))

    @Slot(object)
    def _scan_completed(self, result: object) -> None:
        self._busy = False
        if self._stopped:
            return
        if isinstance(result, Mapping):
            self.updated.emit(dict(result))
        else:
            self.updated.emit({})

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._stop_event.set()
        self._pool.clear()
        self._pool.waitForDone()
