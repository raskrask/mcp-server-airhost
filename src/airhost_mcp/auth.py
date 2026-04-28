"""OAuth 2.1 bearer-token validation backed by Firebase Authentication.

The MCP server is a Protected Resource (RFC 9728); Firebase Authentication
acts as the Authorization Server. Clients (e.g. claude.ai) obtain a Firebase
ID token via the standard browser sign-in flow and present it as
``Authorization: Bearer <jwt>`` on every MCP request.

Validation steps performed by :func:`verify_oauth_token`:

1. Extract the bearer token from the ``Authorization`` header.
2. Verify signature, issuer, audience, and expiry via
   ``firebase_admin.auth.verify_id_token``.
3. Require ``email_verified`` true.
4. Require ``email`` to be a member of the ``MCP_ALLOWED_EMAILS`` allowlist
   (case-insensitive).

Every failure returns HTTP 401 with a ``WWW-Authenticate: Bearer`` challenge
that points at the protected-resource metadata document so MCP clients can
restart the OAuth dance cleanly.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from fastapi import HTTPException, Request, status

from .config import get_settings

logger = logging.getLogger(__name__)

_FIREBASE_INIT_LOCK = threading.Lock()
_firebase_ready: bool = False


def _ensure_firebase_initialized() -> None:
    """Lazy, idempotent ``firebase_admin`` initialization.

    On Cloud Run the SDK auto-discovers Application Default Credentials, so no
    service-account key file is required. Locally the operator can set
    ``GOOGLE_APPLICATION_CREDENTIALS`` to a downloaded key.
    """
    global _firebase_ready
    if _firebase_ready:
        return
    with _FIREBASE_INIT_LOCK:
        if _firebase_ready:
            return
        import firebase_admin  # imported lazily to keep test startup fast

        if not firebase_admin._apps:  # type: ignore[attr-defined]
            settings = get_settings()
            options: dict[str, Any] = {}
            if settings.firebase_project_id:
                options["projectId"] = settings.firebase_project_id
            firebase_admin.initialize_app(options=options or None)
        _firebase_ready = True


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


async def verify_oauth_token(request: Request) -> dict[str, Any]:
    """Validate the request's bearer token and enforce the email allowlist.

    Returns the verified Firebase ID-token claims on success. On any failure
    raises ``HTTPException(401)`` with a populated ``WWW-Authenticate`` header.

    The function is async to fit FastAPI dependency / middleware idioms; the
    underlying Firebase call is synchronous but cheap (in-process JWKS cache),
    so we don't bother offloading to a thread.
    """
    settings = get_settings()

    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise _unauthorized(request, "missing bearer token")
    token = header[7:].strip()
    if not token:
        raise _unauthorized(request, "empty bearer token")

    if not settings.allowed_email_set:
        # Misconfiguration: allowlist must be populated before deployment. We
        # still emit 401 (not 500) so MCP clients re-authenticate cleanly; the
        # log is the operator's signal.
        logger.error("MCP_ALLOWED_EMAILS is empty; rejecting all requests")
        raise _unauthorized(request, "server allowlist not configured")

    try:
        _ensure_firebase_initialized()
    except Exception as exc:  # pragma: no cover - init failure is exceptional
        logger.exception("firebase_admin initialization failed: %s", exc)
        raise _unauthorized(request, "auth backend unavailable") from exc

    try:
        from firebase_admin import auth as fb_auth  # local import: lazy

        claims = fb_auth.verify_id_token(token, check_revoked=False)
    except Exception as exc:
        logger.info("firebase verify_id_token rejected token: %s", exc)
        raise _unauthorized(request, "invalid id token") from exc

    if not claims.get("email_verified"):
        raise _unauthorized(request, "email not verified")

    email = claims.get("email")
    if not isinstance(email, str) or not email:
        raise _unauthorized(request, "id token missing email claim")
    email_lc = email.lower()
    if email_lc not in settings.allowed_email_set:
        # Per the brief: allowlist failures stay 401 (not 403) so MCP clients
        # transparently re-trigger the OAuth flow.
        raise _unauthorized(request, "email not in allowlist")

    request.state.user_email = email_lc
    return claims
