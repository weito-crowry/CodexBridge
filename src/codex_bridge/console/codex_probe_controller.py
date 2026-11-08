from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .codex_resolver import CodexProbeDiagnostic
from .i18n import tr

if TYPE_CHECKING:
    from .main_window import MainWindow


class CodexProbeController:
    """Own probe generations and the retry-only revalidation route."""

    def __init__(self, window: MainWindow) -> None:
        self._window = window
        self._generation = 0
        self._active: Any | None = None
        self._initial_failed = False

    def attach(self, probe: Any) -> None:
        self._generation += 1
        generation = self._generation
        self._active = probe
        probe.resolved.connect(lambda value: self._resolved(probe, generation, value))
        probe.failed.connect(lambda message: self._failed(probe, generation, message))
        if hasattr(probe, "diagnosticChanged"):
            probe.diagnosticChanged.connect(
                lambda diagnostic: self._diagnostic(probe, generation, diagnostic)
            )

    def retry_now(self, retry_existing: Any) -> None:
        window = self._window
        if window._closing:
            return
        if window._codex_resolution is not None or not self._initial_failed:
            retry_existing()
            return
        if self._active is not None and not getattr(self._active, "_finished", True):
            return
        probe = window._new_codex_probe()
        window._codex_probe = probe
        self._initial_failed = False
        window.codex_status_label.setText(tr("Codex: checking"))
        window.codex_status_label.setToolTip("")
        self.attach(probe)
        probe.start()

    def _is_current(self, probe: Any, generation: int) -> bool:
        return (
            not self._window._closing and probe is self._active and generation == self._generation
        )

    def _resolved(self, probe: Any, generation: int, value: object) -> None:
        if self._is_current(probe, generation):
            self._window._apply_codex_resolution(value)  # type: ignore[arg-type]

    def _failed(self, probe: Any, generation: int, message: str) -> None:
        if self._is_current(probe, generation):
            self._initial_failed = True
            del message
            self.apply_failure()

    def _diagnostic(self, probe: Any, generation: int, diagnostic: CodexProbeDiagnostic) -> None:
        if self._is_current(probe, generation):
            parts = [tr("Reason: {p0}", p0=diagnostic.reason)]
            if diagnostic.candidate_source:
                parts.append(
                    tr(
                        "Candidate: {p0} ({p1})",
                        p0=diagnostic.candidate_path,
                        p1=diagnostic.candidate_source,
                    )
                )
            parts.append(tr("Stage: {p0}", p0=diagnostic.stage))
            if diagnostic.process_error is not None:
                parts.append(tr("Process error: {p0}", p0=diagnostic.process_error))
            if diagnostic.exit_code is not None:
                parts.append(tr("Exit code: {p0}", p0=diagnostic.exit_code))
            parts.append(tr("Timeout: {p0}s", p0=diagnostic.timeout_seconds))
            self._window.codex_status_label.setToolTip("\n".join(parts))

    def apply_failure(self) -> None:
        window = self._window
        if window._closing:
            return
        window._codex_resolution = None
        diagnostic = getattr(self._active, "diagnostic", None)
        missing = diagnostic is None or diagnostic.reason == "no_candidate"
        window.codex_status_label.setText(
            tr("Codex: not found") if missing else tr("Codex: version verification failed")
        )
        window._update_start_button()
        window._sync_codex_update_controls()
