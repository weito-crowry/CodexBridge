from __future__ import annotations

import re
import sys
from typing import BinaryIO

READ_CHUNK_BYTES = 1024
MAX_LINE_BYTES = 512
MAX_EVENTS = 8
RELAY_READ_FAILURE_EXIT_CODE = 2
READY_LINE = "workflow.stderr_relay.ready"
EVENTS_TRUNCATED_LINE = "workflow.stderr_relay.events_truncated"
LONG_LINE_DISCARDED_LINE = "workflow.stderr_relay.long_line_discarded"
STARTUP_DIAGNOSTIC_EVENT_ID = "workflow.startup_diagnostic.persistence_failed"
KNOWN_STAGES = frozenset(
    {
        "request_reserved",
        "starting_state_persisted",
        "manager_validation",
        "worker_config_write",
        "worker_spawn",
        "worker_readiness_wait",
    }
)
PERSISTENCE_FAILURE_REASON_CODES = frozenset(
    {"diagnostic_payload_too_large", "diagnostic_persistence_failed"}
)
_SAFE_EXCEPTION_CLASS = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z", re.ASCII)
_DIAGNOSTIC_EVENT = re.compile(
    rf"{re.escape(STARTUP_DIAGNOSTIC_EVENT_ID)} "
    r"stage=(?P<stage>[A-Za-z_][A-Za-z0-9_]*) "
    r"original_exception=(?P<original>none|[A-Za-z_][A-Za-z0-9_]{0,63}) "
    r"persistence_exception=(?P<persistence>[A-Za-z_][A-Za-z0-9_]{0,63})\Z",
    re.ASCII,
)


def _validated_event(raw_line: bytes) -> bytes | None:
    if raw_line.endswith(b"\r"):
        raw_line = raw_line[:-1]
    try:
        line = raw_line.decode("ascii")
    except UnicodeDecodeError:
        return None
    match = _DIAGNOSTIC_EVENT.fullmatch(line)
    if match is None:
        return None
    stage = match.group("stage")
    original = match.group("original")
    persistence = match.group("persistence")
    if (
        stage not in KNOWN_STAGES
        or (original != "none" and not _SAFE_EXCEPTION_CLASS.fullmatch(original))
        or persistence == "none"
        or (
            not _SAFE_EXCEPTION_CLASS.fullmatch(persistence)
            and persistence not in PERSISTENCE_FAILURE_REASON_CODES
        )
    ):
        return None
    return line.encode("ascii") + b"\n"


def _write_line(destination: BinaryIO, line: bytes) -> bool:
    try:
        written = destination.write(line)
        if written != len(line):
            return False
        destination.flush()
        return True
    except BaseException:
        return False


def relay_stderr(source: BinaryIO, destination: BinaryIO) -> tuple[int, bool]:
    """Drain arbitrary stderr, forwarding only bounded, validated diagnostic lines."""
    output_attached = _write_line(destination, READY_LINE.encode("ascii") + b"\n")
    line_buffer = bytearray()
    discarding_long_line = False
    long_line_reported = False
    events_forwarded = 0
    events_truncated = False

    try:
        read_chunk = getattr(source, "read1", None)
        while True:
            chunk = (
                read_chunk(READ_CHUNK_BYTES)
                if callable(read_chunk)
                else source.read(READ_CHUNK_BYTES)
            )
            if not chunk:
                if discarding_long_line and output_attached and not long_line_reported:
                    output_attached = _write_line(
                        destination, LONG_LINE_DISCARDED_LINE.encode("ascii") + b"\n"
                    )
                elif not discarding_long_line and line_buffer:
                    output_attached, events_forwarded, events_truncated = _forward_line(
                        bytes(line_buffer),
                        destination,
                        output_attached=output_attached,
                        events_forwarded=events_forwarded,
                        events_truncated=events_truncated,
                    )
                return 0, output_attached
            if not isinstance(chunk, bytes) or len(chunk) > READ_CHUNK_BYTES:
                return RELAY_READ_FAILURE_EXIT_CODE, output_attached

            for byte in chunk:
                if byte == 0x0A:
                    if discarding_long_line:
                        if output_attached and not long_line_reported:
                            output_attached = _write_line(
                                destination, LONG_LINE_DISCARDED_LINE.encode("ascii") + b"\n"
                            )
                            long_line_reported = True
                    else:
                        output_attached, events_forwarded, events_truncated = _forward_line(
                            bytes(line_buffer),
                            destination,
                            output_attached=output_attached,
                            events_forwarded=events_forwarded,
                            events_truncated=events_truncated,
                        )
                    line_buffer.clear()
                    discarding_long_line = False
                elif not discarding_long_line:
                    if len(line_buffer) >= MAX_LINE_BYTES:
                        line_buffer.clear()
                        discarding_long_line = True
                    else:
                        line_buffer.append(byte)
    except BaseException:
        return RELAY_READ_FAILURE_EXIT_CODE, output_attached


def _forward_line(
    raw_line: bytes,
    destination: BinaryIO,
    *,
    output_attached: bool,
    events_forwarded: int,
    events_truncated: bool,
) -> tuple[bool, int, bool]:
    event = _validated_event(raw_line)
    if event is None or not output_attached:
        return output_attached, events_forwarded, events_truncated
    if events_forwarded < MAX_EVENTS:
        output_attached = _write_line(destination, event)
        events_forwarded += 1
    elif not events_truncated:
        output_attached = _write_line(destination, EVENTS_TRUNCATED_LINE.encode("ascii") + b"\n")
        events_truncated = True
    return output_attached, events_forwarded, events_truncated


def main() -> int:
    exit_code, output_attached = relay_stderr(sys.stdin.buffer, sys.stdout.buffer)
    if not output_attached:
        try:
            sys.stdout.close()
        except BaseException:
            pass
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
