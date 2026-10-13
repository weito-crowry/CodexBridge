from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from codex_bridge.activity import ActivityStore
from codex_bridge.ui_api import create_ui_app
from tests.test_ui_control import FakeBridge, config, request, route

PNG_HEADER = b"\x89PNG\r\n\x1a\n"
JPEG_HEADER = b"\xff\xd8\xff"


def _create_request(app, payload: dict[str, Any], token: str | None = "local-control-token"):
    headers = {"authorization": f"Bearer {token}"} if token else {}
    return route(app, "/ui-api/control/threads").endpoint(
        request(
            "/ui-api/control/threads",
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            authorization=headers.get("authorization"),
        )
    )


class AttachmentBridge(FakeBridge):
    async def continue_thread(
        self, thread_id: str, prompt: str, *, image_paths: tuple[str, ...] = ()
    ) -> dict[str, Any]:
        kwargs = {"image_paths": image_paths} if image_paths else {}
        self.calls.append(("continue_thread", (thread_id, prompt), kwargs))
        await self._wait_for_operation(thread_id)
        return {
            "thread_id": thread_id,
            "turn_id": "continued-turn",
            "state": "in_progress",
        }


@pytest.mark.asyncio
async def test_create_sends_images_and_quotes_other_paths_in_prompt(tmp_path: Path) -> None:
    bridge = FakeBridge()
    app = create_ui_app(bridge, ActivityStore(), config(tmp_path))
    source = tmp_path / "日本語 (review)" / "main.py"
    source.parent.mkdir()
    source.write_text("print('data stays local')", encoding="utf-8")
    image = tmp_path / "sample image.png"
    image.write_bytes(PNG_HEADER + b"png")
    payload = {
        "client_request_id": "request-attach-create-1",
        "cwd": str(tmp_path),
        "prompt": "このファイルと画像を確認してください",
        "attachments": [str(source), str(image)],
    }

    response = await _create_request(app, payload)

    assert response.status_code == 200
    call = next(call for call in bridge.calls if call[0] == "start")
    assert call[1] == (
        str(tmp_path),
        "このファイルと画像を確認してください\n\n---\n"
        "添付ファイル参照:\n- " + json.dumps(str(source.resolve()), ensure_ascii=False),
    )
    assert call[2]["image_paths"] == (str(image.resolve()),)
    assert source.read_bytes() == b"print('data stays local')"


@pytest.mark.asyncio
async def test_create_preserves_image_selection_order_and_accepts_empty_list(
    tmp_path: Path,
) -> None:
    bridge = AttachmentBridge()
    app = create_ui_app(bridge, ActivityStore(), config(tmp_path))
    images = [tmp_path / "one.jpg", tmp_path / "二番.png"]
    images[0].write_bytes(JPEG_HEADER + b"jpeg")
    images[1].write_bytes(PNG_HEADER + b"png")

    response = await _create_request(
        app,
        {
            "client_request_id": "request-attach-order-1",
            "cwd": str(tmp_path),
            "prompt": "画像を比較",
            "attachments": [str(path) for path in images],
        },
    )
    empty = await _create_request(
        app,
        {
            "client_request_id": "request-attach-empty-1",
            "cwd": str(tmp_path),
            "prompt": "text only",
            "attachments": [],
        },
    )

    assert response.status_code == empty.status_code == 200
    create_calls = [call for call in bridge.calls if call[0] == "start"]
    assert create_calls[0][2]["image_paths"] == tuple(str(path.resolve()) for path in images)
    assert create_calls[1][1][1] == "text only"
    assert "image_paths" not in create_calls[1][2]


@pytest.mark.asyncio
async def test_continue_sends_image_and_keeps_text_path_reference(tmp_path: Path) -> None:
    bridge = AttachmentBridge()
    bridge.cwd = str(tmp_path)
    app = create_ui_app(bridge, ActivityStore(), config(tmp_path))
    source = tmp_path / "config (作業).json"
    source.write_text("{}", encoding="utf-8")
    image = tmp_path / "photo.jpeg"
    image.write_bytes(JPEG_HEADER + b"jpeg")
    endpoint = route(app, "/ui-api/control/threads/{thread_id}/turns").endpoint

    response = await endpoint(
        request(
            "/ui-api/control/threads/thread-1/turns",
            body=json.dumps(
                {
                    "client_request_id": "request-attach-continue-1",
                    "prompt": "続けて確認",
                    "attachments": [str(source), str(image)],
                },
                ensure_ascii=False,
            ).encode("utf-8"),
            authorization="Bearer local-control-token",
            path_params={"thread_id": "thread-1"},
        )
    )

    assert response.status_code == 200
    call = next(call for call in bridge.calls if call[0] == "continue_thread")
    assert call[1] == (
        "thread-1",
        "続けて確認\n\n---\n添付ファイル参照:\n- "
        + json.dumps(str(source.resolve()), ensure_ascii=False),
    )
    assert call[2]["image_paths"] == (str(image.resolve()),)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_attachments",
    [
        "not-a-list",
        [1],
        ["relative.txt"],
        ["missing.txt"],
        lambda root: [str(root / "deleted-after-selection.txt")],
        lambda root: [str(root / f"{index}.txt") for index in range(9)],
    ],
)
async def test_invalid_attachment_is_400_and_does_not_start_turn(
    tmp_path: Path, bad_attachments: object
) -> None:
    bridge = FakeBridge()
    app = create_ui_app(bridge, ActivityStore(), config(tmp_path))

    response = await _create_request(
        app,
        {
            "client_request_id": "request-attach-invalid-1",
            "cwd": str(tmp_path),
            "prompt": "go",
            "attachments": bad_attachments(tmp_path)
            if callable(bad_attachments)
            else bad_attachments,
        },
    )

    assert response.status_code == 400
    assert str(tmp_path) not in response.body.decode()
    assert not any(call[0] == "start" for call in bridge.calls)


