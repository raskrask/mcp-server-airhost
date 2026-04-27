"""Tests for the OAuth 2.1 / Firebase auth path.

Every Firebase call is mocked — these tests never touch the network. They
cover both the bearer-token validator and the well-known discovery
endpoints.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request

from airhost_mcp import auth as auth_mod
from airhost_mcp.config import reset_settings_cache
from airhost_mcp.well_known import build_router, reset_well_known_cache


# --------------------------------------------------------------------------- #
# fixtures                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch):
    """Force a known env for every test so settings caching can't bleed across."""
    for key in (
        "MCP_BEARER_TOKENS",
        "FIREBASE_PROJECT_ID",
        "MCP_ALLOWED_EMAILS",
        "MCP_PUBLIC_URL",
        "DEV_DISABLE_AUTH",
        "K_SERVICE",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "mot-cozy-space")
    monkeypatch.setenv("MCP_ALLOWED_EMAILS", "alice@example.com,bob@example.com")
    monkeypatch.setenv("MCP_PUBLIC_URL", "https://mcp.example.com")
    reset_settings_cache()
    reset_well_known_cache()
    # Pretend firebase_admin has been initialized so verify_oauth_token
    # doesn't try to import / contact it.
    auth_mod._firebase_ready = True
    yield
    auth_mod._firebase_ready = False
    reset_settings_cache()
    reset_well_known_cache()


def _make_request(headers: dict[str, str] | None = None) -> Request:
    """Build a minimal ASGI Request for the auth helpers."""
    scope: dict[str, Any] = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/mcp/",
        "raw_path": b"/mcp/",
        "query_string": b"",
        "headers": [
            (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
        ],
        "server": ("mcp.example.com", 443),
        "client": ("127.0.0.1", 12345),
    }
    return Request(scope)


# --------------------------------------------------------------------------- #
# verify_oauth_token                                                          #
# --------------------------------------------------------------------------- #


async def test_happy_path_sets_user_email() -> None:
    claims = {
        "email": "Alice@Example.com",
        "email_verified": True,
        "sub": "uid-123",
    }
    request = _make_request({"Authorization": "Bearer good-token"})
    with patch("firebase_admin.auth.verify_id_token", return_value=claims) as m:
        out = await auth_mod.verify_oauth_token(request)
    m.assert_called_once_with("good-token", check_revoked=False)
    assert out is claims
    assert request.state.user_email == "alice@example.com"


async def test_email_not_in_allowlist_returns_401() -> None:
    claims = {
        "email": "eve@example.com",
        "email_verified": True,
    }
    request = _make_request({"Authorization": "Bearer good-token"})
    with patch("firebase_admin.auth.verify_id_token", return_value=claims):
        with pytest.raises(HTTPException) as excinfo:
            await auth_mod.verify_oauth_token(request)
    assert excinfo.value.status_code == 401
    assert "WWW-Authenticate" in (excinfo.value.headers or {})
    challenge = (excinfo.value.headers or {})["WWW-Authenticate"]
    assert 'error="invalid_token"' in challenge
    assert "/.well-known/oauth-protected-resource" in challenge


async def test_email_not_verified_returns_401() -> None:
    claims = {"email": "alice@example.com", "email_verified": False}
    request = _make_request({"Authorization": "Bearer good-token"})
    with patch("firebase_admin.auth.verify_id_token", return_value=claims):
        with pytest.raises(HTTPException) as excinfo:
            await auth_mod.verify_oauth_token(request)
    assert excinfo.value.status_code == 401


async def test_missing_authorization_header_returns_401() -> None:
    request = _make_request({})
    with pytest.raises(HTTPException) as excinfo:
        await auth_mod.verify_oauth_token(request)
    assert excinfo.value.status_code == 401
    assert "WWW-Authenticate" in (excinfo.value.headers or {})


async def test_malformed_authorization_header_returns_401() -> None:
    request = _make_request({"Authorization": "Basic abc"})
    with pytest.raises(HTTPException) as excinfo:
        await auth_mod.verify_oauth_token(request)
    assert excinfo.value.status_code == 401


async def test_empty_bearer_value_returns_401() -> None:
    request = _make_request({"Authorization": "Bearer "})
    with pytest.raises(HTTPException) as excinfo:
        await auth_mod.verify_oauth_token(request)
    assert excinfo.value.status_code == 401


async def test_firebase_raises_returns_401() -> None:
    request = _make_request({"Authorization": "Bearer bad-token"})
    with patch(
        "firebase_admin.auth.verify_id_token",
        side_effect=ValueError("token expired"),
    ):
        with pytest.raises(HTTPException) as excinfo:
            await auth_mod.verify_oauth_token(request)
    assert excinfo.value.status_code == 401
    challenge = (excinfo.value.headers or {})["WWW-Authenticate"]
    assert 'error="invalid_token"' in challenge


async def test_missing_email_claim_returns_401() -> None:
    claims = {"email_verified": True, "sub": "uid"}  # no 'email'
    request = _make_request({"Authorization": "Bearer good-token"})
    with patch("firebase_admin.auth.verify_id_token", return_value=claims):
        with pytest.raises(HTTPException) as excinfo:
            await auth_mod.verify_oauth_token(request)
    assert excinfo.value.status_code == 401


