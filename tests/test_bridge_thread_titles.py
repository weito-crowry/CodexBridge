from __future__ import annotations

import pytest

from codex_bridge.bridge import BridgeError
from codex_bridge.jsonrpc import JsonRpcRemoteError
from tests.bridge_test_fakes import make_bridge

pytest_plugins = ("tests.bridge_test_support",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("title", "expected"),
    [
        (
            "  \u65e5\u672c\u8a9e   \u30bf\u30a4\u30c8\u30eb\n\u3067\u3059  ",
            "\u65e5\u672c\u8a9e \u30bf\u30a4\u30c8\u30eb \u3067\u3059",
        ),
        ("  English\tstartup   review ", "English startup review"),
    ],
)
async def test_start_normalizes_explicit_title_and_sets_it_before_turn(
    allowed_dir, title: str, expected: str
) -> None:
    bridge, app, _ = make_bridge(allowed_dir)

    result = await bridge.start(str(allowed_dir), "inspect this", title=title)

    assert app.methods == ["thread/start", "thread/name/set", "turn/start"]
    assert app.calls[1] == (
        "thread/name/set",
        {"threadId": "native-thread", "name": expected},
    )
    assert result["thread_id"] == "native-thread"
    assert result["title_assignment"] == {"status": "assigned", "name": expected}


@pytest.mark.asyncio
async def test_start_uses_meaningful_markdown_heading_before_prompt_body(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)

    await bridge.start(
        str(allowed_dir),
        "You are Codex, a helpful assistant.\n\nPlease inspect this.\n## "
        "\u65e5\u672c\u8a9e\u306e\u4fee\u6b63\u6982\u8981\n" + "body " * 100,
    )

    assert app.calls[1] == (
        "thread/name/set",
        {
            "threadId": "native-thread",
            "name": "\u65e5\u672c\u8a9e\u306e\u4fee\u6b63\u6982\u8981",
        },
    )


@pytest.mark.asyncio
async def test_start_extracts_quoted_prompt_line_and_ignores_code_fence(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)

    await bridge.start(
        str(allowed_dir),
        "```md\n# Example heading\n```\n\n> - "
        "\u201cImplement thread titles in ChatGPT\u201d\nDetails follow.",
    )

    assert app.calls[1][1]["name"] == "Implement thread titles in ChatGPT"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prompt",
    [
        "",
        "## New thread\nNo specific request",
        "## API_KEY=short-placeholder-secret\nNo specific request",
        "## API key: short-placeholder-secret\nNo specific request",
        "## " + ("A" * 50),
        "## Background\n## Context\n## Objective",
        "```text\n# Hidden code title\n```",
    ],
)
async def test_start_uses_native_id_suffix_for_empty_generic_or_unsafe_prompt(
    allowed_dir, prompt: str
) -> None:
    bridge, app, _ = make_bridge(allowed_dir)

    result = await bridge.start(str(allowed_dir), prompt)

    assert app.calls[1] == (
        "thread/name/set",
        {"threadId": "native-thread", "name": "New Codex thread (e-thread)"},
    )
    assert result["thread_id"] == "native-thread"


@pytest.mark.asyncio
async def test_start_uses_each_native_thread_id_in_fallback_title(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_id = "native-unique-2"

    await bridge.start(str(allowed_dir), "## New thread")

    assert app.calls[1][1] == {
        "threadId": "native-unique-2",
        "name": "New Codex thread (unique-2)",
    }


@pytest.mark.asyncio
async def test_start_bounds_prompt_title(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)

    await bridge.start(str(allowed_dir), "A title " * 30)

    assert len(app.calls[1][1]["name"]) == 100
    assert app.calls[1][1]["name"].startswith("A title A title")


@pytest.mark.asyncio
async def test_start_truncates_explicit_title_to_one_hundred_characters(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)

    await bridge.start(str(allowed_dir), "prompt", title="\u7814\u7a76" * 105)

    assert app.calls[1][1]["name"] == "\u7814\u7a76" * 49 + "\u7814\u2026"
    assert len(app.calls[1][1]["name"]) == 100


@pytest.mark.asyncio
@pytest.mark.parametrize("title", ["", " \t "])
async def test_start_rejects_blank_explicit_title_before_creating_thread(allowed_dir, title: str):
    bridge, app, _ = make_bridge(allowed_dir)

    with pytest.raises(ValueError, match="title must not be blank"):
        await bridge.start(str(allowed_dir), "secret prompt", title=title)

    assert app.calls == []


@pytest.mark.asyncio
async def test_start_rejects_non_whitespace_control_in_explicit_title(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)

    with pytest.raises(ValueError, match="control characters") as error:
        await bridge.start(str(allowed_dir), "prompt", title="title\x01secret")

    assert "secret" not in str(error.value)
    assert app.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [JsonRpcRemoteError("raw server detail"), OSError("raw I/O detail")]
)
async def test_title_failure_warns_but_still_starts_turn_once(
    allowed_dir, failure: Exception
) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_name_error = failure

    result = await bridge.start(str(allowed_dir), "inspect this", title="Review startup")

    assert app.methods == ["thread/start", "thread/name/set", "turn/start"]
    assert result["thread_id"] == "native-thread"
    assert result["turn_id"] == "native-turn"
    assert result["title_assignment"] == {"status": "failed"}
    assert result["title_warning"] == "Thread name could not be set"
    assert "raw" not in result["title_warning"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("settings", "reviewer", "message"),
    [
        ({"approvalsReviewer": "auto_review"}, "user", "approvals_reviewer"),
        ({"approvalPolicy": "never"}, "auto_review", "approval_policy"),
    ],
)
async def test_start_does_not_assign_title_when_thread_settings_mismatch(
    allowed_dir, settings: dict[str, str], reviewer: str, message: str
) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_start_settings = settings

    with pytest.raises(BridgeError, match=message):
        await bridge.start(
            str(allowed_dir), "inspect this", approvals_reviewer=reviewer, title="Title"
        )

    assert app.methods == ["thread/start"]
