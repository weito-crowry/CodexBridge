from __future__ import annotations

import asyncio
import email.utils
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta

from .event_security import (
    MAX_WEBHOOK_BODY_BYTES,
    CallbackRequestError,
    CallbackValidationError,
    WebhookClient,
    decode_signing_secret,
    signed_headers,
)
from .event_store import EventStore, OutboxDelivery, utc_now
from .logging_utils import log_event

_RETRY_BACKOFF_SECONDS = (5.0, 15.0, 45.0)
_MAX_RETRY_AFTER_SECONDS = 3600.0
_IDLE_POLL_SECONDS = 30.0


class EventDeliveryWorker:
    def __init__(
        self,
        store: EventStore,
        *,
        timeout_seconds: float = 10.0,
        max_retries: int = 3,
        client: WebhookClient | None = None,
        clock: Callable[[], datetime] = utc_now,
        unix_time: Callable[[], int] = lambda: int(time.time()),
    ) -> None:
        self.store = store
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.client = client or WebhookClient()
        self._clock = clock
        self._unix_time = unix_time
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping = False
        self.store.reset_inflight()
        self.store.cleanup(self._clock())
        self._task = asyncio.create_task(self._run(), name="codexbridge-mcp-events-delivery")

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        self._stopping = True
        self._wake.set()
        await task
        self._task = None

    def notify(self) -> None:
        self._wake.set()

    async def process_next(self) -> bool:
        delivery = self.store.claim_next_due(self._clock())
        if delivery is None:
            return False
        if not self.store.subscription_is_active(delivery.subscription_id, now=self._clock()):
            self.store.mark_cancelled(delivery.event_id)
            return True
        await self._deliver(delivery)
        return True

    async def _deliver(self, delivery: OutboxDelivery) -> None:
        try:
            body = delivery.payload_json.encode("utf-8")
            if len(body) > MAX_WEBHOOK_BODY_BYTES:
                self.store.mark_failed(delivery.event_id)
                log_event("mcp_events.delivery_failed", reason="payload_too_large")
                return
            secret = decode_signing_secret(delivery.signing_secret)
            secrets_to_sign = [secret]
            if (
                delivery.previous_signing_secret is not None
                and delivery.previous_secret_expires_at is not None
                and _parse_utc(delivery.previous_secret_expires_at) > self._clock()
            ):
                try:
                    secrets_to_sign.append(decode_signing_secret(delivery.previous_signing_secret))
                except ValueError:
                    log_event("mcp_events.rotation_secret_ignored")
            destination = await self.client.validate(delivery.callback_url, self.timeout_seconds)
        except CallbackValidationError:
            self.store.mark_failed(delivery.event_id)
            log_event("mcp_events.delivery_failed", reason="invalid_callback")
            return
        except CallbackRequestError as exc:
            self._retry_or_fail(delivery, exc.reason)
            return
        except ValueError:
            self.store.mark_failed(delivery.event_id)
            log_event("mcp_events.delivery_failed", reason="invalid_subscription")
            return
        except Exception as exc:
            self._retry_or_fail(delivery, _failure_reason(exc))
            return

        if not self.store.subscription_is_active(delivery.subscription_id, now=self._clock()):
            self.store.mark_cancelled(delivery.event_id)
            return
        headers = signed_headers(
            tuple(secrets_to_sign),
            delivery.event_id,
            body,
            delivery.subscription_id,
            timestamp=self._unix_time(),
        )
        try:
            response = await self.client.post(destination, body, headers, self.timeout_seconds)
        except CallbackRequestError as exc:
            self._retry_or_fail(delivery, exc.reason)
            return
        except TimeoutError:
            self._retry_or_fail(delivery, "timeout")
            return
        except Exception as exc:
            self._retry_or_fail(delivery, _failure_reason(exc))
            return

        if 200 <= response.status < 300:
            self.store.mark_delivered(delivery.event_id, self._clock())
            log_event("mcp_events.delivered", event_id=delivery.event_id)
            return
        if 300 <= response.status < 500 and response.status != 429:
            self.store.mark_failed(delivery.event_id)
            log_event(
                "mcp_events.delivery_failed",
                event_id=delivery.event_id,
                http_status=response.status,
            )
            return
        retry_after = _retry_after_seconds(response.headers, self._clock())
        self._retry_or_fail(
            delivery,
            f"http_{response.status}",
            retry_after=retry_after if response.status == 429 else None,
        )

    def _retry_or_fail(
        self,
        delivery: OutboxDelivery,
        reason: str,
        *,
        retry_after: float | None = None,
    ) -> None:
        if delivery.attempt_count > self.max_retries:
            self.store.mark_failed(delivery.event_id)
            log_event("mcp_events.delivery_failed", event_id=delivery.event_id, reason=reason)
            return
        backoff_index = min(delivery.attempt_count - 1, len(_RETRY_BACKOFF_SECONDS) - 1)
        delay = retry_after if retry_after is not None else _RETRY_BACKOFF_SECONDS[backoff_index]
        delay = max(0.0, min(delay, _MAX_RETRY_AFTER_SECONDS))
        self.store.schedule_retry(
            delivery.event_id,
            self._clock() + timedelta(seconds=delay),
        )
        log_event(
            "mcp_events.delivery_retry_scheduled",
            event_id=delivery.event_id,
            reason=reason,
            attempt_count=delivery.attempt_count,
            delay_seconds=delay,
        )

    async def _run(self) -> None:
        last_cleanup = 0.0
        while not self._stopping:
            self._wake.clear()
            try:
                if time.monotonic() - last_cleanup >= 60:
                    self.store.cleanup(self._clock())
                    last_cleanup = time.monotonic()
                if await self.process_next():
                    continue
                delay = self.store.next_attempt_delay(
                    self._clock(), maximum_seconds=_IDLE_POLL_SECONDS
                )
            except Exception as exc:
                log_event("mcp_events.worker_error", error_type=exc.__class__.__name__)
                delay = 1.0
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=max(0.05, delay))
            except TimeoutError:
                pass


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _retry_after_seconds(headers: Mapping[str, str], now: datetime) -> float | None:
    value = next(
        (item for key, item in headers.items() if key.casefold() == "retry-after"),
        None,
    )
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            retry_at = email.utils.parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            seconds = (retry_at.astimezone(UTC) - now.astimezone(UTC)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    if seconds < 0:
        return 0.0
    return min(seconds, _MAX_RETRY_AFTER_SECONDS)


def _failure_reason(error: BaseException) -> str:
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, OSError):
        return "callback_unreachable"
    return "delivery_error"
