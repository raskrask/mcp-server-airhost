"""Self-hosted minimal OAuth 2.1 Authorization Server.

Implements Authorization Code + PKCE (RFC 7636) so Claude's MCP connector
can authenticate using the client_id / client_secret it stores at setup time.

No user login page — the client_id + client_secret pair IS the credential.
All legitimate users share the same connector credentials (internal use only).

Flow
----
1. GET /oauth/authorize
       ?client_id=...&response_type=code&code_challenge=...
       &code_challenge_method=S256&redirect_uri=...&state=...
   → validates client_id, stores code↔challenge mapping, redirects to
     redirect_uri?code=CODE&state=STATE immediately (no login page).

2. POST /oauth/token  (grant_type=authorization_code)
       client_id + client_secret + code + code_verifier + redirect_uri
   → verifies PKCE, verifies client_secret, issues JWT access_token +
     opaque refresh_token.

3. POST /oauth/token  (grant_type=refresh_token)
       client_id + client_secret + refresh_token
   → verifies refresh_token, issues new JWT access_token.

Token storage
-------------
Authorization codes and refresh tokens are kept in module-level dicts.
Cloud Run instances may be recycled, but access tokens are long-lived
(default 365 days) so reconnect after a cold start is rare.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .config import get_settings

logger = logging.getLogger(__name__)

# Authorization codes: code → {challenge, redirect_uri, client_id, exp}
_auth_codes: dict[str, dict[str, Any]] = {}
_AUTH_CODE_TTL = 600  # 10 minutes

# Refresh tokens: token → {client_id, exp}
_refresh_tokens: dict[str, dict[str, Any]] = {}


def _issue_access_token(client_id: str, server_url: str, ttl_days: int, token_secret: str) -> str:
    from jose import jwt as jose_jwt

    now = int(time.time())
    claims = {
        "sub": client_id,
        "iss": server_url,
        "iat": now,
        "exp": now + ttl_days * 86400,
    }
    return jose_jwt.encode(claims, token_secret, algorithm="HS256")


def _issue_refresh_token(client_id: str, ttl_days: int = 365) -> str:
    token = secrets.token_urlsafe(32)
    _refresh_tokens[token] = {
        "client_id": client_id,
        "exp": int(time.time()) + ttl_days * 86400,
    }
    return token


def _verify_pkce_s256(verifier: str, challenge: str) -> bool:
    digest = hashlib.sha256(verifier.encode()).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return secrets.compare_digest(expected, challenge)


def _error(error: str, description: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse(
        {"error": error, "error_description": description},
        status_code=status_code,
    )


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get("/oauth/authorize")
    async def authorize(request: Request) -> RedirectResponse:
        settings = get_settings()
        params = dict(request.query_params)

        client_id = params.get("client_id", "")
        response_type = params.get("response_type", "")
        code_challenge = params.get("code_challenge", "")
        code_challenge_method = params.get("code_challenge_method", "")
        redirect_uri = params.get("redirect_uri", "")
        state = params.get("state", "")

        if not settings.mcp_client_id:
            logger.error("MCP_CLIENT_ID not configured")
            return RedirectResponse(
                f"{redirect_uri}?error=server_error&state={state}", status_code=302
            )

        if client_id != settings.mcp_client_id:
            logger.warning("authorize: unknown client_id=%r", client_id)
            return RedirectResponse(
                f"{redirect_uri}?error=unauthorized_client&state={state}", status_code=302
            )

        if response_type != "code":
            return RedirectResponse(
                f"{redirect_uri}?error=unsupported_response_type&state={state}", status_code=302
            )

        if not code_challenge or code_challenge_method != "S256":
            return RedirectResponse(
                f"{redirect_uri}?error=invalid_request"
                f"&error_description=code_challenge+required+S256&state={state}",
                status_code=302,
            )

        code = secrets.token_urlsafe(32)
        _auth_codes[code] = {
            "challenge": code_challenge,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "exp": int(time.time()) + _AUTH_CODE_TTL,
        }
        logger.info("authorize: issued code for client_id=%r", client_id)

        sep = "&" if "?" in redirect_uri else "?"
        location = f"{redirect_uri}{sep}code={code}&state={state}"
        return RedirectResponse(location, status_code=302)

    @router.post("/oauth/token")
    async def token(
        request: Request,
        grant_type: str = Form(...),
        client_id: str = Form(default=""),
        client_secret: str = Form(default=""),
        code: str = Form(default=""),
        code_verifier: str = Form(default=""),
        redirect_uri: str = Form(default=""),
        refresh_token: str = Form(default=""),
    ) -> JSONResponse:
        settings = get_settings()

        if not settings.mcp_client_id or not settings.mcp_client_secret:
            return _error("server_error", "OAuth not configured", 500)

        # Client authentication (also accept HTTP Basic).
        auth_header = request.headers.get("authorization", "")
        logger.info(
            "token: grant_type=%r form_client_id=%r form_secret_present=%s "
            "auth_header_prefix=%r",
            grant_type, client_id, bool(client_secret), auth_header[:20] if auth_header else "",
        )
        if not client_id:
            import base64 as b64mod
            if auth_header.lower().startswith("basic "):
                try:
                    decoded = b64mod.b64decode(auth_header[6:]).decode()
                    client_id, _, client_secret = decoded.partition(":")
                    logger.info("token: extracted client_id from Basic auth: %r", client_id)
                except Exception as exc:
                    logger.warning("token: Basic auth decode failed: %s", exc)

        if not secrets.compare_digest(client_id, settings.mcp_client_id):
            logger.info("token: client_id mismatch received=%r expected=%r", client_id, settings.mcp_client_id)
            return _error("invalid_client", "unknown client_id", 401)
        if not secrets.compare_digest(client_secret, settings.mcp_client_secret):
            logger.info("token: client_secret mismatch (secret_present=%s)", bool(client_secret))
            return _error("invalid_client", "invalid client_secret", 401)

        server_url = (settings.mcp_public_url or str(request.base_url)).rstrip("/")

        if grant_type == "authorization_code":
            entry = _auth_codes.pop(code, None)
            if entry is None:
                return _error("invalid_grant", "unknown or expired code")
            if entry["exp"] < int(time.time()):
                return _error("invalid_grant", "code expired")
            if entry["client_id"] != client_id:
                return _error("invalid_grant", "client_id mismatch")
            if not _verify_pkce_s256(code_verifier, entry["challenge"]):
                return _error("invalid_grant", "code_verifier mismatch")

            access_token = _issue_access_token(
                client_id, server_url,
                settings.mcp_access_token_ttl_days,
                settings.mcp_token_secret,
            )
            rt = _issue_refresh_token(client_id)
            logger.info("token: issued access+refresh for client_id=%r", client_id)
            return JSONResponse({
                "access_token": access_token,
                "token_type": "bearer",
                "expires_in": settings.mcp_access_token_ttl_days * 86400,
                "refresh_token": rt,
                "scope": "offline_access",
            })

        if grant_type == "refresh_token":
            entry = _refresh_tokens.pop(refresh_token, None)
            if entry is None:
                return _error("invalid_grant", "unknown or expired refresh_token")
            if entry["exp"] < int(time.time()):
                return _error("invalid_grant", "refresh_token expired")
            if entry["client_id"] != client_id:
                return _error("invalid_grant", "client_id mismatch")

            access_token = _issue_access_token(
                client_id, server_url,
                settings.mcp_access_token_ttl_days,
                settings.mcp_token_secret,
            )
            new_rt = _issue_refresh_token(client_id)
            logger.info("token: refreshed access token for client_id=%r", client_id)
            return JSONResponse({
                "access_token": access_token,
                "token_type": "bearer",
                "expires_in": settings.mcp_access_token_ttl_days * 86400,
                "refresh_token": new_rt,
                "scope": "offline_access",
            })

        return _error("unsupported_grant_type", f"unsupported grant_type: {grant_type}")

    return router
