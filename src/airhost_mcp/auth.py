"""OAuth 2.1 bearer-token validation backed by Auth0.

The MCP server is a Protected Resource (RFC 9728); Auth0 acts as the
Authorization Server. Clients (e.g. claude.ai) obtain an Auth0 access token
via the Authorization Code + PKCE flow (driven through Google login) and
present it as ``Authorization: Bearer <jwt>`` on every MCP request.

Validation steps performed by :func:`verify_oauth_token`:

1. Extract the bearer token from the ``Authorization`` header.
2. Verify signature, issuer, audience, and expiry against Auth0's JWKS.
3. Require an email claim (Auth0 doesn't put email in access tokens by
   default — see README for the Auth0 Action that copies it as a custom
   claim ``https://airhost-mcp/email``). We accept either the standard
   ``email`` claim or the namespaced custom claim.
4. Require ``email_verified`` true (custom claim
   ``https://airhost-mcp/email_verified`` accepted as fallback).
5. Require ``email`` to be a member of the ``MCP_ALLOWED_EMAILS`` allowlist
   (case-insensitive).

Every failure returns HTTP 401 with a ``WWW-Authenticate: Bearer`` challenge
that points at the protected-resource metadata document so MCP clients can
restart the OAuth dance cleanly.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx
from fastapi import HTTPException, Request, status
from jose import jwt
from jose.exceptions import JWTError

from .config import get_settings

logger = logging.getLogger(__name__)

# Custom-claim names matching the Auth0 Action recommended in README. Auth0
# requires custom claim keys to be namespaced URLs.
_EMAIL_CLAIM_NS = "https://airhost-mcp/email"
_EMAIL_VERIFIED_CLAIM_NS = "https://airhost-mcp/email_verified"

# In-process JWKS cache. Auth0 rotates signing keys infrequently; 10 minutes
# strikes a balance between freshness and avoiding a fetch per request.
_JWKS_TTL_SECONDS = 600.0
_jwks_lock = asyncio.Lock()
_jwks_cache: dict[str, Any] | None = None
_jwks_cache_at: float = 0.0


def reset_jwks_cache() -> None:
    """Test helper: drop the cached JWKS so the next verify call re-fetches."""
    global _jwks_cache, _jwks_cache_at
    _jwks_cache = None
    _jwks_cache_at = 0.0


def _issuer(settings: Any) -> str:
    """Resolve the Auth0 issuer URL.

    Auth0's standard issuer is ``https://{domain}/`` (trailing slash matters —
    the iss claim in tokens carries the slash). Allow override via env for
    custom domain setups.
    """
    if settings.auth0_issuer:
        return settings.auth0_issuer
    if not settings.auth0_domain:
        return ""
    return f"https://{settings.auth0_domain}/"


async def _fetch_jwks(domain: str) -> dict[str, Any]:
    """Fetch and cache Auth0's JWKS document."""
    global _jwks_cache, _jwks_cache_at

    now = time.monotonic()
    if _jwks_cache is not None and (now - _jwks_cache_at) < _JWKS_TTL_SECONDS:
        return _jwks_cache

    async with _jwks_lock:
        # Double-check after the lock — another coroutine may have populated
        # the cache while we waited.
        now = time.monotonic()
        if _jwks_cache is not None and (now - _jwks_cache_at) < _JWKS_TTL_SECONDS:
            return _jwks_cache

        url = f"https://{domain}/.well-known/jwks.json"
        async with httpx.AsyncClient(timeout=5.0) as http:
            resp = await http.get(url)
            resp.raise_for_status()
            data = resp.json()

        _jwks_cache = data
        _jwks_cache_at = now
        return data


