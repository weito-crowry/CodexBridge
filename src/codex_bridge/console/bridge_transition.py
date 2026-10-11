from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class BridgeTransitionContext:
    closing: bool
    control_token: str | None
    stop_confirmation_active: bool


class BridgeTransitionCoordinator:
    def __init__(
        self,
        *,
        get_context: Callable[[], BridgeTransitionContext],
        stop_tunnel: Callable[[Callable[[], None]], bool],
        post_shutdown: Callable[[str], bool],
        on_transition_started: Callable[[str], None],
        on_request_failure: Callable[[], None],
    ) -> None:
        self._get_context = get_context
        self._stop_tunnel = stop_tunnel
        self._post_shutdown = post_shutdown
        self._on_transition_started = on_transition_started
        self._on_request_failure = on_request_failure
        self._pending_action: Literal["stop", "restart"] | None = None
        self._restart_tunnel_was_running = False
        self._control_request_pending = False

    @property
    def pending_action(self) -> Literal["stop", "restart"] | None:
        return self._pending_action

    @property
    def restart_tunnel_was_running(self) -> bool:
        return self._restart_tunnel_was_running

    @property
    def control_request_pending(self) -> bool:
        return self._control_request_pending

    def begin_transition(
        self,
        action: Literal["stop", "restart"],
        *,
        tunnel_running: bool,
    ) -> None:
        self._pending_action = action
        self._restart_tunnel_was_running = tunnel_running
        self._on_transition_started(action)
        if tunnel_running:
            callback_called = False

            def on_tunnel_stopped() -> None:
                nonlocal callback_called
                callback_called = True
                self.on_tunnel_stopped()

            started = self._stop_tunnel(on_tunnel_stopped)
            if not started and not self._control_request_pending and not callback_called:
                self.request_shutdown()
        else:
            self.request_shutdown()

    def on_tunnel_stopped(self) -> None:
        self.request_shutdown()

    def request_shutdown(self, *, allow_closing: bool = False) -> None:
        context = self._get_context()
        if (
            (context.closing and not allow_closing)
            or self._control_request_pending
            or context.stop_confirmation_active
        ):
            return
        token = context.control_token
        if token is None:
            self._on_request_failure()
            return
        self._control_request_pending = True
        if not self._post_shutdown(token):
            self._control_request_pending = False
            self._on_request_failure()

    def acknowledge_shutdown_success(self) -> bool:
        if not self._control_request_pending:
            return False
        self._control_request_pending = False
        return True

    def mark_shutdown_failed(self) -> None:
        self._control_request_pending = False

    def clear_pending_action(self) -> None:
        self._pending_action = None

    def reset_for_launch(self) -> None:
        self._pending_action = None
        self._restart_tunnel_was_running = False

    def clear_restart_flag(self) -> None:
        self._restart_tunnel_was_running = False

    def consume_restart_tunnel_flag(self) -> bool:
        was_running = self._restart_tunnel_was_running
        self._restart_tunnel_was_running = False
        return was_running
