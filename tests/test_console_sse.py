from __future__ import annotations

import json

import pytest

from codex_bridge.activity import Activity
from codex_bridge.console.sse import SseEvent, SseParser, SseProtocolError, parse_activity_event


def _payload(**overrides: object) -> bytes:
    value: dict[str, object] = {
        "activity_id": "activity-1",
        "timestamp": "2026-08-28T00:00:00Z",
        "thread_id": "thread-1",
        "turn_id": "turn-1",
        "type": "command_completed",
        "status": "completed",
        "summary": "pytest",
        "details": {"exit_code": 0, "paths": ["src/a.py"], "decision": "accept"},
    }
    value.update(overrides)
    return json.dumps(value, ensure_ascii=False).encode()


def test_parser_emits_single_event_with_event_id_and_data() -> None:
    parser = SseParser()

    events = parser.feed(b'event: activity\nid: abc\ndata: {"ok":true}\n\n')

    assert events == (SseEvent(event="activity", event_id="abc", data='{"ok":true}'),)


def test_parser_emits_multiple_events_and_ignores_keepalive_comments() -> None:
    parser = SseParser()

    events = parser.feed(
        b": keepalive\n\nevent: activity\ndata: one\n\nevent: activity\ndata: two\n\n"
    )

    assert [event.data for event in events] == ["one", "two"]


def test_parser_handles_network_chunk_and_line_boundary_splits() -> None:
    parser = SseParser()

    assert parser.feed(b"event: act") == ()
    assert parser.feed(b"ivity\nid: a\ndata: value\n") == ()
    assert parser.feed(b"\n") == (SseEvent(event="activity", event_id="a", data="value"),)


def test_parser_handles_utf8_multibyte_character_split_between_chunks() -> None:
    parser = SseParser()
    encoded = "event: activity\ndata: 日本語\n\n".encode()
    split = encoded.index("日".encode()) + 1

    assert parser.feed(encoded[:split]) == ()
    events = parser.feed(encoded[split:])

    assert events[0].data == "日本語"


def test_parser_joins_multiple_data_lines_with_newlines() -> None:
    parser = SseParser()

    events = parser.feed(b"event: activity\ndata: first\ndata: second\n\n")

    assert events[0].data == "first\nsecond"


def test_parser_does_not_dispatch_an_incomplete_final_event() -> None:
    parser = SseParser()

    assert parser.feed(b"event: activity\ndata: incomplete\n") == ()
    assert parser.finish() == ()


@pytest.mark.parametrize(
    "data",
    [b"not-json", b"[]", b'{"activity_id":"missing-rest"}'],
)
def test_activity_parser_rejects_malformed_or_incomplete_json(data: bytes) -> None:
    activity = parse_activity_event(SseEvent(event="activity", event_id=None, data=data.decode()))

    assert activity is None


def test_activity_parser_requires_safe_fields_and_ignores_unknown_fields() -> None:
    payload = json.loads(_payload())
    payload.update(
        {
            "raw": "never display",
            "secret": "never display",
            "api_key": "never display",
        }
    )
    payload["details"]["secret"] = "never display"
    activity = parse_activity_event(
        SseEvent(
            event="activity",
            event_id="ignored",
            data=json.dumps(payload),
        )
    )

    assert activity is not None
    assert activity["activity_id"] == "activity-1"
    assert "raw" not in activity
    assert "secret" not in activity
    assert "api_key" not in activity
    assert "secret" not in activity["details"]  # type: ignore[operator]
    assert activity["details"] == {
        "exit_code": 0,
        "paths": ["src/a.py"],
        "decision": "accept",
    }


