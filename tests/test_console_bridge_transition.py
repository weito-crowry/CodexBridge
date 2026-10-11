from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from codex_bridge.console.bridge_transition import (
    BridgeTransitionContext,
    BridgeTransitionCoordinator,
)


@dataclass
class MutableContext:
    closing: bool = False
    control_token: str | None = "control-token"
    stop_confirmation_active: bool = False

    def snapshot(self) -> BridgeTransitionContext:
        return BridgeTransitionContext(
            closing=self.closing,
            control_token=self.control_token,
            stop_confirmation_active=self.stop_confirmation_active,
        )


class FakeTunnel:
    def __init__(self) -> None:
        self.callbacks: list[Callable[[], None]] = []
        self.stop_result = True
        self.call_callback_synchronously = False

    def stop(self, on_finished: Callable[[], None]) -> bool:
        self.callbacks.append(on_finished)
        if self.call_callback_synchronously:
            on_finished()
        return self.stop_result

    def finish(self, index: int = 0) -> None:
        self.callbacks[index]()


class FakeClient:
    def __init__(self) -> None:
        self.requests: list[str] = []
        self.accept = True

    def post_shutdown(self, token: str) -> bool:
        self.requests.append(token)
        return self.accept


def make_coordinator() -> tuple[
    BridgeTransitionCoordinator,
    MutableContext,
    FakeTunnel,
    FakeClient,
    list[str],
    list[str],
]:
    context = MutableContext()
    tunnel = FakeTunnel()
    client = FakeClient()
    transitions: list[str] = []
    failures: list[str] = []
    holder: dict[str, BridgeTransitionCoordinator] = {}

    def on_transition_started(action: str) -> None:
        coordinator = holder["coordinator"]
        assert coordinator.pending_action == action
        transitions.append(action)

    coordinator = BridgeTransitionCoordinator(
        get_context=context.snapshot,
        stop_tunnel=tunnel.stop,
        post_shutdown=client.post_shutdown,
        on_transition_started=on_transition_started,
        on_request_failure=lambda: failures.append("failed"),
    )
    holder["coordinator"] = coordinator
    return coordinator, context, tunnel, client, transitions, failures


def test_begin_stop_sets_action_before_callback_and_posts_without_tunnel() -> None:
    coordinator, _, _, client, transitions, _ = make_coordinator()

    coordinator.begin_transition("stop", tunnel_running=False)

    assert transitions == ["stop"]
    assert coordinator.pending_action == "stop"
    assert client.requests == ["control-token"]
    assert coordinator.control_request_pending


def test_restart_waits_for_tunnel_stop_then_posts_shutdown_once() -> None:
    coordinator, _, tunnel, client, transitions, _ = make_coordinator()

    coordinator.begin_transition("restart", tunnel_running=True)

    assert transitions == ["restart"]
    assert coordinator.pending_action == "restart"
    assert len(tunnel.callbacks) == 1
    assert client.requests == []

    tunnel.finish()
    tunnel.finish()

    assert client.requests == ["control-token"]
    assert coordinator.control_request_pending


def test_duplicate_shutdown_request_is_ignored_while_pending() -> None:
    coordinator, _, tunnel, client, _, _ = make_coordinator()
    coordinator.begin_transition("restart", tunnel_running=True)

    tunnel.finish()
    coordinator.request_shutdown()
    tunnel.finish()

    assert client.requests == ["control-token"]


def test_tunnel_stop_false_falls_back_to_shutdown_request() -> None:
    coordinator, _, tunnel, client, _, _ = make_coordinator()
    tunnel.stop_result = False

    coordinator.begin_transition("stop", tunnel_running=True)

    assert client.requests == ["control-token"]
    assert coordinator.control_request_pending


def test_synchronous_tunnel_callback_and_false_return_do_not_post_twice() -> None:
    coordinator, _, tunnel, client, _, failures = make_coordinator()
    tunnel.stop_result = False
    tunnel.call_callback_synchronously = True
    client.accept = False

    coordinator.begin_transition("restart", tunnel_running=True)

    assert client.requests == ["control-token"]
    assert failures == ["failed"]
    assert not coordinator.control_request_pending


def test_closing_rejects_shutdown_unless_explicitly_allowed() -> None:
    coordinator, context, _, client, _, _ = make_coordinator()
    context.closing = True

    coordinator.request_shutdown()
    assert client.requests == []

    coordinator.request_shutdown(allow_closing=True)
    assert client.requests == ["control-token"]


def test_shutdown_request_is_ignored_during_stop_confirmation() -> None:
    coordinator, context, _, client, _, _ = make_coordinator()
    context.stop_confirmation_active = True

    coordinator.request_shutdown()

    assert client.requests == []
    assert not coordinator.control_request_pending


def test_missing_token_and_rejected_post_report_failure_and_clear_pending() -> None:
    coordinator, context, _, client, _, failures = make_coordinator()
    context.control_token = None

    coordinator.request_shutdown()
    assert failures == ["failed"]
    assert client.requests == []
    assert not coordinator.control_request_pending

    context.control_token = "control-token"
    client.accept = False
    coordinator.request_shutdown()
    assert failures == ["failed", "failed"]
    assert client.requests == ["control-token"]
    assert not coordinator.control_request_pending


def test_shutdown_success_acknowledgement_is_one_shot() -> None:
    coordinator, _, _, _, _, _ = make_coordinator()
    coordinator.request_shutdown()

    assert coordinator.acknowledge_shutdown_success()
    assert not coordinator.control_request_pending
    assert not coordinator.acknowledge_shutdown_success()


def test_mark_shutdown_failed_clears_pending_request() -> None:
    coordinator, _, _, _, _, _ = make_coordinator()
    coordinator.request_shutdown()

    coordinator.mark_shutdown_failed()

    assert not coordinator.control_request_pending


def test_pending_and_restart_flags_can_be_reset_cleared_and_consumed() -> None:
    coordinator, _, _, _, _, _ = make_coordinator()
    coordinator.begin_transition("restart", tunnel_running=True)
    assert coordinator.pending_action == "restart"
    assert coordinator.restart_tunnel_was_running

    coordinator.clear_pending_action()
    assert coordinator.pending_action is None
    assert coordinator.consume_restart_tunnel_flag()
    assert not coordinator.restart_tunnel_was_running
    assert not coordinator.consume_restart_tunnel_flag()

    coordinator.begin_transition("stop", tunnel_running=False)
    coordinator.clear_restart_flag()
    assert not coordinator.restart_tunnel_was_running
    coordinator.reset_for_launch()
    assert coordinator.pending_action is None
    assert not coordinator.restart_tunnel_was_running
