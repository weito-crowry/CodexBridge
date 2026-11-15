"""Authentication for explicitly enabled LAN access to the existing MCP transport."""

from __future__ import annotations

import hmac
import ipaddress

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class LanMcpAuthMiddleware:
    def __init__(self, app: ASGIApp, *, token: str) -> None:
        self.app = app
        self._token = token.encode("ascii")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        peer = scope.get("client")
        try:
            local = peer is not None and ipaddress.ip_address(peer[0]).is_loopback
        except ValueError:
            local = False
        if not local:
            values = Headers(scope=scope).getlist("authorization")
            credential = values[0].encode("latin-1") if len(values) == 1 else b""
            scheme, _, value = credential.partition(b" ")
            if scheme.lower() != b"bearer" or not hmac.compare_digest(value, self._token):
                response = JSONResponse(
                    {"error": "unauthorized"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
