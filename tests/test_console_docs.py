from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_readme_documents_console_launch_and_boundaries() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    for text in (
        "uv sync --extra dev --extra console",
        "uv run codex-bridge",
        "uv run codex-bridge-console",
        "read-only",
        "127.0.0.1",
        "is not a tunnel target.",
    ):
        assert text in readme


def test_readme_documents_full_access_and_keeps_cwd_boundary() -> None:
    readme = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())
    for text in (
        'sandbox_mode="inherit"',
        'sandbox_mode="danger-full-access"',
        "The default is `inherit`",
        "does not disable or change the Codex approval policy",
        "configured `allowed_roots` check remains in force",
    ):
        assert text in readme


def test_readme_documents_console_approval_and_external_bridge_limits() -> None:
    readme = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())
    for text in (
        "Review and resolve pending approvals",
        "same approval handling as the `codex_approval` MCP tool",
        "without the Console-owned control token",
        "resolve those requests through an MCP client instead",
    ):
        assert text in readme


def test_readme_does_not_restore_historical_phase_labels() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for phase_label in ("Phase 3", "Phase 4A", "Phase 4B", "Phase 4C"):
        assert phase_label not in readme


def test_console_source_contains_no_mutation_or_process_ownership_operations() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in [
            ROOT / "src" / "codex_bridge" / "console_entry.py",
            *sorted((ROOT / "src" / "codex_bridge" / "console").glob("*.py")),
        ]
    )

    for forbidden in (
        r"\bPUT\b",
        r"\bDELETE\b",
        "thread/resume",
        "turn/start",
        "turn/steer",
        "turn/interrupt",
    ):
        assert re.search(forbidden, source) is None

    launcher_source = (ROOT / "src" / "codex_bridge" / "console" / "runtime_launcher.py").read_text(
        encoding="utf-8"
    )
    for forbidden in ("terminate(", "kill(", "waitFor", "stop("):
        assert forbidden not in launcher_source
