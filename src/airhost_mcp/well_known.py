"""OAuth 2.1 discovery endpoints (RFC 9728 + RFC 8414).

The MCP server is the Protected Resource. **Auth0** is the Authorization
Server. claude.ai (and other MCP clients) discover this layout via two
well-known endpoints:

* ``/.well-known/oauth-protected-resource`` — RFC 9728 metadata pointing at
  the Auth0 issuer. Required by the MCP authorization spec (2025-06-18).
  Served by us.

* ``/.well-known/oauth-authorization-server`` — RFC 8414 authorization-server
  metadata. The spec says the *client* fetches this from the AS itself, but
  several MCP clients (including some claude.ai builds) probe the resource
  server first. We proxy Auth0's ``openid-configuration`` so those clients
  get a usable response from a single hop. The result is cached in-process
  for ten minutes.

Auth0 supports **Dynamic Client Registration (RFC 7591)** natively; its
OpenID configuration includes a populated ``registration_endpoint``. This
is the bit that makes claude.ai's Custom Connector flow work — claude.ai
calls that endpoint to register itself as an OAuth client without any
manual setup on our side.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from fastapi import APIRouter, Request

from .config import get_settings

logger = logging.getLogger(__name__)

# In-process cache for the proxied Auth0 OpenID configuration.
_AS_METADATA_CACHE: dict[str, Any] | None = None
_AS_METADATA_CACHE_AT: float = 0.0
_AS_METADATA_TTL_SECONDS: float = 600.0


def _auth0_issuer(settings: Any) -> str:
    """Resolve the Auth0 issuer URL (with trailing slash, per Auth0 convention)."""
    if settings.auth0_issuer:
        return settings.auth0_issuer
    if not settings.auth0_domain:
        return ""
    return f"https://{settings.auth0_domain}/"


def _resource_url(request: Request) -> str:
    """Compute the canonical resource URL for this MCP server.

    Preference order: explicit ``MCP_PUBLIC_URL`` setting → request base URL
    (works behind Cloud Run's HTTPS terminator). RFC 8707 prefers the form
    without a trailing slash for interoperability.
    """
    settings = get_settings()
    if settings.mcp_public_url:
        return settings.mcp_public_url.rstrip("/")
    return str(request.base_url).rstrip("/")


def _hand_written_as_metadata(issuer: str) -> dict[str, Any]:
    """Minimal RFC 8414 metadata used when the Auth0 fetch fails.

    Endpoints follow Auth0's standard URL pattern. ``registration_endpoint``
    is included because Auth0 supports DCR — clients shouldn't have to
    fall back to this hand-written subset in production, but listing the
    endpoint keeps DCR working even on a temporary upstream blip.
    """
    base = issuer.rstrip("/")
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "userinfo_endpoint": f"{base}/userinfo",
        "registration_endpoint": f"{base}/oidc/register",
        "jwks_uri": f"{base}/.well-known/jwks.json",
        "response_types_supported": ["code", "token", "id_token"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "scopes_supported": ["openid", "email", "profile"],
        "token_endpoint_auth_methods_supported": [
            "client_secret_post",
            "client_secret_basic",
            "none",
        ],
        "code_challenge_methods_supported": ["S256"],
    }


async def _fetch_auth0_openid_configuration(issuer: str) -> dict[str, Any]:
    """Fetch and cache Auth0's OpenID configuration document."""
    global _AS_METADATA_CACHE, _AS_METADATA_CACHE_AT

    now = time.monotonic()
    if (
        _AS_METADATA_CACHE is not None
        and (now - _AS_METADATA_CACHE_AT) < _AS_METADATA_TTL_SECONDS
    ):
        return _AS_METADATA_CACHE

    url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    try:
        async with httpx.AsyncClient(timeout=5.0) as http:
            resp = await http.get(url)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # network / parse failure
        logger.warning("upstream AS metadata fetch failed (%s); using fallback", exc)
        data = _hand_written_as_metadata(issuer)

    _AS_METADATA_CACHE = data
    _AS_METADATA_CACHE_AT = now
    return data


def reset_well_known_cache() -> None:
    """Test helper: drop the cached AS metadata."""
    global _AS_METADATA_CACHE, _AS_METADATA_CACHE_AT
    _AS_METADATA_CACHE = None
    _AS_METADATA_CACHE_AT = 0.0


def build_router() -> APIRouter:
    """Return a FastAPI router that exposes the OAuth discovery endpoints."""
    router = APIRouter()

    @router.get("/.well-known/oauth-protected-resource")
    async def protected_resource_metadata(request: Request) -> dict[str, Any]:
        """RFC 9728 protected-resource metadata for this MCP server."""
        settings = get_settings()
        issuer = _auth0_issuer(settings)
        return {
            "resource": _resource_url(request),
            "authorization_servers": [issuer] if issuer else [],
            "bearer_methods_supported": ["header"],
            "scopes_supported": ["openid", "email", "profile"],
            "resource_documentation": "https://github.com/raskrask/mcp-server-airhost",
        }

    @router.get("/.well-known/oauth-authorization-server")
    async def authorization_server_metadata(request: Request) -> dict[str, Any]:
        """RFC 8414 metadata, proxied + cached from Auth0."""
        settings = get_settings()
        issuer = _auth0_issuer(settings)
        if not issuer:
            # No Auth0 configured — return a placeholder fallback so discovery
            # doesn't 500 in local dev.
            return _hand_written_as_metadata("https://unconfigured.auth0.invalid/")
        return await _fetch_auth0_openid_configuration(issuer)

    return router
