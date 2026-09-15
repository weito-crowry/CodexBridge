from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("codex_bridge")
_SAFE_FIELDS = {
    "catalog_sha256",
    "codex_version",
    "decision",
    "duration",
    "error_category",
    "error_type",
    "exposed_remote_tool_count",
    "exit_code",
    "method",
    "native_tool_count",
    "notification",
    "page",
    "provider",
    "request_id",
    "serialized_schema_bytes",
    "state",
    "thread_id",
    "tool_name",
    "total_tool_count",
    "turn_id",
    "upstream_tool_count",
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
