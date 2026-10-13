from __future__ import annotations

import base64
import hashlib
import hmac
import json
from types import SimpleNamespace

import pytest

from codex_bridge.event_security import (
    MAX_WEBHOOK_BODY_BYTES,
    CallbackRequestError,
    CallbackValidationError,
    CallbackVerificationCache,
    WebhookClient,
    WebhookResponse,
    decode_signing_secret,
    principal_from_request_context,
    signed_headers,
    standard_webhook_signature,
    validate_callback_url,
    verify_callback,
)


def _secret(seed: int = 0) -> tuple[str, bytes]:
    decoded = bytes((seed + index) % 256 for index in range(32))
    return "whsec_" + base64.b64encode(decoded).decode("ascii"), decoded


def test_signing_secret_requires_standard_prefix_base64_and_length() -> None:
    secret, decoded = _secret()

    assert decode_signing_secret(secret) == decoded
    for invalid in ("secret", "whsec_not-base64!", "whsec_" + base64.b64encode(b"short").decode()):
        with pytest.raises(ValueError):
            decode_signing_secret(invalid)


@pytest.mark.parametrize(
    "url",
    [
        "http://receiver.example/callback",
        "https://user:pass@receiver.example/callback",
        "https://127.0.0.1/callback",
        "https://10.2.3.4/callback",
        "https://receiver.example:0/callback",
        "https:///missing-host",
        "https://receiver.example/callback#fragment",
    ],
)
def test_callback_url_rejects_non_https_credentials_and_non_public_ips(url: str) -> None:
    with pytest.raises(CallbackValidationError):
        validate_callback_url(url)


def test_callback_url_preserves_path_query_and_hostname_for_tls() -> None:
    callback = validate_callback_url("https://receiver.example/events?key=value")

    assert callback.hostname == "receiver.example"
    assert callback.port == 443
    assert callback.request_target == "/events?key=value"


def test_standard_webhook_signature_uses_exact_utf8_body() -> None:
    secret = bytes(range(32))
    body = '{"status":"完了"}'.encode()
    expected = base64.b64encode(
        hmac.new(secret, b"evt_1.1700000000." + body, hashlib.sha256).digest()
    ).decode("ascii")

    assert standard_webhook_signature(secret, "evt_1", "1700000000", body) == "v1," + expected
    headers = signed_headers((secret,), "evt_1", body, "sub_1", timestamp=1_700_000_000)
    assert headers["webhook-signature"] == "v1," + expected
    assert headers["webhook-timestamp"] == "1700000000"
    assert headers["X-MCP-Subscription-Id"] == "sub_1"


def test_signed_headers_support_old_and_new_rotation_signatures() -> None:
    body = b"{}"
    headers = signed_headers((b"old secret", b"new secret"), "evt", body, "sub", timestamp=7)

    assert headers["webhook-signature"].count("v1,") == 2
    assert headers["webhook-signature"].split()[0] == standard_webhook_signature(
        b"old secret", "evt", "7", body
    )


def test_signed_headers_rejects_bodies_over_256_kib() -> None:
    with pytest.raises(ValueError, match="256 KiB"):
        signed_headers((bytes(range(32)),), "evt", b"x" * (MAX_WEBHOOK_BODY_BYTES + 1), "sub")


@pytest.mark.asyncio
async def test_webhook_client_pins_the_validated_dns_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def resolve(_host: str, _port: int) -> tuple[str, ...]:
        return ("1.1.1.1",)

    monkeypatch.setattr("codex_bridge.event_security.resolve_public_addresses", resolve)
    client = WebhookClient()

    destination = await client.validate("https://receiver.example/events")

    assert destination.address == "1.1.1.1"
    assert destination.callback.hostname == "receiver.example"


@pytest.mark.asyncio
async def test_webhook_client_rejects_mixed_public_and_private_dns_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def resolve(_host: str, _port: int) -> tuple[str, ...]:
        return ("1.1.1.1", "127.0.0.1")

    monkeypatch.setattr("codex_bridge.event_security.resolve_public_addresses", resolve)

    with pytest.raises(CallbackValidationError):
        await WebhookClient().validate("https://receiver.example/events")


class _ChallengeClient(WebhookClient):
    def __init__(self, *, status: int = 200, response: bytes | None = None) -> None:
        self.status = status
        self.response = response
        self.posts: list[tuple[bytes, dict[str, str]]] = []

    async def validate(self, _url: str, _timeout_seconds: float = 10.0):
        return SimpleNamespace(callback=None, address="1.1.1.1")

    async def post(self, _destination, body, headers, _timeout):
        self.posts.append((body, dict(headers)))
        response = self.response
        if response is None:
            challenge = json.loads(body)["challenge"]
            response = json.dumps({"challenge": challenge}).encode()
        return WebhookResponse(self.status, {}, response)


class _TimeoutChallengeClient(_ChallengeClient):
    async def post(self, _destination, _body, _headers, _timeout):
        raise TimeoutError


@pytest.mark.asyncio
async def test_callback_challenge_is_signed_and_cached_by_principal_url_and_secret() -> None:
    secret_text, secret = _secret()
    client = _ChallengeClient()
    cache = CallbackVerificationCache()
    values = {
        "client": client,
        "cache": cache,
        "principal": "local-owner",
        "url": "https://receiver.example/callback",
        "signing_secret": secret,
        "subscription_id": "sub_1",
        "timeout_seconds": 10,
    }

    await verify_callback(**values)
    await verify_callback(**values)

    assert len(client.posts) == 1
    body, headers = client.posts[0]
    challenge_body = json.loads(body)
    assert challenge_body["type"] == "verification"
    assert headers["webhook-id"].startswith("msg_verification_")
    assert headers["webhook-signature"].startswith("v1,")
    assert decode_signing_secret(secret_text) == secret


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "response", "reason"),
    [
        (200, b'{"challenge":"wrong"}', "challenge_failed"),
        (200, b"not-json", "invalid_response"),
        (503, b"{}", "challenge_failed"),
    ],
)
async def test_callback_challenge_rejects_bad_responses(
    status: int, response: bytes, reason: str
) -> None:
    _, secret = _secret()
    with pytest.raises(CallbackRequestError) as exc_info:
        await verify_callback(
            client=_ChallengeClient(status=status, response=response),
            cache=CallbackVerificationCache(),
            principal="owner",
            url="https://receiver.example/callback",
            signing_secret=secret,
            subscription_id="sub_1",
            timeout_seconds=1,
        )

    assert exc_info.value.reason == reason


@pytest.mark.asyncio
async def test_callback_challenge_timeout_has_an_explicit_reason() -> None:
    _, secret = _secret()

    with pytest.raises(CallbackRequestError) as exc_info:
        await verify_callback(
            client=_TimeoutChallengeClient(),
            cache=CallbackVerificationCache(),
            principal="owner",
            url="https://receiver.example/callback",
            signing_secret=secret,
            subscription_id="sub_1",
            timeout_seconds=1,
        )

    assert exc_info.value.reason == "timeout"


def test_principal_resolver_uses_authenticated_request_scope_only() -> None:
    anonymous = SimpleNamespace(request=SimpleNamespace(headers={"x-owner": "forged"}, scope={}))
    authenticated = SimpleNamespace(
        request=SimpleNamespace(
            scope={
                "auth": object(),
                "user": SimpleNamespace(is_authenticated=True, display_name="local-owner"),
            }
        )
    )

    assert principal_from_request_context(anonymous) is None
    assert principal_from_request_context(authenticated) == "local-owner"
