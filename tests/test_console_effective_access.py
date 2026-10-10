from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
)

from codex_bridge.console.widgets import ActivityPane
from tests.test_console_main_window import (
    FakeClient,
    _application,
    _set_usage_ready,
    _usage_window,
)


def test_main_window_uses_current_thread_metadata_not_setup_access_values() -> None:
    _application()
    client = FakeClient()
    window = _usage_window(client)

    window.select_thread("thread-a")
    status_a = f"selection:{window._selection_generation}:status"
    client.result(
        status_a,
        {
            "thread_id": "thread-a",
            "state": "in_progress",
            "thread_metadata": {
                "sandbox_mode": "danger-full-access",
                "approval_policy": "never",
                "approvals_reviewer": "user",
            },
        },
    )
    sandbox = window.activity_pane.findChild(QLabel, "effectiveSandboxMode")
    policy = window.activity_pane.findChild(QLabel, "effectiveApprovalPolicy")
    reviewer = window.activity_pane.findChild(QLabel, "effectiveApprovalsReviewer")
    assert sandbox is not None
    assert policy is not None
    assert reviewer is not None
    assert "Full Access" in sandbox.text()

    window.select_thread("thread-b")
    status_b = f"selection:{window._selection_generation}:status"
    assert sandbox.text() == "Sandbox: Unknown"
    assert policy.text() == "Approval policy: Unknown"
    assert reviewer.text() == "Approvals reviewer: Unknown"

    # A late response for the previous generation must not overwrite thread B.
    client.result(
        status_a,
        {
            "thread_id": "thread-a",
            "state": "in_progress",
            "thread_metadata": {
                "sandbox_mode": "danger-full-access",
                "approval_policy": "never",
                "approvals_reviewer": "user",
            },
        },
    )
    client.result(
        status_b,
        {
            "thread_id": "thread-b",
            "state": "in_progress",
            "thread_metadata": {
                "sandbox_mode": "workspace-write",
                "approval_policy": "on-request",
                "approvals_reviewer": "auto_review",
            },
            "setup_selection": {"sandbox_mode": "danger-full-access"},
            "sandbox_mode": "danger-full-access",
        },
    )

    assert sandbox.text() == "Sandbox: workspace-write"
    assert policy.text() == "Approval policy: on-request"
    assert reviewer.text() == "Approvals reviewer: auto_review"
    window.close()


def test_main_window_shows_unknown_access_while_selected_thread_status_is_loading() -> None:
    application = _application()
    client = FakeClient()
    window = _usage_window(client)
    _set_usage_ready(window)

    window.select_thread("thread-a")
    window.show()
    application.processEvents()

    sandbox = window.activity_pane.findChild(QLabel, "effectiveSandboxMode")
    policy = window.activity_pane.findChild(QLabel, "effectiveApprovalPolicy")
    reviewer = window.activity_pane.findChild(QLabel, "effectiveApprovalsReviewer")
    assert sandbox is not None
    assert policy is not None
    assert reviewer is not None
    assert sandbox.text() == "Sandbox: Unknown"
    assert policy.text() == "Approval policy: Unknown"
    assert reviewer.text() == "Approvals reviewer: Unknown"
    assert sandbox.isVisible()
    assert policy.isVisible()
    assert reviewer.isVisible()

    window.select_thread(None)
    application.processEvents()
    assert not sandbox.isVisible()
    assert not policy.isVisible()
    assert not reviewer.isVisible()
    window.close()


