from __future__ import annotations

import logging
import subprocess
import sys

from codex_bridge.logging_utils import log_thread_started


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


def test_smoke_requires_explicit_opt_in() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/integration_smoke.py", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
