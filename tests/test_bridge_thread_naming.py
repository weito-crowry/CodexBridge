from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.bridge_test_fakes import make_bridge

pytest_plugins = ("tests.bridge_test_support",)


def _write_catalog(path: Path, thread_id: str, title: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE local_thread_catalog ("
            "host_id TEXT NOT NULL, thread_id TEXT NOT NULL, display_title TEXT NOT NULL, "
            "PRIMARY KEY (host_id, thread_id))"
        )
        connection.execute(
            "INSERT INTO local_thread_catalog (host_id, thread_id, display_title) VALUES (?, ?, ?)",
            ("local", thread_id, title),
        )


@pytest.mark.asyncio
async def test_conditional_rename_rechecks_and_skips_fresh_app_server_name(
    allowed_dir, monkeypatch
) -> None:
    import codex_bridge.bridge as bridge_module

    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_cwds["already-named"] = str(allowed_dir)
    original_request = app.request

    async def named_request(method, params):
        response = await original_request(method, params)
        if method == "thread/read":
            response["thread"]["name"] = "Fresh title"
        return response

    monkeypatch.setattr(app, "request", named_request)
    monkeypatch.setattr(bridge_module, "_LOCAL_THREAD_CATALOG_PATH", allowed_dir / "unused.db")

    result = await bridge.rename_thread("already-named", "Proposed", only_if_untitled=True)

    assert result == {"status": "skipped", "reason": "already_named"}
    assert app.calls == [("thread/read", {"threadId": "already-named", "includeTurns": False})]


@pytest.mark.asyncio
async def test_conditional_rename_skips_usable_recovered_catalog_title(
    allowed_dir, tmp_path, monkeypatch
) -> None:
    import codex_bridge.bridge as bridge_module

    catalog = tmp_path / "codex-dev.db"
    _write_catalog(catalog, "catalog-named", "Recovered title")
    monkeypatch.setattr(bridge_module, "_LOCAL_THREAD_CATALOG_PATH", catalog)
    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_cwds["catalog-named"] = str(allowed_dir)
    original_request = app.request

    async def preview_request(method, params):
        response = await original_request(method, params)
        if method == "thread/read":
            response["thread"]["preview"] = "A different request preview"
        return response

    monkeypatch.setattr(app, "request", preview_request)

    result = await bridge.rename_thread("catalog-named", "Proposed", only_if_untitled=True)

    assert result == {"status": "skipped", "reason": "already_named"}
    assert app.calls == [("thread/read", {"threadId": "catalog-named", "includeTurns": False})]


@pytest.mark.asyncio
async def test_conditional_rename_writes_only_after_fresh_untitled_check(
    allowed_dir, tmp_path, monkeypatch
) -> None:
    import codex_bridge.bridge as bridge_module

    monkeypatch.setattr(bridge_module, "_LOCAL_THREAD_CATALOG_PATH", tmp_path / "unused.db")
    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_cwds["untitled"] = str(allowed_dir)

    result = await bridge.rename_thread("untitled", "Proposed", only_if_untitled=True)

    assert result == {"status": "renamed"}
    assert app.calls == [
        ("thread/read", {"threadId": "untitled", "includeTurns": False}),
        ("thread/name/set", {"threadId": "untitled", "name": "Proposed"}),
    ]
