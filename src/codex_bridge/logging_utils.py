from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("codex_bridge")
_SAFE_FIELDS = {
    "codex_version",
    "decision",
    "error_type",
    "exit_code",
    "method",
    "request_id",
    "state",
    "thread_id",
    "turn_id",
}


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def log_event(event: str, **fields: Any) -> None:
    safe_fields = {
        key: value
        for key, value in fields.items()
        if key in _SAFE_FIELDS and isinstance(value, (str, int, float, bool))
    }
    details = " ".join(f"{key}={value}" for key, value in sorted(safe_fields.items()))
    logger.info("%s%s", event, f" {details}" if details else "")


def log_thread_started(thread_id: str, prompt: str | None = None, **fields: Any) -> None:
    del prompt
    log_event("thread.start", thread_id=thread_id, **fields)