@pytest.mark.asyncio
async def test_out_of_root_attachment_is_400_before_bridge_dispatch(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    bridge = FakeBridge()
    app = create_ui_app(bridge, ActivityStore(), config(allowed))

    response = await _create_request(
        app,
        {
            "client_request_id": "request-attach-outside-1",
            "cwd": str(allowed),
            "prompt": "go",
            "attachments": [str(outside)],
        },
    )

    assert response.status_code == 400
    assert "outside.txt" not in response.body.decode()
    assert not any(call[0] == "start" for call in bridge.calls)


@pytest.mark.asyncio
async def test_attachment_list_is_in_idempotency_digest(tmp_path: Path) -> None:
    bridge = FakeBridge()
    app = create_ui_app(bridge, ActivityStore(), config(tmp_path))
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("1", encoding="utf-8")
    second.write_text("2", encoding="utf-8")
    payload = {
        "client_request_id": "request-attach-replay-1",
        "cwd": str(tmp_path),
        "prompt": "go",
        "attachments": [str(first), str(second)],
    }

    original = await _create_request(app, payload)
    replay = await _create_request(app, payload)
    changed = await _create_request(app, {**payload, "attachments": [str(second), str(first)]})

    assert original.status_code == replay.status_code == 200
    assert changed.status_code == 409
    assert len([call for call in bridge.calls if call[0] == "start"]) == 1


@pytest.mark.asyncio
async def test_continue_attachment_replay_and_changed_attachment_conflict(tmp_path: Path) -> None:
    bridge = AttachmentBridge()
    bridge.cwd = str(tmp_path)
    app = create_ui_app(bridge, ActivityStore(), config(tmp_path))
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("1", encoding="utf-8")
    second.write_text("2", encoding="utf-8")
    endpoint = route(app, "/ui-api/control/threads/{thread_id}/turns").endpoint

    async def send(paths: list[str]):
        return await endpoint(
            request(
                "/ui-api/control/threads/thread-1/turns",
                body=json.dumps(
                    {
                        "client_request_id": "request-attach-continue-replay-1",
                        "prompt": "go",
                        "attachments": paths,
                    }
                ).encode(),
                authorization="Bearer local-control-token",
                path_params={"thread_id": "thread-1"},
            )
        )

    original = await send([str(first), str(second)])
    replay = await send([str(first), str(second)])
    changed = await send([str(second), str(first)])

    assert original.status_code == replay.status_code == 200
    assert changed.status_code == 409
    assert len([call for call in bridge.calls if call[0] == "continue_thread"]) == 1


@pytest.mark.asyncio
async def test_attachment_continue_keeps_same_thread_conflict_guard(tmp_path: Path) -> None:
    bridge = AttachmentBridge()
    bridge.cwd = str(tmp_path)
    bridge.operation_gate = asyncio.Event()
    app = create_ui_app(bridge, ActivityStore(), config(tmp_path))
    image = tmp_path / "photo.jpg"
    image.write_bytes(JPEG_HEADER + b"jpeg")
    endpoint = route(app, "/ui-api/control/threads/{thread_id}/turns").endpoint

    async def send(request_id: str):
        return await endpoint(
            request(
                "/ui-api/control/threads/thread-1/turns",
                body=json.dumps(
                    {
                        "client_request_id": request_id,
                        "prompt": "go",
                        "attachments": [str(image)],
                    }
                ).encode(),
                authorization="Bearer local-control-token",
                path_params={"thread_id": "thread-1"},
            )
        )

    first = asyncio.create_task(send("request-attach-concurrent-1"))
    while not bridge.operation_entered.get("thread-1"):
        await asyncio.sleep(0)
    second = await send("request-attach-concurrent-2")
    bridge.operation_gate.set()
    first_response = await first

    assert first_response.status_code == 200
    assert second.status_code == 409
    assert len([call for call in bridge.calls if call[0] == "continue_thread"]) == 1


@pytest.mark.asyncio
async def test_invalid_token_cannot_execute_attachment_operation(tmp_path: Path) -> None:
    bridge = FakeBridge()
    app = create_ui_app(bridge, ActivityStore(), config(tmp_path))
    file_path = tmp_path / "file.txt"
    file_path.write_text("data", encoding="utf-8")

    response = await _create_request(
        app,
        {
            "client_request_id": "request-attach-token-1",
            "cwd": str(tmp_path),
            "prompt": "go",
            "attachments": [str(file_path)],
        },
        token="wrong-token",
    )

    assert response.status_code == 403
    assert not any(call[0] == "start" for call in bridge.calls)