def _challenge_header(request: Request) -> dict[str, str]:
    """Build the RFC 6750 / RFC 9728 ``WWW-Authenticate`` challenge."""
    settings = get_settings()
    base = settings.mcp_public_url or str(request.base_url).rstrip("/")
    metadata_url = f"{base.rstrip('/')}/.well-known/oauth-protected-resource"
    return {
        "WWW-Authenticate": (
            f'Bearer realm="airhost-mcp", '
            f'error="invalid_token", '
            f'resource_metadata="{metadata_url}"'
        )
    }


def _unauthorized(request: Request, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers=_challenge_header(request),
    )


def _extract_email(claims: dict[str, Any]) -> tuple[str | None, bool]:
    """Pull the user's email + verification status out of token claims.

    Auth0 access tokens don't include ``email`` by default; readers should
    add an Auth0 Action that copies the user's email into the access token
    as a namespaced custom claim. We accept both the standard claim and the
    namespaced one so either Auth0 setup works.
    """
    email = claims.get(_EMAIL_CLAIM_NS) or claims.get("email")
    verified = claims.get(_EMAIL_VERIFIED_CLAIM_NS)
    if verified is None:
        verified = claims.get("email_verified")
    return (email if isinstance(email, str) and email else None, bool(verified))


async def verify_oauth_token(request: Request) -> dict[str, Any]:
    """Validate the request's bearer token and enforce the email allowlist.

    Returns the verified token claims on success. On any failure raises
    ``HTTPException(401)`` with a populated ``WWW-Authenticate`` header.
    """
    settings = get_settings()

    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise _unauthorized(request, "missing bearer token")
    token = header[7:].strip()
    if not token:
        raise _unauthorized(request, "empty bearer token")

    if not settings.allowed_email_set:
        logger.error("MCP_ALLOWED_EMAILS is empty; rejecting all requests")
        raise _unauthorized(request, "server allowlist not configured")

    if not settings.auth0_domain or not settings.auth0_audience:
        logger.error("AUTH0_DOMAIN and AUTH0_AUDIENCE must be set")
        raise _unauthorized(request, "auth backend not configured")

    issuer = _issuer(settings)

    # Fetch JWKS and pick the key matching the token's kid header.
    try:
        unverified_header = jwt.get_unverified_header(token)
    except JWTError as exc:
        logger.info("malformed JWT header: %s", exc)
        raise _unauthorized(request, "malformed token") from exc

    kid = unverified_header.get("kid")
    if not kid:
        raise _unauthorized(request, "token missing key id")

    try:
        jwks = await _fetch_jwks(settings.auth0_domain)
    except Exception as exc:
        logger.exception("JWKS fetch failed: %s", exc)
        raise _unauthorized(request, "auth backend unavailable") from exc

    matching_key = next(
        (k for k in jwks.get("keys", []) if k.get("kid") == kid), None
    )
    if matching_key is None:
        # The signing key may have rotated since our cache was filled. Drop
        # the cache and try once more before giving up.
        reset_jwks_cache()
        try:
            jwks = await _fetch_jwks(settings.auth0_domain)
        except Exception as exc:
            logger.exception("JWKS refresh failed: %s", exc)
            raise _unauthorized(request, "auth backend unavailable") from exc
        matching_key = next(
            (k for k in jwks.get("keys", []) if k.get("kid") == kid), None
        )
    if matching_key is None:
        raise _unauthorized(request, "no matching signing key")

    try:
        claims = jwt.decode(
            token,
            matching_key,
            algorithms=["RS256"],
            audience=settings.auth0_audience,
            issuer=issuer,
        )
    except JWTError as exc:
        logger.info("Auth0 JWT rejected: %s", exc)
        raise _unauthorized(request, "invalid token") from exc

    email, verified = _extract_email(claims)
    if email is None:
        raise _unauthorized(
            request, "token missing email claim (Auth0 Action required)"
        )
    if not verified:
        raise _unauthorized(request, "email not verified")

    email_lc = email.lower()
    if email_lc not in settings.allowed_email_set:
        raise _unauthorized(request, "email not in allowlist")

    request.state.user_email = email_lc
    return claims