def test_main_window_clears_effective_access_on_status_error_and_disconnect() -> None:
    application = _application()
    client = FakeClient()
    window = _usage_window(client)
    window.show()
    application.processEvents()
    window.select_thread("thread-a")
    status_key = f"selection:{window._selection_generation}:status"
    client.result(
        status_key,
        {
            "thread_id": "thread-a",
            "state": "in_progress",
            "thread_metadata": {
                "sandbox_mode": "workspace-write",
                "approval_policy": "on-request",
                "approvals_reviewer": "user",
            },
            "recent_activities": [
                {"activity_id": "activity-1", "type": "turn_started", "summary": "started"}
            ],
        },
    )

    client.failure(status_key, "Status request failed")

    sandbox = window.activity_pane.findChild(QLabel, "effectiveSandboxMode")
    policy = window.activity_pane.findChild(QLabel, "effectiveApprovalPolicy")
    reviewer = window.activity_pane.findChild(QLabel, "effectiveApprovalsReviewer")
    assert sandbox is not None
    assert policy is not None
    assert reviewer is not None
    assert sandbox.text() == "Sandbox: Unavailable"
    assert policy.text() == "Approval policy: Unavailable"
    assert reviewer.text() == "Approvals reviewer: Unavailable"
    assert sandbox.isVisible()
    assert policy.isVisible()
    assert reviewer.isVisible()
    assert window.activity_pane.activity_list.count() == 1

    client.result(
        status_key,
        {
            "thread_id": "thread-a",
            "state": "in_progress",
            "thread_metadata": {
                "sandbox_mode": "danger-full-access",
                "approval_policy": "never",
                "approvals_reviewer": "user",
            },
        },
    )
    assert "Full Access" in sandbox.text()

    client.failure("bridge-status", "Bridge unavailable")

    assert sandbox.text() == "Sandbox: Unavailable"
    assert policy.text() == "Approval policy: Unavailable"
    assert reviewer.text() == "Approvals reviewer: Unavailable"
    assert sandbox.isVisible()
    assert policy.isVisible()
    assert reviewer.isVisible()
    assert window.activity_pane.activity_list.count() == 1
    window.close()


def test_activity_pane_hides_empty_state_but_shows_unloaded_access_and_recent_activities() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = ActivityPane()

    pane.set_empty_state("No thread selected.")
    assert not pane.state_label.isVisibleTo(pane)

    pane.set_snapshot(
        {
            "state": "not_loaded",
            "pending_request": None,
            "recent_activities": [
                {"activity_id": "activity-1", "type": "turn_completed", "summary": "done"}
            ],
        }
    )

    pane.show()
    application.processEvents()
    access_labels = (
        pane.findChild(QLabel, "effectiveSandboxMode"),
        pane.findChild(QLabel, "effectiveApprovalPolicy"),
        pane.findChild(QLabel, "effectiveApprovalsReviewer"),
    )
    assert all(label is not None for label in access_labels)
    assert tuple(label.text() for label in access_labels if label is not None) == (
        "Sandbox: Unknown",
        "Approval policy: Unknown",
        "Approvals reviewer: Unknown",
    )
    assert all(label.isVisible() for label in access_labels if label is not None)
    recent_header = next(
        label for label in pane.findChildren(QLabel) if label.text() == "Recent activities"
    )
    assert recent_header.isVisibleTo(pane)
    assert pane.activity_list.count() == 1
    pane.close()


def test_activity_pane_renders_effective_thread_access_from_metadata_only() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = ActivityPane()

    pane.set_snapshot(
        {
            "state": "in_progress",
            "thread_metadata": {
                "sandbox_mode": "workspace-write",
                "approval_policy": "on-request",
                "approvals_reviewer": "user",
            },
            "sandbox_mode": "danger-full-access",
            "setup": {"sandbox_mode": "danger-full-access"},
        }
    )

    sandbox = pane.findChild(QLabel, "effectiveSandboxMode")
    policy = pane.findChild(QLabel, "effectiveApprovalPolicy")
    reviewer = pane.findChild(QLabel, "effectiveApprovalsReviewer")
    assert sandbox is not None
    assert policy is not None
    assert reviewer is not None
    assert sandbox.text() == "Sandbox: workspace-write"
    assert policy.text() == "Approval policy: on-request"
    assert reviewer.text() == "Approvals reviewer: user"
    assert all(
        label.textFormat() == Qt.TextFormat.PlainText for label in (sandbox, policy, reviewer)
    )


def test_activity_pane_marks_full_access_with_warning_text_and_palette_emphasis() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = ActivityPane()
    pane.set_snapshot(
        {
            "state": "in_progress",
            "thread_metadata": {
                "sandbox_mode": "danger-full-access",
                "approval_policy": "never",
                "approvals_reviewer": "auto_review",
            },
        }
    )

    sandbox = pane.findChild(QLabel, "effectiveSandboxMode")
    assert sandbox is not None
    assert "WARNING" in sandbox.text()
    assert "Full Access" in sandbox.text()
    assert "danger-full-access" in sandbox.text()
    assert sandbox.font().bold()
    assert sandbox.palette().color(QPalette.ColorRole.WindowText) == sandbox.palette().color(
        QPalette.ColorRole.BrightText
    )


