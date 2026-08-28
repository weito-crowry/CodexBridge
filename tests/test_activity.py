from __future__ import annotations

from codex_bridge.activity import ActivityStore


def test_activity_store_is_bounded_per_thread_and_drops_oldest() -> None:
    store = ActivityStore()

    for index in range(501):
        store.add(
            thread_id="thread-a",
            turn_id="turn-a",
            type="error",
            status="failed",
            summary=f"error-{index}",
        )

    recent = store.get_recent("thread-a", "turn-a", limit=500)

    assert len(recent) == 500
    assert recent[0].summary == "error-1"
    assert recent[-1].summary == "error-500"


def test_activity_store_separates_threads_and_turns() -> None:
    store = ActivityStore()
    store.add(thread_id="thread-a", turn_id="turn-a", type="turn_started", status="in_progress")
    store.add(thread_id="thread-a", turn_id="turn-b", type="turn_started", status="in_progress")
    store.add(thread_id="thread-b", turn_id="turn-a", type="turn_started", status="in_progress")

    assert [item.turn_id for item in store.get_recent("thread-a", "turn-a")] == ["turn-a"]
    assert [item.thread_id for item in store.get_recent("thread-a")] == [
        "thread-a",
        "thread-a",
    ]
    assert store.latest("thread-a", "turn-a").turn_id == "turn-a"
    assert store.latest_known_turn("thread-a") == "turn-b"
    assert store.latest_known_turn("thread-b") == "turn-a"


def test_activity_store_returns_safe_bounded_activity_dict() -> None:
    store = ActivityStore()
    store.add(
        thread_id="thread",
        turn_id="turn",
        type="command_completed",
        status="completed",
        summary="pytest tests/test_activity.py",
        details={"exit_code": 0},
    )

    public = store.latest("thread", "turn").to_dict()

    assert set(public) == {
        "activity_id",
        "timestamp",
        "thread_id",
        "turn_id",
        "item_id",
        "type",
        "status",
        "summary",
        "details",
    }
    assert public["details"] == {"exit_code": 0}


def test_activity_store_redacts_secret_like_summary_values() -> None:
    store = ActivityStore()
    store.add(
        thread_id="thread",
        turn_id="turn",
        type="error",
        status="failed",
        summary="request token: fake-token",
        details={"message": "api_key=fake-key", "reasoning": "do not retain"},
    )

    public = store.latest("thread", "turn").to_dict()

    assert "fake-token" not in str(public)
    assert "fake-key" not in str(public)
    assert "reasoning" not in public["details"]


def test_activity_store_drops_sensitive_detail_key_variants() -> None:
    store = ActivityStore()
    store.add(
        thread_id="thread",
        turn_id="turn",
        type="error",
        status="failed",
        details={
            "password": "fake-password-value",
            "credential": "fake-credential-value",
            "api_key": "fake-api-key-value",
            "apiKey": "fake-camel-api-key-value",
            "authorization": "fake-auth-value",
            "exit_code": 0,
        },
    )

    public = store.latest("thread", "turn").to_dict()

    assert public["details"] == {"exit_code": 0}
    assert all(
        value not in str(public)
        for value in (
            "fake-password-value",
            "fake-credential-value",
            "fake-api-key-value",
            "fake-camel-api-key-value",
            "fake-auth-value",
        )
    )
