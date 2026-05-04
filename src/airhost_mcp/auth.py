"""Bearer-token validation for the self-hosted OAuth server.

Tokens are HMAC-SHA256 signed JWTs (HS256) issued by ``oauth_server.py``.
Validation steps:
1. Extract the bearer token from the ``Authorization`` header.
2. Decode and verify the JWT signature against ``MCP_TOKEN_SECRET``.
3. Verify ``exp`` has not passed (handled by python-jose).
On failure: HTTP 401 with ``WWW-Authenticate: Bearer`` pointing at the
protected-resource metadata so MCP clients can restart the OAuth dance.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException, Request, status
from jose import jwt
from jose.exceptions import JWTError

from .config import get_settings

logger = logging.getLogger(__name__)


def _challenge_header(request: Request, error_code: str | None = None) -> dict[str, str]:
    settings = get_settings()
    base = settings.mcp_public_url or str(request.base_url).rstrip("/")
    metadata_url = f"{base.rstrip('/')}/.well-known/oauth-protected-resource"
    parts = [
        'Bearer realm="airhost-mcp"',
        f'resource_metadata="{metadata_url}"',
    ]
    if error_code:
        parts.insert(1, f'error="{error_code}"')
    return {"WWW-Authenticate": ", ".join(parts)}


def _unauthorized(request: Request, detail: str, *, token_present: bool = False) -> HTTPException:
    error_code = "invalid_token" if token_present else None
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers=_challenge_header(request, error_code=error_code),
    )


async def verify_oauth_token(request: Request) -> dict[str, Any]:
    """Validate the request's bearer token.

    Returns the verified JWT claims on success.
    Raises ``HTTPException(401)`` on any failure.
    """
    settings = get_settings()

    header = request.headers.get("authorization", "")
    logger.info(
        "DEBUG auth: Authorization header present=%s prefix=%r",
        bool(header),
        header[:20] if header else "",
    )
    if not header.lower().startswith("bearer "):
        raise _unauthorized(request, "missing bearer token")
    token = header[7:].strip()
    if not token:
        raise _unauthorized(request, "empty bearer token")

    if not settings.mcp_token_secret:
        logger.error("MCP_TOKEN_SECRET is not set; rejecting all requests")
        raise _unauthorized(request, "auth not configured")

    try:
        claims = jwt.decode(token, settings.mcp_token_secret, algorithms=["HS256"])
    except JWTError as exc:
        logger.info("token rejected: %s", exc)
        raise _unauthorized(request, "invalid token", token_present=True) from exc

    logger.info("DEBUG auth: token accepted, sub=%r", claims.get("sub"))
    return claims
