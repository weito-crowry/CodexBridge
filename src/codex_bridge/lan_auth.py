"""Authentication for the existing Streamable HTTP MCP transport."""

from __future__ import annotations

import hmac
import ipaddress

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

MCP_PRINCIPAL_SCOPE_KEY = "codex_bridge.mcp_principal"
MCP_PRINCIPAL_FULL = "full"
MCP_PRINCIPAL_CPP = "cpp"


class McpAuthMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        token: str | None,
        cpp_token: str | None = None,
        lan_enabled: bool = True,
    ) -> None:
        self.app = app
        self._token = None if token is None else token.encode("ascii")
        self._cpp_token = None if cpp_token is None else cpp_token.encode("ascii")
        self._lan_enabled = lan_enabled

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        peer = scope.get("client")
        try:
            local = peer is not None and ipaddress.ip_address(peer[0]).is_loopback
        except ValueError:
            local = False
        values = Headers(scope=scope).getlist("authorization")
        principal = None
        if not values:
            if local or not self._lan_enabled:
                principal = MCP_PRINCIPAL_FULL
        elif len(values) == 1:
            credential = values[0].encode("latin-1")
            scheme, separator, value = credential.partition(b" ")
            if scheme.lower() == b"bearer" and separator:
                if self._token is not None and hmac.compare_digest(value, self._token):
                    principal = MCP_PRINCIPAL_FULL
                elif self._cpp_token is not None and hmac.compare_digest(value, self._cpp_token):
                    principal = MCP_PRINCIPAL_CPP
        if principal is None:
            response = JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        # MCP SDK v2 carries this exact HTTP Request through each handler context.
        # Per-scope identity avoids session-ID and concurrent-request privilege leaks.
        state = scope.get("state")
        if not isinstance(state, dict):
            state = {}
            scope["state"] = state
        state[MCP_PRINCIPAL_SCOPE_KEY] = principal
        await self.app(scope, receive, send)


LanMcpAuthMiddleware = McpAuthMiddleware