@pytest.mark.parametrize(
    ("snapshot", "expected"),
    [
        (
            {"state": "in_progress"},
            ("Sandbox: Unknown", "Approval policy: Unknown", "Approvals reviewer: Unknown"),
        ),
        (
            {"state": "in_progress", "thread_metadata": None},
            ("Sandbox: Unknown", "Approval policy: Unknown", "Approvals reviewer: Unknown"),
        ),
        (
            {"state": "in_progress", "thread_metadata": "sandbox_mode=danger-full-access"},
            ("Sandbox: Unknown", "Approval policy: Unknown", "Approvals reviewer: Unknown"),
        ),
        (
            {"state": "in_progress", "thread_metadata": {"sandbox_mode": "workspace-write"}},
            ("Sandbox: workspace-write", "Approval policy: Unknown", "Approvals reviewer: Unknown"),
        ),
        (
            {
                "state": "in_progress",
                "thread_metadata": {
                    "sandbox_mode": None,
                    "approval_policy": None,
                    "approvals_reviewer": None,
                },
            },
            ("Sandbox: Unknown", "Approval policy: Unknown", "Approvals reviewer: Unknown"),
        ),
        (
            {
                "state": "in_progress",
                "thread_metadata": {
                    "sandbox_mode": "",
                    "approval_policy": " ",
                    "approvals_reviewer": "",
                },
            },
            ("Sandbox: Unknown", "Approval policy: Unknown", "Approvals reviewer: Unknown"),
        ),
        (
            {
                "state": "in_progress",
                "thread_metadata": {
                    "sandbox_mode": 42,
                    "approval_policy": ["on-request"],
                    "approvals_reviewer": {"name": "user"},
                },
            },
            ("Sandbox: Unknown", "Approval policy: Unknown", "Approvals reviewer: Unknown"),
        ),
        (
            {
                "state": "in_progress",
                "thread_metadata": {
                    "sandbox_mode": "<b>danger-full-access</b>",
                    "approval_policy": "unknown-policy",
                    "approvals_reviewer": "unknown-reviewer",
                },
            },
            ("Sandbox: Unknown", "Approval policy: Unknown", "Approvals reviewer: Unknown"),
        ),
        (
            {
                "state": "not_loaded",
                "thread_metadata": {
                    "sandbox_mode": "danger-full-access",
                    "approval_policy": "on-request",
                    "approvals_reviewer": "user",
                },
            },
            ("Sandbox: Unknown", "Approval policy: Unknown", "Approvals reviewer: Unknown"),
        ),
    ],
)
def test_activity_pane_shows_unknown_for_missing_or_invalid_thread_access_metadata(
    snapshot: dict[str, object], expected: tuple[str, str, str]
) -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = ActivityPane()

    pane.set_snapshot(snapshot)

    assert (
        pane.findChild(QLabel, "effectiveSandboxMode").text(),
        pane.findChild(QLabel, "effectiveApprovalPolicy").text(),
        pane.findChild(QLabel, "effectiveApprovalsReviewer").text(),
    ) == expected


