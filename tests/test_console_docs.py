from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_readme_documents_phase3_console_launch_and_boundaries() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    for text in (
        "uv sync --extra dev --extra console",
        "uv run codex-bridge",
        "uv run codex-bridge-console",
        "read-only",
        "Console close",
        "127.0.0.1",
        "not a Tunnel target",
    ):
        assert text in readme


def test_console_source_contains_no_mutation_or_process_ownership_operations() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in [
            ROOT / "src" / "codex_bridge" / "console_entry.py",
            *sorted((ROOT / "src" / "codex_bridge" / "console").glob("*.py")),
        ]
    )

    for forbidden in (
        "POST",
        "PUT",
        "DELETE",
        "thread/resume",
        "turn/start",
        "turn/steer",
        "turn/interrupt",
        "QProcess",
    ):
        assert forbidden not in source
