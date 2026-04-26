"""Bearer-token auth for the MCP HTTP endpoint.

Two-user setup, so we use a small set of long random tokens compared in
constant time. Tokens come from env (``MCP_BEARER_TOKENS``).
"""

from __future__ import annotations

import hmac

from fastapi import HTTPException, Request, status

from .config import get_settings


def verify_bearer(request: Request) -> str:
    """Validate the ``Authorization: Bearer ...`` header.

    Returns the matched token (so callers can identify which user) or raises
    ``HTTPException`` with 401.
    """
    settings = get_settings()
    valid = settings.bearer_token_set
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="server is missing MCP_BEARER_TOKENS configuration",
        )

    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    presented = header[7:].strip()
    for token in valid:
        if hmac.compare_digest(presented, token):
            return token

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid bearer token",
        headers={"WWW-Authenticate": "Bearer"},
    )
