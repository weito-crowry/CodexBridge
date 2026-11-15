from __future__ import annotations

from codex_bridge.console import main_window
from codex_bridge.console.main_window import _runtime_thread_status_observation


def test_runtime_thread_status_observation_preserves_supported_states() -> None:
    cases = (
        ({"runtime_status": {"type": "notLoaded"}}, (True, None)),
        ({"runtime_status": {"type": "idle"}}, (True, None)),
        ({"runtime_status": {"type": "systemError"}}, (True, None)),
        ({"runtime_status": {"type": "active", "active_flags": []}}, (True, "in_progress")),
        (
            {"runtime_status": {"type": "active", "active_flags": ["waitingOnApproval"]}},
            (True, "needs_approval"),
        ),
        (
            {"runtime_status": {"type": "active", "active_flags": ["waitingOnUserInput"]}},
            (True, "needs_input"),
        ),
        (
            {"runtime_status": {"type": "active", "active_flags": ["other"]}},
            (True, "waiting"),
        ),
    )
    for thread, expected in cases:
        assert _runtime_thread_status_observation(thread) == expected


def test_runtime_thread_status_observation_ignores_malformed_values() -> None:
    malformed = (
        {},
        {"runtime_status": None},
        {"runtime_status": {"type": 3}},
        {"runtime_status": {"type": "unrecognized"}},
        {"runtime_status": {"type": "active"}},
        {"runtime_status": {"type": "active", "active_flags": "waitingOnApproval"}},
        {"runtime_status": {"type": "active", "active_flags": [None]}},
    )
    for thread in malformed:
        assert _runtime_thread_status_observation(thread) == (False, None)


def test_activity_classifier_is_available_from_main_window_compatibility_surface() -> None:
    classifier = getattr(main_window, "classify_activity_thread_status", None)
    assert callable(classifier)


def test_activity_classifier_preserves_status_transitions() -> None:
    classifier = getattr(main_window, "classify_activity_thread_status", None)
    assert callable(classifier)
    cases = (
        (
            {"thread_id": "t", "type": "approval_requested", "status": "requested"},
            ("t", "needs_approval"),
        ),
        (
            {"thread_id": "t", "type": "user_input_requested", "status": "requested"},
            ("t", "needs_input"),
        ),
        ({"thread_id": "t", "type": "turn_started", "status": "in_progress"}, ("t", "in_progress")),
        ({"thread_id": "t", "type": "turn_started", "status": "needs_input"}, ("t", "needs_input")),
        ({"thread_id": "t", "type": "turn_completed", "status": "completed"}, ("t", "completed")),
        ({"thread_id": "t", "type": "turn_failed", "status": "failed"}, ("t", "failed")),
        (
            {"thread_id": "t", "type": "turn_interrupted", "status": "interrupted"},
            ("t", "interrupted"),
        ),
        ({"thread_id": "t", "type": "error", "status": "error"}, ("t", "error")),
    )
    for payload, expected in cases:
        assert classifier(payload) == expected


def test_activity_classifier_ignores_malformed_and_unrelated_activity() -> None:
    classifier = getattr(main_window, "classify_activity_thread_status", None)
    assert callable(classifier)
    ignored = (
        None,
        [],
        {"type": "turn_started", "status": "in_progress"},
        {"thread_id": 4, "type": "turn_started", "status": "in_progress"},
        {"thread_id": "t", "type": 4, "status": "in_progress"},
        {"thread_id": "t", "type": "approval_requested", "status": "resolved"},
        {"thread_id": "t", "type": "turn_started", "status": "completed"},
        {"thread_id": "t", "type": "other", "status": "completed"},
        {"thread_id": "t", "type": "error", "status": "in_progress"},
    )
    for payload in ignored:
        assert classifier(payload) is None
