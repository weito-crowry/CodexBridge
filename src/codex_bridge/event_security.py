from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import secrets
import socket
import ssl
import time
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from .logging_utils import log_event

MAX_WEBHOOK_BODY_BYTES = 262_144
_VERIFICATION_CACHE_SECONDS = 600
_VERIFICATION_BODY = {"type": "verification"}


class CallbackValidationError(ValueError):
    """Raised when a callback URL cannot be safely reached."""


class CallbackRequestError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class WebhookResponseTooLarge(ValueError):
    """Raised when a callback response exceeds the bounded response size."""


@dataclass(frozen=True, slots=True)
class CallbackUrl:
    url: str
    hostname: str
    port: int
    request_target: str
    literal_ip: str | None = None


@dataclass(frozen=True, slots=True)
class CallbackDestination:
    callback: CallbackUrl
    address: str


@dataclass(frozen=True, slots=True)
class WebhookResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class WebhookClient:
    async def validate(self, url: str, timeout_seconds: float = 10.0) -> CallbackDestination:
        callback = validate_callback_url(url)
        if callback.literal_ip is not None:
            addresses: tuple[str, ...] = (callback.literal_ip,)
        else:
            try:
                addresses = await asyncio.wait_for(
                    resolve_public_addresses(callback.hostname, callback.port),
                    timeout=timeout_seconds,
                )
            except CallbackValidationError:
                raise
            except TimeoutError as exc:
                raise CallbackRequestError("timeout") from exc
            except OSError as exc:
                raise CallbackRequestError("callback_unreachable") from exc
        if not addresses:
            raise CallbackValidationError("callback host has no public addresses")
        for address in addresses:
            if not _is_public_ip(address):
                raise CallbackValidationError("callback host resolved to a non-public address")
        return CallbackDestination(callback, addresses[0])

    async def post(
        self,
        destination: CallbackDestination,
        body: bytes,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> WebhookResponse:
        if len(body) > MAX_WEBHOOK_BODY_BYTES:
            raise ValueError("webhook body exceeds 256 KiB")
        try:
            return await asyncio.to_thread(
                self._post_sync, destination, body, headers, timeout_seconds
            )
        except TimeoutError as exc:
            raise CallbackRequestError("timeout") from exc
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            raise CallbackRequestError("callback_unreachable") from exc

    @staticmethod
    def _post_sync(
        destination: CallbackDestination,
        body: bytes,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> WebhookResponse:
        context = ssl.create_default_context()
        raw_socket = socket.create_connection(
            (destination.address, destination.callback.port), timeout=timeout_seconds
        )
        try:
            tls_socket = context.wrap_socket(
                raw_socket, server_hostname=destination.callback.hostname
            )
        except BaseException:
            raw_socket.close()
            raise
        connection = http.client.HTTPSConnection(
            destination.callback.hostname,
            destination.callback.port,
            timeout=timeout_seconds,
            context=context,
        )
        connection.sock = tls_socket
        try:
            connection.request(
                "POST",
                destination.callback.request_target,
                body=body,
                headers=dict(headers),
            )
            response = connection.getresponse()
            response_body = response.read(MAX_WEBHOOK_BODY_BYTES + 1)
            if len(response_body) > MAX_WEBHOOK_BODY_BYTES:
                raise WebhookResponseTooLarge("callback response exceeds 256 KiB")
            return WebhookResponse(
                response.status,
                {key.casefold(): value for key, value in response.getheaders()},
                response_body,
            )
        finally:
            connection.close()


async def resolve_public_addresses(hostname: str, port: int) -> tuple[str, ...]:
    loop = asyncio.get_running_loop()
    results = await loop.getaddrinfo(
        hostname,
        port,
        family=socket.AF_UNSPEC,
        type=socket.SOCK_STREAM,
        proto=socket.IPPROTO_TCP,
    )
    addresses = tuple(
        dict.fromkeys(
            sockaddr[0]
            for result in results
            if isinstance((sockaddr := result[4]), tuple) and isinstance(sockaddr[0], str)
        )
    )
    if not addresses:
        raise CallbackValidationError("callback host has no addresses")
    if any(not _is_public_ip(address) for address in addresses):
        raise CallbackValidationError("callback host resolved to a non-public address")
    return addresses


def validate_callback_url(url: str) -> CallbackUrl:
    if not isinstance(url, str) or not url or any(ord(char) < 0x20 for char in url):
        raise CallbackValidationError("callback URL is invalid")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        parsed_port = parsed.port
        port = 443 if parsed_port is None else parsed_port
    except ValueError as exc:
        raise CallbackValidationError("callback URL is invalid") from exc
    if (
        parsed.scheme.casefold() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not hostname
        or not 1 <= port <= 65_535
    ):
        raise CallbackValidationError("callback URL must be an HTTPS URL without user information")
    try:
        normalized_host = hostname.rstrip(".").encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise CallbackValidationError("callback hostname is invalid") from exc
    if not normalized_host:
        raise CallbackValidationError("callback hostname is invalid")
    try:
        literal = ipaddress.ip_address(normalized_host)
    except ValueError:
        literal_ip = None
    else:
        if not literal.is_global:
            raise CallbackValidationError("callback IP address is not public")
        literal_ip = str(literal)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    return CallbackUrl(url, normalized_host, port, path, literal_ip)


def _is_public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    return address.is_global


def decode_signing_secret(value: str) -> bytes:
    if not isinstance(value, str) or not value.startswith("whsec_"):
        raise ValueError("signing secret must use the whsec_ format")
    encoded = value[6:]
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("signing secret must contain valid base64") from exc
    if not 24 <= len(decoded) <= 64:
        raise ValueError("signing secret must decode to 24-64 bytes")
    return decoded


def standard_webhook_signature(secret: bytes, webhook_id: str, timestamp: str, body: bytes) -> str:
    signed = webhook_id.encode("utf-8") + b"." + timestamp.encode("ascii") + b"." + body
    signature = hmac.new(secret, signed, hashlib.sha256).digest()
    return "v1," + base64.b64encode(signature).decode("ascii")


def signed_headers(
    secrets_to_sign: tuple[bytes, ...],
    webhook_id: str,
    body: bytes,
    subscription_id: str,
    *,
    timestamp: int | None = None,
) -> dict[str, str]:
    if len(body) > MAX_WEBHOOK_BODY_BYTES:
        raise ValueError("webhook body exceeds 256 KiB")
    signed_at = str(int(time.time()) if timestamp is None else timestamp)
    signatures = " ".join(
        standard_webhook_signature(secret, webhook_id, signed_at, body)
        for secret in secrets_to_sign
    )
    return {
        "Content-Type": "application/json",
        "webhook-id": webhook_id,
        "webhook-timestamp": signed_at,
        "webhook-signature": signatures,
        "X-MCP-Subscription-Id": subscription_id,
    }


class CallbackVerificationCache:
    def __init__(self, ttl_seconds: float = _VERIFICATION_CACHE_SECONDS) -> None:
        self._ttl_seconds = ttl_seconds
        self._verified: dict[tuple[str, str, str], float] = {}

    def contains(self, principal: str, url: str, secret: bytes) -> bool:
        key = (principal, url, hashlib.sha256(secret).hexdigest())
        expiry = self._verified.get(key)
        if expiry is None:
            return False
        if expiry <= time.monotonic():
            del self._verified[key]
            return False
        return True

    def add(self, principal: str, url: str, secret: bytes) -> None:
        now = time.monotonic()
        for key, expiry in tuple(self._verified.items()):
            if expiry <= now:
                del self._verified[key]
        key = (principal, url, hashlib.sha256(secret).hexdigest())
        self._verified[key] = now + self._ttl_seconds


async def verify_callback(
    *,
    client: WebhookClient,
    cache: CallbackVerificationCache,
    principal: str,
    url: str,
    signing_secret: bytes,
    subscription_id: str,
    timeout_seconds: float,
) -> None:
    if cache.contains(principal, url, signing_secret):
        return
    try:
        destination = await client.validate(url, timeout_seconds)
    except CallbackValidationError as exc:
        log_event("mcp_events.callback_rejected", reason="invalid_callback")
        raise CallbackRequestError("invalid_callback") from exc
    challenge = secrets.token_urlsafe(32)
    payload = {**_VERIFICATION_BODY, "challenge": challenge}
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    verification_id = "msg_verification_" + secrets.token_urlsafe(18)
    headers = signed_headers((signing_secret,), verification_id, body, subscription_id)
    try:
        response = await client.post(destination, body, headers, timeout_seconds)
    except CallbackRequestError:
        raise
    except TimeoutError as exc:
        raise CallbackRequestError("timeout") from exc
    except Exception as exc:
        raise CallbackRequestError("callback_unreachable") from exc
    if not 200 <= response.status < 300:
        raise CallbackRequestError("challenge_failed")
    try:
        decoded = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CallbackRequestError("invalid_response") from exc
    if not isinstance(decoded, dict) or not isinstance(decoded.get("challenge"), str):
        raise CallbackRequestError("invalid_response")
    if not hmac.compare_digest(decoded["challenge"], challenge):
        raise CallbackRequestError("challenge_failed")
    cache.add(principal, url, signing_secret)


def principal_from_request_context(context: object) -> str | None:
    request = getattr(context, "request", None)
    scope = getattr(request, "scope", None)
    if not isinstance(scope, Mapping):
        return None
    user = scope.get("user")
    auth = scope.get("auth")
    if user is None or auth is None or getattr(user, "is_authenticated", False) is not True:
        return None
    principal = getattr(user, "display_name", None)
    if not isinstance(principal, str) or not principal.strip():
        principal = getattr(user, "username", None)
    if not isinstance(principal, str) or not principal.strip():
        return None
    return principal.strip()
