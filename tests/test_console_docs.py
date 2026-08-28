from __future__ import annotations

import re
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


def test_phase4a_docs_describe_detection_detached_launch_and_boundaries() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    spec = (ROOT / "docs" / "superpowers" / "specs" / "2026-08-28-codexbridge-design.md").read_text(
        encoding="utf-8"
    )

    for text in (
        "Phase 4A",
        "detached",
        "sys.executable -m codex_bridge",
        "existing external Bridge is never replaced",
        "no automatic restart",
        "Tunnel/tray remain out of scope",
    ):
        assert text in readme
        assert text in spec


def test_phase4b_docs_describe_tunnel_supervision_tray_and_boundaries() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    spec = (ROOT / "docs" / "superpowers" / "specs" / "2026-08-28-codexbridge-design.md").read_text(
        encoding="utf-8"
    )

    for text in (
        "Phase 4B",
        "CODEX_BRIDGE_TUNNEL_EXECUTABLE",
        "CODEX_BRIDGE_TUNNEL_PROFILE",
        "Tunnel profile creation remains external",
        "Tunnel secrets/identity are not stored by CodexBridge",
        "window close minimizes/hides to tray when available",
        "explicit Exit stops Console-owned Tunnel",
        "Bridge remains running on Console Exit",
        "external Tunnel is never discovered/taken over",
        "automatic Tunnel restart is disabled",
        "Bridge Stop/Restart is Phase 4C",
    ):
        assert text in readme
        assert text in spec


def test_console_source_contains_no_mutation_or_process_ownership_operations() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in [
            ROOT / "src" / "codex_bridge" / "console_entry.py",
            *sorted((ROOT / "src" / "codex_bridge" / "console").glob("*.py")),
        ]
    )

    for forbidden in (
        r"\bPOST\b",
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
