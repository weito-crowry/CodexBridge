from __future__ import annotations

from collections.abc import Mapping

from PySide6.QtCore import QCoreApplication, QTimer

from codex_bridge.console.history_requests import (
    HistoryRequestContext,
    HistoryRequestCoordinator,
)


class FakeJsonClient:
    def __init__(self, rejected_keys: set[str] | None = None) -> None:
        self.requests: list[tuple[str, str, Mapping[str, object] | None]] = []
        self.active_keys: set[str] = set()
        self.rejected_keys = rejected_keys or set()

    def get_json(
        self,
        path: str,
        *,
        key: str,
        query: Mapping[str, object] | None = None,
    ) -> bool:
        self.requests.append((key, path, query))
        if key in self.rejected_keys or key in self.active_keys:
            return False
        self.active_keys.add(key)
        return True


def _coordinator(
    client: FakeJsonClient,
) -> tuple[HistoryRequestCoordinator, QTimer]:
    QCoreApplication.instance() or QCoreApplication([])
    timer = QTimer()
    timer.setSingleShot(True)
    timer.setInterval(250)
    return HistoryRequestCoordinator(client, timer), timer


def _finish(
    client: FakeJsonClient,
    coordinator: HistoryRequestCoordinator,
    keys: tuple[str, ...],
    context: HistoryRequestContext,
) -> None:
    for key in keys:
        client.active_keys.discard(key)
        coordinator.finish_request(key, context)


def test_pending_history_requests_dispatch_snapshot_then_older_then_live() -> None:
    client = FakeJsonClient()
    coordinator, timer = _coordinator(client)
    context = HistoryRequestContext(
        generation=4,
        selected_thread_id="thread-a",
        next_cursor="older-page",
        closing=False,
    )

    coordinator.request_snapshot(4, context)
    first_group = ("selection:4:turns", "selection:4:items")
    assert tuple(request[0] for request in client.requests) == first_group

    coordinator.request_selected_history(context)
    coordinator.request_older(context)
    coordinator.request_snapshot(4, context)
    coordinator.request_selected_history(context)

    _finish(client, coordinator, first_group, context)
    snapshot_group = ("selection:4:turns", "selection:4:items")
    assert tuple(request[0] for request in client.requests[2:]) == snapshot_group

    _finish(client, coordinator, snapshot_group, context)
    older_request = client.requests[-1]
    assert older_request == (
        "selection:4:older:older-page",
        "/ui-api/threads/thread-a/items",
        {"limit": 100, "sort_direction": "desc", "cursor": "older-page"},
    )

    _finish(client, coordinator, (older_request[0],), context)
    assert timer.isActive()
    assert timer.isSingleShot()
    assert timer.interval() == 250

    timer.stop()
    coordinator.request_selected_history(context)
    live_group = ("selection:4:history:turns", "selection:4:history:items")
    assert tuple(request[0] for request in client.requests[-2:]) == live_group


def test_rejected_history_requests_do_not_leave_group_in_flight() -> None:
    client = FakeJsonClient(rejected_keys={"selection:8:turns", "selection:8:items"})
    coordinator, timer = _coordinator(client)
    context = HistoryRequestContext(
        generation=8,
        selected_thread_id="thread-b",
        next_cursor=None,
        closing=False,
    )

    coordinator.request_snapshot(8, context)

    assert len(client.requests) == 2
    assert not coordinator.in_flight
    assert client.active_keys == set()
    assert not timer.isActive()


def test_partial_history_request_rejection_still_waits_for_group_and_dispatches_older() -> None:
    client = FakeJsonClient(rejected_keys={"selection:9:turns"})
    coordinator, _ = _coordinator(client)
    context = HistoryRequestContext(9, "thread-c", "older-page", False)

    coordinator.request_snapshot(9, context)
    coordinator.request_older(context)

    assert coordinator.in_flight
    assert client.active_keys == {"selection:9:items"}
    assert len(client.requests) == 2

    _finish(client, coordinator, ("selection:9:items",), context)

    assert client.requests[-1] == (
        "selection:9:older:older-page",
        "/ui-api/threads/thread-c/items",
        {"limit": 100, "sort_direction": "desc", "cursor": "older-page"},
    )
    assert coordinator.in_flight


def test_selection_reset_discards_old_requests_and_generation() -> None:
    client = FakeJsonClient()
    coordinator, _ = _coordinator(client)
    first_context = HistoryRequestContext(1, "thread-a", None, False)
    second_context = HistoryRequestContext(2, "thread-b", None, False)

    coordinator.request_snapshot(1, first_context)
    coordinator.reset()
    coordinator.request_snapshot(1, second_context)

    assert len(client.requests) == 2
    assert not coordinator.in_flight

    coordinator.request_snapshot(2, second_context)
    assert tuple(request[0] for request in client.requests[2:]) == (
        "selection:2:turns",
        "selection:2:items",
    )
