from __future__ import annotations

import logging
from datetime import datetime
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
    "process_alive",
    "process_id",
    "reason",
    "request_id",
    "serialized_schema_bytes",
    "state",
    "target_id",
    "thread_id",
    "tool_name",
    "total_tool_count",
    "turn_id",
    "upstream_tool_count",
    "stderr_had_output",
    "stderr_line_count",
}
_SAFE_CONTROL_REASONS = frozenset({"control_api"})


class _IsoTimestampFormatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        del datefmt
        return (
            datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="milliseconds")
        )


def configure_logging() -> None:
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    formatter = _IsoTimestampFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    if not root_logger.handlers:
        root_logger.addHandler(logging.StreamHandler())
    for handler in root_logger.handlers:
        handler.setFormatter(formatter)
    logging.captureWarnings(True)


def log_event(event: str, **fields: Any) -> None:
    safe_fields: dict[str, str | int | float | bool] = {}
    for key, value in fields.items():
        if key not in _SAFE_FIELDS or not isinstance(value, (str, int, float, bool)):
            continue
        if key == "reason" and value not in _SAFE_CONTROL_REASONS:
            continue
        if key == "process_id" and (
            isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 4_294_967_295
        ):
            continue
        if key in {"process_alive", "stderr_had_output"} and not isinstance(value, bool):
            continue
        if key == "stderr_line_count" and (
            isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2_147_483_647
        ):
            continue
        safe_fields[key] = value
    details = " ".join(f"{key}={value}" for key, value in sorted(safe_fields.items()))
    logger.info("%s%s", event, f" {details}" if details else "")


def log_thread_started(thread_id: str, prompt: str | None = None, **fields: Any) -> None:
    del prompt
    log_event("thread.start", thread_id=thread_id, **fields)