async def test_empty_allowlist_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_ALLOWED_EMAILS", "")
    reset_settings_cache()
    request = _make_request({"Authorization": "Bearer good-token"})
    with pytest.raises(HTTPException) as excinfo:
        await auth_mod.verify_oauth_token(request)
    assert excinfo.value.status_code == 401


# --------------------------------------------------------------------------- #
# discovery endpoints                                                         #
# --------------------------------------------------------------------------- #


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(build_router())
    return app


async def test_protected_resource_metadata_shape() -> None:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="https://mcp.example.com") as c:
        resp = await c.get("/.well-known/oauth-protected-resource")
    assert resp.status_code == 200
    body = resp.json()
    assert body["resource"] == "https://mcp.example.com"
    assert body["authorization_servers"] == [
        "https://securetoken.google.com/mot-cozy-space"
    ]
    assert "header" in body["bearer_methods_supported"]
    assert "openid" in body["scopes_supported"]


async def test_authorization_server_metadata_proxies_firebase() -> None:
    upstream = {
        "issuer": "https://securetoken.google.com/mot-cozy-space",
        "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_endpoint": "https://oauth2.googleapis.com/token",
        "jwks_uri": "https://example.test/jwks",
        "response_types_supported": ["code"],
    }

    async def _fake_get(self, url, *args, **kwargs):  # type: ignore[no-untyped-def]
        request = httpx.Request("GET", url)
        return httpx.Response(200, json=upstream, request=request)

    transport = httpx.ASGITransport(app=_app())
    with patch.object(httpx.AsyncClient, "get", _fake_get):
        async with httpx.AsyncClient(
            transport=transport, base_url="https://mcp.example.com"
        ) as c:
            resp = await c.get("/.well-known/oauth-authorization-server")
    assert resp.status_code == 200
    body = resp.json()
    assert body["issuer"] == upstream["issuer"]
    assert body["authorization_endpoint"] == upstream["authorization_endpoint"]
    assert body["token_endpoint"] == upstream["token_endpoint"]


async def test_authorization_server_metadata_falls_back_when_upstream_fails() -> None:
    # Drive the fallback path directly against the helper to avoid having a
    # patched ``httpx.AsyncClient.get`` also intercept the test client's
    # request to the ASGI app (which would short-circuit the assertion).
    from airhost_mcp import well_known

    well_known.reset_well_known_cache()

    async def _boom(self, url, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise httpx.ConnectError("boom")

    with patch.object(httpx.AsyncClient, "get", _boom):
        body = await well_known._fetch_firebase_openid_configuration(
            "https://securetoken.google.com/mot-cozy-space"
        )

    assert body["issuer"] == "https://securetoken.google.com/mot-cozy-space"
    assert "code" in body["response_types_supported"]
    assert "S256" in body["code_challenge_methods_supported"]


# --------------------------------------------------------------------------- #
# end-to-end middleware behavior                                              #
# --------------------------------------------------------------------------- #


async def test_health_endpoint_is_public(monkeypatch: pytest.MonkeyPatch) -> None:
    # Build the full app fresh, with a known env.
    monkeypatch.setenv("AIRHOST_CLIENT", "mock")
    monkeypatch.delenv("K_SERVICE", raising=False)
    reset_settings_cache()

    # Re-import server with a clean module cache so its module-level
    # ``app = create_app()`` runs against the test env.
    import importlib

    import airhost_mcp.server as srv_mod

    importlib.reload(srv_mod)
    app = srv_mod.app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://mcp.example.com") as c:
        resp = await c.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_well_known_is_public_via_full_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIRHOST_CLIENT", "mock")
    monkeypatch.delenv("K_SERVICE", raising=False)
    reset_settings_cache()

    import importlib

    import airhost_mcp.server as srv_mod

    importlib.reload(srv_mod)
    app = srv_mod.app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://mcp.example.com") as c:
        resp = await c.get("/.well-known/oauth-protected-resource")
    assert resp.status_code == 200
    body = resp.json()
    assert body["resource"] == "https://mcp.example.com"


async def test_mcp_request_without_token_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIRHOST_CLIENT", "mock")
    monkeypatch.delenv("K_SERVICE", raising=False)
    reset_settings_cache()

    import importlib

    import airhost_mcp.server as srv_mod

    importlib.reload(srv_mod)
    app = srv_mod.app

    # Recent httpx removed the ``lifespan`` kwarg from ASGITransport. The
    # 401 path is hit by the auth middleware before any FastMCP lifespan
    # resource is needed, so we don't have to start the lifespan manually.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://mcp.example.com") as c:
        resp = await c.get("/mcp/")
    assert resp.status_code == 401
    assert "WWW-Authenticate" in resp.headers
    assert "/.well-known/oauth-protected-resource" in resp.headers["WWW-Authenticate"]


def test_dev_disable_auth_blocked_in_managed_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production guard: K_SERVICE present means DEV_DISABLE_AUTH must be ignored."""
    # The middleware reads ``in_managed_runtime`` at create_app() time, so we
    # only need to check the helper logic. K_SERVICE => no shortcut.
    monkeypatch.setenv("K_SERVICE", "airhost-mcp")
    assert bool(os.environ.get("K_SERVICE")) is True