def test_activity_pane_clears_effective_access_on_reset_and_error() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = ActivityPane()
    access = {
        "state": "in_progress",
        "thread_metadata": {
            "sandbox_mode": "workspace-write",
            "approval_policy": "on-request",
            "approvals_reviewer": "user",
        },
    }

    pane.set_snapshot(access)
    pane.set_snapshot(
        {
            "state": "in_progress",
            "thread_metadata": {"sandbox_mode": "read-only"},
        }
    )
    assert pane.findChild(QLabel, "effectiveSandboxMode").text() == "Sandbox: read-only"
    assert pane.findChild(QLabel, "effectiveApprovalPolicy").text() == "Approval policy: Unknown"
    assert (
        pane.findChild(QLabel, "effectiveApprovalsReviewer").text() == "Approvals reviewer: Unknown"
    )

    pane.set_snapshot(access)
    pane.set_empty_state("Loading activity…")
    assert pane.findChild(QLabel, "effectiveSandboxMode").text() == "Sandbox: Unknown"
    assert pane.findChild(QLabel, "effectiveApprovalPolicy").text() == "Approval policy: Unknown"
    assert (
        pane.findChild(QLabel, "effectiveApprovalsReviewer").text() == "Approvals reviewer: Unknown"
    )

    pane.set_snapshot(access)
    pane.set_error("Status unavailable")
    assert pane.findChild(QLabel, "effectiveSandboxMode").text() == "Sandbox: Unavailable"
    assert (
        pane.findChild(QLabel, "effectiveApprovalPolicy").text() == "Approval policy: Unavailable"
    )
    assert (
        pane.findChild(QLabel, "effectiveApprovalsReviewer").text()
        == "Approvals reviewer: Unavailable"
    )


def test_activity_pane_reviews_command_file_and_permission_approvals() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = ActivityPane()
    requested: list[tuple[object, str]] = []
    pane.approval_decision_requested.connect(
        lambda request_id, decision: requested.append((request_id, decision))
    )
    pane.set_control_available(True)
    pane.show()
    application.processEvents()

    pane.set_snapshot(
        {
            "state": "needs_approval",
            "thread_metadata": {
                "sandbox_mode": "workspace-write",
                "approval_policy": "on-request",
                "approvals_reviewer": "user",
            },
            "current_diff": "diff --git a/a.txt b/a.txt",
            "pending_request": {
                "request_id": "command-1",
                "method": "item/commandExecution/requestApproval",
                "thread_id": "thread-1",
                "turn_id": "turn-1",
                "summary": "pytest tests/test_example.py",
            },
        }
    )
    assert pane.findChild(QLabel, "effectiveSandboxMode").text() == "Sandbox: workspace-write"
    assert pane.findChild(QLabel, "effectiveApprovalPolicy").text() == "Approval policy: on-request"
    assert pane.findChild(QLabel, "effectiveApprovalsReviewer").text() == "Approvals reviewer: user"
    command_details = pane.approval_details_label.text()
    assert "Command approval" in command_details
    assert "pytest tests/test_example.py" in command_details
    assert "Thread: thread-1" in command_details
    assert "Turn: turn-1" in command_details
    assert pane.approval_details_label.isVisible()
    assert pane.approval_buttons_widget.isVisible()

    pane.allow_once_button.click()
    pane.allow_for_session_button.click()
    pane.decline_button.click()
    pane.cancel_button.click()
    assert requested == [
        ("command-1", "accept"),
        ("command-1", "acceptForSession"),
        ("command-1", "decline"),
        ("command-1", "cancel"),
    ]

    pane.set_snapshot(
        {
            "state": "needs_approval",
            "current_diff": "diff --git a/a.txt b/a.txt",
            "pending_request": {
                "request_id": 2,
                "method": "item/fileChange/requestApproval",
                "thread_id": "thread-1",
                "turn_id": "turn-2",
                "summary": "Update a.txt",
            },
        }
    )
    assert "File change approval" in pane.approval_details_label.text()
    assert "diff --git a/a.txt b/a.txt" in pane.approval_details_label.text()

    pane.set_snapshot(
        {
            "state": "needs_approval",
            "pending_request": {
                "request_id": 3,
                "method": "item/permissions/requestApproval",
                "thread_id": "thread-1",
                "turn_id": "turn-3",
                "summary": "Need local access",
                "permission": {
                    "reason": "Need local access",
                    "cwd": "C:/repo",
                    "requested_permissions": {
                        "fileSystem": {"entries": [{"access": "read", "path": "C:/repo"}]},
                        "network": {"enabled": True},
                    },
                    "allowed_scopes": ["turn", "session"],
                },
            },
        }
    )
    permission_details = pane.approval_details_label.text()
    assert "Permission approval" in permission_details
    assert "Reason: Need local access" in permission_details
    assert "C:/repo" in permission_details
    assert "Requested filesystem permissions" in permission_details
    assert "Requested network permissions" in permission_details
    assert "Allowed scopes: turn, session" in permission_details
    pane.close()
