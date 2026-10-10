from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]
SETUP_HTML = ROOT / "src" / "codex_bridge" / "assets" / "codexbridge_setup_app.html"


def test_setup_app_asset_exposes_default_and_full_access_with_warning() -> None:
    html = SETUP_HTML.read_text(encoding="utf-8")

    assert 'id="access"' in html
    assert 'value="inherit"' in html
    assert 'value="danger-full-access"' in html
    assert "Default — Use Codex settings" in html
    assert "Full access — No filesystem sandbox restrictions" in html
    assert "Codex may read and modify files outside the selected working directory." in html
    assert "Approval handling remains enabled according to the Codex approval policy." in html
    assert "sandbox_mode:" in html
    assert "sandbox_mode=" in html


def test_setup_app_asset_exposes_auto_review_default_and_manual_choice() -> None:
    html = SETUP_HTML.read_text(encoding="utf-8")

    assert 'id="reviewer"' in html
    assert 'value="auto_review"' in html
    assert 'value="user"' in html
    assert "Auto-review" in html
    assert "Manual" in html
    assert "approvals_reviewer=" in html
