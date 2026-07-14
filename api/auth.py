"""API key authentication dependency.

If `GATEWAY_API_KEYS` is empty, auth is disabled (useful for local dev).
Otherwise, every request must carry `Authorization: Bearer <key>` where
`<key>` is one of the configured values.
"""
from __future__ import annotations

import hmac
from typing import Optional

from fastapi import Request

from api.errors import AuthenticationError
from config import Settings


def _extract_bearer_token(request: Request) -> Optional[str]:
    auth = request.headers.get("Authorization") or request.headers.get("authorization")
    if not auth:
        return None
    parts = auth.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip()


def _extract_api_key_header(request: Request) -> Optional[str]:
    # Some clients send `api-key: <key>` (e.g. older Azure SDK shapes).
    return request.headers.get("api-key") or request.headers.get("X-Api-Key")


async def verify_api_key(request: Request, settings: Settings) -> None:
    """FastAPI dependency: raise AuthenticationError if the key is missing/wrong."""
    if not settings.auth_enabled:
        return  # auth disabled — open mode

    presented = _extract_bearer_token(request) or _extract_api_key_header(request)
    if not presented:
        raise AuthenticationError(
            "Missing API key. Send it as `Authorization: Bearer <key>`.",
            code="missing_api_key",
        )

    # Constant-time comparison to avoid timing leaks.
    for accepted in settings.api_keys:
        if hmac.compare_digest(presented, accepted):
            return

    raise AuthenticationError(
        "Invalid API key.", code="invalid_api_key"
    )
