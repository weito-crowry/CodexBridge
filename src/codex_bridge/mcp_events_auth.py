from __future__ import annotations

from collections.abc import Mapping
from hmac import compare_digest

from starlette.authentication import AuthCredentials, AuthenticationBackend, BaseUser
from starlette.requests import HTTPConnection

from .lan_auth import MCP_PRINCIPAL_CPP, MCP_PRINCIPAL_SCOPE_KEY

_AUTH_HEADER = "X-CodexBridge-Event-Auth"


class _LocalTunnelOwner(BaseUser):
    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def display_name(self) -> str:
        return "local-tunnel-owner"

    @property
    def identity(self) -> str:
        return "local-tunnel-owner"


class LocalMcpEventsAuthenticationBackend(AuthenticationBackend):
    def __init__(self, token: str) -> None:
        self._token = token.encode("ascii")

    async def authenticate(
        self, connection: HTTPConnection
    ) -> tuple[AuthCredentials, BaseUser] | None:
        state = connection.scope.get("state")
        if isinstance(state, Mapping) and state.get(MCP_PRINCIPAL_SCOPE_KEY) == MCP_PRINCIPAL_CPP:
            return None
        supplied = connection.headers.get(_AUTH_HEADER)
        if supplied is None or not compare_digest(supplied.encode("latin-1"), self._token):
            return None
        return AuthCredentials(["authenticated"]), _LocalTunnelOwner()
