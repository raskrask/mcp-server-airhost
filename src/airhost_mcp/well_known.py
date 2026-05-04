"""OAuth 2.1 discovery endpoints (RFC 9728 + RFC 8414).

This server acts as both the Protected Resource and the Authorization Server.
No external OAuth provider (Auth0) is used.

Endpoints
---------
* ``/.well-known/oauth-protected-resource``  — RFC 9728. Points at this server
  as the Authorization Server.
* ``/.well-known/oauth-authorization-server`` — RFC 8414. Describes this
  server's own OAuth endpoints (/oauth/authorize, /oauth/token, /oidc/register).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from .config import get_settings


def _base_url(request: Request) -> str:
    settings = get_settings()
    if settings.mcp_public_url:
        return settings.mcp_public_url.rstrip("/")
    return str(request.base_url).rstrip("/")


def build_router() -> APIRouter:
    router = APIRouter()

    async def _protected_resource_response(request: Request) -> dict[str, Any]:
        base = _base_url(request)
        return {
            "resource": base + "/",
            "authorization_servers": [base + "/"],
            "bearer_methods_supported": ["header"],
            "scopes_supported": ["offline_access"],
            "resource_documentation": "https://github.com/raskrask/mcp-server-airhost",
        }

    @router.get("/.well-known/oauth-protected-resource")
    async def protected_resource_metadata(request: Request) -> dict[str, Any]:
        return await _protected_resource_response(request)

    @router.get("/.well-known/oauth-protected-resource/{path:path}")
    async def protected_resource_metadata_path(
        request: Request, path: str
    ) -> dict[str, Any]:
        return await _protected_resource_response(request)

    @router.get("/.well-known/oauth-authorization-server")
    async def authorization_server_metadata(request: Request) -> dict[str, Any]:
        base = _base_url(request)
        return {
            "issuer": base + "/",
            "authorization_endpoint": base + "/oauth/authorize",
            "token_endpoint": base + "/oauth/token",
            "registration_endpoint": base + "/oidc/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": [
                "client_secret_post",
                "client_secret_basic",
                "none",
            ],
            "scopes_supported": ["offline_access"],
        }

    return router
