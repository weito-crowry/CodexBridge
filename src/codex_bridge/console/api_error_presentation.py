"""Translate known local API errors only at UI display boundaries.

ApiClient signals retain their English strings because control code consumes
those fixed messages. Unknown backend details remain verbatim.
"""

from __future__ import annotations

from .i18n import tr

_LOCAL_API_MESSAGES = frozenset(
    {
        "Thread not found",
        "Request failed",
        "Invalid response",
        "Bridge unavailable",
        "Bridge control request failed",
        "Approval already resolved",
        "Invalid approval response",
    }
)


def format_api_error(message: str) -> str:
    return tr(message) if message in _LOCAL_API_MESSAGES else message