def test_activity_parser_allows_bounded_command_monitor_metadata() -> None:
    payload = json.loads(_payload())
    payload["type"] = "command_output"
    payload["summary"] = "日本語 output"
    payload["details"] = {
        "cwd": "C:/workspace",
        "process_id": "pty-42",
        "duration_ms": 25,
        "started_at_ms": 1_791_101_948_000,
        "completed_at_ms": 1_791_101_948_025,
        "output_truncated": True,
        "output_source": "completed_item",
        "raw": "not allowed",
    }

    activity = parse_activity_event(
        SseEvent(event="activity", event_id=None, data=json.dumps(payload, ensure_ascii=False))
    )

    assert activity is not None
    assert activity["details"] == {
        "cwd": "C:/workspace",
        "process_id": "pty-42",
        "duration_ms": 25,
        "started_at_ms": 1_791_101_948_000,
        "completed_at_ms": 1_791_101_948_025,
        "output_truncated": True,
        "output_source": "completed_item",
    }


def test_activity_parser_preserves_item_id_from_activity_serialization() -> None:
    item_id = "exec-8f9a6194-83db-4e07-bf44-f618c7171b4c"
    source = Activity(
        activity_id="activity-live-command",
        timestamp="2026-10-22T12:00:00Z",
        thread_id="thread-live",
        turn_id="turn-live",
        item_id=item_id,
        type="command_started",
        status="in_progress",
        summary="PowerShell live command",
        details={"process_id": "8648", "started_at_ms": 1_792_670_400_000},
    )

    parsed = parse_activity_event(
        SseEvent(
            event="activity",
            event_id=None,
            data=json.dumps(source.to_dict(), ensure_ascii=False),
        )
    )

    assert parsed is not None
    assert parsed["item_id"] == item_id
    assert parsed["thread_id"] == "thread-live"
    assert parsed["turn_id"] == "turn-live"
    assert parsed["details"]["process_id"] == "8648"  # type: ignore[index]


@pytest.mark.parametrize(
    ("include_item_id", "item_id", "expected"),
    [
        (True, None, None),
        (False, None, None),
        (True, 8648, None),
        (True, "x" * 513, None),
    ],
)
def test_activity_parser_keeps_missing_or_invalid_item_id_unassociated(
    include_item_id: bool, item_id: object, expected: None
) -> None:
    payload = json.loads(_payload())
    if include_item_id:
        payload["item_id"] = item_id

    parsed = parse_activity_event(
        SseEvent(event="activity", event_id=None, data=json.dumps(payload))
    )

    assert parsed is not None
    assert parsed["item_id"] is expected
    assert parsed["activity_id"] == "activity-1"
    assert "raw" not in parsed


def test_activity_parser_accepts_item_id_at_512_character_limit() -> None:
    item_id = "x" * 512

    parsed = parse_activity_event(
        SseEvent(event="activity", event_id=None, data=_payload(item_id=item_id).decode())
    )

    assert parsed is not None
    assert parsed["item_id"] == item_id


def test_activity_parser_keeps_bounded_notification_request_id() -> None:
    payload = json.loads(_payload())
    payload["type"] = "approval_requested"
    payload["status"] = "requested"
    payload["details"] = {"request_id": "request-1", "unknown": "ignored"}

    activity = parse_activity_event(
        SseEvent(event="activity", event_id=None, data=json.dumps(payload))
    )

    assert activity is not None
    assert activity["details"] == {"request_id": "request-1"}


def test_activity_parser_rejects_malformed_notification_request_id() -> None:
    payload = json.loads(_payload())
    payload["details"] = {"request_id": {"nested": "unexpected"}}

    activity = parse_activity_event(
        SseEvent(event="activity", event_id=None, data=json.dumps(payload))
    )

    assert activity is not None
    assert activity["details"] == {}


def test_activity_parser_rejects_missing_required_key_even_when_null_is_allowed() -> None:
    payload = json.loads(_payload())
    del payload["turn_id"]

    assert (
        parse_activity_event(SseEvent(event="activity", event_id=None, data=json.dumps(payload)))
        is None
    )


def test_parser_rejects_pending_buffer_overflow_and_discards_buffer() -> None:
    parser = SseParser(max_buffer_bytes=64)

    with pytest.raises(SseProtocolError, match="buffer"):
        parser.feed(b"data: " + b"x" * 70)

    assert parser.feed(b"event: activity\ndata: ok\n\n") == (
        SseEvent(event="activity", event_id=None, data="ok"),
    )
