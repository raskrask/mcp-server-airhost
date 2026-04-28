"""OAuth 2.1 discovery endpoints (RFC 9728 + RFC 8414).

The MCP server is the Protected Resource. Firebase Authentication is the
Authorization Server. claude.ai (and other MCP clients) discover this layout
via two well-known endpoints:

* ``/.well-known/oauth-protected-resource`` — RFC 9728 metadata pointing at
  the Firebase issuer. Required by the MCP authorization spec
  (2025-06-18). Served by us.

* ``/.well-known/oauth-authorization-server`` — RFC 8414 authorization-server
  metadata. The spec says the *client* fetches this from the AS itself, but
  several MCP clients (including some claude.ai builds) probe the resource
  server first. We proxy Firebase's ``openid-configuration`` so those
  clients get a usable response from a single hop. The result is cached
  in-process for ten minutes; cache misses fall through to a hand-written
  subset that is sufficient for OAuth 2.1 discovery.

We do NOT implement Dynamic Client Registration. Firebase Authentication
does not support RFC 7591 — its OAuth clients are managed in the Firebase
console — so DCR is impossible to honor truthfully here. Per the MCP
authorization spec, DCR is a SHOULD (not MUST), and clients that don't
find a ``registration_endpoint`` in the AS metadata fall back to using a
pre-configured client ID. claude.ai is in that bucket.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from fastapi import APIRouter, Request

from .config import get_settings

logger = logging.getLogger(__name__)

# In-process cache for the proxied Firebase OpenID configuration.
_AS_METADATA_CACHE: dict[str, Any] | None = None
_AS_METADATA_CACHE_AT: float = 0.0
_AS_METADATA_TTL_SECONDS: float = 600.0


def _firebase_issuer(project_id: str) -> str:
    return f"https://securetoken.google.com/{project_id}"


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
    """Minimal RFC 8414 metadata sufficient for OAuth 2.1 discovery.

    Used when the upstream Firebase fetch fails. Endpoints reflect Google's
    public OAuth 2.0 endpoints (Firebase delegates to them).
    """
    return {
        "issuer": issuer,
        "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_endpoint": "https://oauth2.googleapis.com/token",
        "jwks_uri": (
            "https://www.googleapis.com/service_accounts/v1/jwk/"
            "securetoken@system.gserviceaccount.com"
        ),
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "scopes_supported": ["openid", "email", "profile"],
        "token_endpoint_auth_methods_supported": [
            "client_secret_post",
            "client_secret_basic",
        ],
        "code_challenge_methods_supported": ["S256"],
    }


async def _fetch_firebase_openid_configuration(issuer: str) -> dict[str, Any]:
    """Fetch and cache Firebase's OpenID configuration document."""
    global _AS_METADATA_CACHE, _AS_METADATA_CACHE_AT

    now = time.monotonic()
    if (
        _AS_METADATA_CACHE is not None
        and (now - _AS_METADATA_CACHE_AT) < _AS_METADATA_TTL_SECONDS
    ):
        return _AS_METADATA_CACHE

    url = f"{issuer}/.well-known/openid-configuration"
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
        project_id = settings.firebase_project_id or "unknown"
        return {
            "resource": _resource_url(request),
            "authorization_servers": [_firebase_issuer(project_id)],
            "bearer_methods_supported": ["header"],
            "scopes_supported": ["openid", "email", "profile"],
            "resource_documentation": "https://github.com/raskrask/mcp-server-airhost",
        }

    @router.get("/.well-known/oauth-authorization-server")
    async def authorization_server_metadata(request: Request) -> dict[str, Any]:
        """RFC 8414 metadata, proxied + cached from Firebase."""
        settings = get_settings()
        project_id = settings.firebase_project_id
        if not project_id:
            # No Firebase configured — return the hand-written fallback so
            # discovery doesn't 500 in dev.
            return _hand_written_as_metadata(_firebase_issuer("unknown"))
        return await _fetch_firebase_openid_configuration(_firebase_issuer(project_id))

    return router
