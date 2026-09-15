from __future__ import annotations

import logging
import subprocess
import sys

from codex_bridge.logging_utils import log_event, log_thread_started


def test_log_record_does_not_include_prompt_or_secret(caplog) -> None:
    caplog.set_level(logging.INFO)

    log_thread_started(
        thread_id="native-thread",
        prompt="do not log this prompt",
        token="do not log this secret",
    )

    assert "do not log this prompt" not in caplog.text
    assert "do not log this secret" not in caplog.text
    assert "native-thread" in caplog.text


def test_mcp_observability_fields_are_recorded_without_payloads(caplog) -> None:
    caplog.set_level(logging.INFO)

    log_event(
        "mcp.catalog.snapshot",
        provider="github",
        native_tool_count=9,
        upstream_tool_count=20,
        exposed_remote_tool_count=10,
        total_tool_count=19,
        serialized_schema_bytes=1234,
        catalog_sha256="abc123",
        tool_name="get_file_contents",
        arguments="do not log arguments",
        result="do not log result",
        pat="do not log pat",
    )

    assert "provider=github" in caplog.text
    assert "native_tool_count=9" in caplog.text
    assert "catalog_sha256=abc123" in caplog.text
    assert "do not log arguments" not in caplog.text
    assert "do not log result" not in caplog.text
    assert "do not log pat" not in caplog.text


def test_smoke_requires_explicit_opt_in() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/integration_smoke.py", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
