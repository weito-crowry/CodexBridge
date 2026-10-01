from __future__ import annotations

import json

from codex_bridge.console.diagnostics import (
    DiagnosticSource,
    DiagnosticsReader,
    default_diagnostic_sources,
)


def _reader(path, *, structured: bool, **kwargs) -> DiagnosticsReader:
    return DiagnosticsReader(
        sources=(DiagnosticSource("Bridge" if structured else "Bridge stdout", path, structured),),
        **kwargs,
    )


def _drain(reader: DiagnosticsReader, *, attempts: int = 100) -> list[str]:
    result: list[str] = []
    for _ in range(attempts):
        result.extend(reader.poll())
        if reader.is_caught_up:
            break
    return result


def test_bridge_observability_jsonl_formats_safe_allowlisted_fields(tmp_path) -> None:
    path = tmp_path / "bridge-observability.jsonl"
    path.write_text(
        json.dumps(
            {
                "timestamp": "2026-09-30T06:00:05Z",
                "event": "mcp.request.end",
                "pid": 123,
                "method": "POST",
                "path": "/mcp",
                "status": 200,
                "duration_ms": 18.4,
                "prompt": "must-not-display",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    reader = _reader(path, structured=True)

    lines = _drain(reader)

    assert lines == [
        (
            "2026-09-30T06:00:05Z [Bridge] mcp.request.end method=POST "
            "path=/mcp status=200 duration_ms=18.4"
        )
    ]
    assert "must-not-display" not in "\n".join(lines)


def test_default_sources_are_limited_to_safe_observability_and_bridge_console_output() -> None:
    sources = default_diagnostic_sources()

    assert [source.label for source in sources] == [
        "Bridge",
        "Console",
        "Bridge stdout",
        "Bridge stderr",
    ]
    assert [source.path.name for source in sources] == [
        "bridge-observability.jsonl",
        "console-observability.jsonl",
        "bridge-runtime-stdout.log",
        "bridge-runtime-stderr.log",
    ]
    assert [source.structured for source in sources] == [True, True, False, False]


def test_console_observability_jsonl_uses_console_source_label(tmp_path) -> None:
    path = tmp_path / "console-observability.jsonl"
    path.write_text(
        '{"timestamp":"2026-09-30T06:00:08Z","event":"tunnel.start","pid":9}\n',
        encoding="utf-8",
    )
    reader = _reader(path, structured=True)
    reader.sources = (DiagnosticSource("Console", path, True),)

    assert _drain(reader) == ["2026-09-30T06:00:08Z [Console] tunnel.start"]


def test_malformed_json_is_skipped_and_reader_continues(tmp_path) -> None:
    path = tmp_path / "bridge-observability.jsonl"
    path.write_text(
        '{broken json}\n{"timestamp":"2026-09-30T06:00:01Z","event":"server.start","secret":"x"}\n',
        encoding="utf-8",
    )
    reader = _reader(path, structured=True)

    assert _drain(reader) == ["2026-09-30T06:00:01Z [Bridge] server.start"]


def test_runtime_stdout_has_source_prefix_and_safe_utf8_replacement(tmp_path) -> None:
    path = tmp_path / "bridge-runtime-stdout.log"
    path.write_bytes(b"2026-09-30 startup\ninvalid-\xff-byte\n")
    reader = _reader(path, structured=False)

    lines = _drain(reader)

    assert lines == [
        "[Bridge stdout] 2026-09-30 startup",
        "[Bridge stdout] invalid-\ufffd-byte",
    ]


def test_runtime_stderr_has_source_prefix(tmp_path) -> None:
    path = tmp_path / "bridge-runtime-stderr.log"
    path.write_text("warning: example\n", encoding="utf-8")
    reader = _reader(path, structured=False)
    reader.sources = (DiagnosticSource("Bridge stderr", path, False),)

    assert _drain(reader) == ["[Bridge stderr] warning: example"]


def test_runtime_line_display_is_bounded(tmp_path) -> None:
    path = tmp_path / "bridge-runtime-stdout.log"
    path.write_text("x" * 10_000 + "\n", encoding="utf-8")
    reader = _reader(path, structured=False, max_line_chars=4096)

    lines = _drain(reader)

    assert len(lines) == 1
    assert len(lines[0]) <= 4096


def test_initial_tail_reads_only_recent_file_bytes(tmp_path) -> None:
    path = tmp_path / "bridge-runtime-stdout.log"
    path.write_text(
        "".join(f"old-{index:02d}\n" for index in range(40)) + "last-entry\n",
        encoding="utf-8",
    )
    reader = _reader(path, structured=False, initial_tail_bytes=80, max_poll_bytes=80)

    lines = _drain(reader)

    assert lines
    assert "last-entry" in lines[-1]
    assert all("old-00" not in line and "old-01" not in line for line in lines)


def test_incremental_tail_does_not_reemit_existing_lines(tmp_path) -> None:
    path = tmp_path / "bridge-runtime-stdout.log"
    path.write_text("first\n", encoding="utf-8")
    reader = _reader(path, structured=False)

    assert _drain(reader) == ["[Bridge stdout] first"]
    assert reader.poll() == []
    with path.open("a", encoding="utf-8") as stream:
        stream.write("second\n")

    assert _drain(reader) == ["[Bridge stdout] second"]


def test_reader_recovers_after_truncation(tmp_path) -> None:
    path = tmp_path / "bridge-observability.jsonl"
    path.write_text('{"event":"server.start"}\n{"event":"server.shutdown"}\n', encoding="utf-8")
    reader = _reader(path, structured=True)
    assert len(_drain(reader)) == 2
    path.write_text('{"event":"server.start"}\n', encoding="utf-8")

    assert _drain(reader) == ["[Bridge] server.start"]


def test_missing_file_can_be_created_later(tmp_path) -> None:
    path = tmp_path / "created-later.log"
    reader = _reader(path, structured=False)

    assert reader.poll() == []
    path.write_text("now present\n", encoding="utf-8")

    assert _drain(reader) == ["[Bridge stdout] now present"]


def test_file_replacement_with_same_or_larger_size_is_detected(tmp_path) -> None:
    path = tmp_path / "replace.log"
    path.write_text("original-line\n", encoding="utf-8")
    reader = _reader(path, structured=False)
    assert _drain(reader) == ["[Bridge stdout] original-line"]
    replacement = tmp_path / "replacement.log"
    replacement.write_text("replacement-entry\n", encoding="utf-8")
    replacement.replace(path)

    lines = _drain(reader)

    assert any("replacement-entry" in line for line in lines)


def test_each_poll_obeys_total_byte_bound(tmp_path) -> None:
    path = tmp_path / "large.log"
    path.write_text("line-data\n" * 1000, encoding="utf-8")
    reader = _reader(
        path,
        structured=False,
        initial_tail_bytes=4096,
        max_poll_bytes=64,
        max_chunk_bytes=16,
    )

    for _ in range(10):
        reader.poll()
        assert reader.bytes_read_last_poll <= 64
