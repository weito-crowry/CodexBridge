from __future__ import annotations

import json

import pytest

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
    activity = parse_activity_event(
        SseEvent(
            event="activity",
            event_id="ignored",
            data=json.dumps({**json.loads(_payload()), "raw": "never display"}),
        )
    )

    assert activity is not None
    assert activity["activity_id"] == "activity-1"
    assert "raw" not in activity
    assert activity["details"] == {
        "exit_code": 0,
        "paths": ["src/a.py"],
        "decision": "accept",
    }


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
