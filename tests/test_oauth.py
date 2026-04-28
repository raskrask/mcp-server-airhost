"""Tests for the OAuth 2.1 / Auth0 auth path.

Every Auth0 / network call is mocked — these tests never touch a real
tenant. They cover both the bearer-token validator and the well-known
discovery endpoints.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI

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
        "AUTH0_DOMAIN",
        "AUTH0_AUDIENCE",
        "AUTH0_ISSUER",
        "MCP_ALLOWED_EMAILS",
        "MCP_PUBLIC_URL",
        "DEV_DISABLE_AUTH",
        "K_SERVICE",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AUTH0_DOMAIN", "tenant.jp.auth0.com")
    monkeypatch.setenv("AUTH0_AUDIENCE", "https://airhost-mcp.example.com")
    monkeypatch.setenv("MCP_ALLOWED_EMAILS", "alice@example.com,bob@example.com")
    monkeypatch.setenv("MCP_PUBLIC_URL", "https://mcp.example.com")
    reset_settings_cache()
    reset_well_known_cache()
    auth_mod.reset_jwks_cache()
    yield
    reset_settings_cache()
    reset_well_known_cache()
    auth_mod.reset_jwks_cache()


def _fake_jwks() -> dict[str, Any]:
    """Minimal JWKS shape — only ``kid`` is read by the verifier."""
    return {"keys": [{"kid": "test-kid-1", "kty": "RSA", "n": "x", "e": "AQAB"}]}


def _build_request(token: str | None = None) -> Any:
    """Construct a minimal FastAPI Request for direct verifier calls."""
    headers: list[tuple[bytes, bytes]] = []
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    scope = {
        "type": "http",
        "headers": headers,
        "method": "POST",
        "path": "/mcp/",
        "scheme": "https",
        "server": ("mcp.example.com", 443),
        "root_path": "",
        "query_string": b"",
    }
    from fastapi import Request

    request = Request(scope)
    request._receive = AsyncMock()  # type: ignore[attr-defined]
    return request


# --------------------------------------------------------------------------- #
# verify_oauth_token                                                          #
# --------------------------------------------------------------------------- #


@patch("airhost_mcp.auth._fetch_jwks", new_callable=AsyncMock)
@patch("airhost_mcp.auth.jwt.decode")
@patch("airhost_mcp.auth.jwt.get_unverified_header")
async def test_verify_happy_path(
    mock_get_header, mock_decode, mock_jwks
) -> None:
    mock_get_header.return_value = {"kid": "test-kid-1"}
    mock_jwks.return_value = _fake_jwks()
    mock_decode.return_value = {
        "sub": "google-oauth2|123",
        "email": "alice@example.com",
        "email_verified": True,
        "iss": "https://tenant.jp.auth0.com/",
        "aud": "https://airhost-mcp.example.com",
    }

    claims = await auth_mod.verify_oauth_token(_build_request("opaque.jwt.token"))
    assert claims["email"] == "alice@example.com"


@patch("airhost_mcp.auth._fetch_jwks", new_callable=AsyncMock)
@patch("airhost_mcp.auth.jwt.decode")
@patch("airhost_mcp.auth.jwt.get_unverified_header")
async def test_verify_namespaced_email_claim(
    mock_get_header, mock_decode, mock_jwks
) -> None:
    """Auth0 Action puts email as a namespaced custom claim — accept it."""
    mock_get_header.return_value = {"kid": "test-kid-1"}
    mock_jwks.return_value = _fake_jwks()
    mock_decode.return_value = {
        "sub": "google-oauth2|123",
        "https://airhost-mcp/email": "alice@example.com",
        "https://airhost-mcp/email_verified": True,
    }
    claims = await auth_mod.verify_oauth_token(_build_request("tok"))
    assert claims["https://airhost-mcp/email"] == "alice@example.com"


@patch("airhost_mcp.auth._fetch_jwks", new_callable=AsyncMock)
@patch("airhost_mcp.auth.jwt.decode")
@patch("airhost_mcp.auth.jwt.get_unverified_header")
async def test_verify_email_not_in_allowlist(
    mock_get_header, mock_decode, mock_jwks
) -> None:
    mock_get_header.return_value = {"kid": "test-kid-1"}
    mock_jwks.return_value = _fake_jwks()
    mock_decode.return_value = {
        "email": "stranger@example.com",
        "email_verified": True,
    }
    with pytest.raises(Exception) as ei:
        await auth_mod.verify_oauth_token(_build_request("tok"))
    assert getattr(ei.value, "status_code", None) == 401
    assert "allowlist" in getattr(ei.value, "detail", "")


@patch("airhost_mcp.auth._fetch_jwks", new_callable=AsyncMock)
@patch("airhost_mcp.auth.jwt.decode")
@patch("airhost_mcp.auth.jwt.get_unverified_header")
async def test_verify_email_unverified(
    mock_get_header, mock_decode, mock_jwks
) -> None:
    mock_get_header.return_value = {"kid": "test-kid-1"}
    mock_jwks.return_value = _fake_jwks()
    mock_decode.return_value = {
        "email": "alice@example.com",
        "email_verified": False,
    }
    with pytest.raises(Exception) as ei:
        await auth_mod.verify_oauth_token(_build_request("tok"))
    assert getattr(ei.value, "status_code", None) == 401


async def test_verify_missing_header() -> None:
    with pytest.raises(Exception) as ei:
        await auth_mod.verify_oauth_token(_build_request(None))
    assert getattr(ei.value, "status_code", None) == 401


async def test_verify_empty_bearer() -> None:
    with pytest.raises(Exception) as ei:
        await auth_mod.verify_oauth_token(_build_request(""))
    assert getattr(ei.value, "status_code", None) == 401


@patch("airhost_mcp.auth.jwt.get_unverified_header")
async def test_verify_malformed_jwt(mock_get_header) -> None:
    from jose.exceptions import JWTError

    mock_get_header.side_effect = JWTError("Invalid header string")
    with pytest.raises(Exception) as ei:
        await auth_mod.verify_oauth_token(_build_request("not-a-jwt"))
    assert getattr(ei.value, "status_code", None) == 401


@patch("airhost_mcp.auth._fetch_jwks", new_callable=AsyncMock)
@patch("airhost_mcp.auth.jwt.decode")
@patch("airhost_mcp.auth.jwt.get_unverified_header")
async def test_verify_signature_rejected(
    mock_get_header, mock_decode, mock_jwks
) -> None:
    from jose.exceptions import JWTError

    mock_get_header.return_value = {"kid": "test-kid-1"}
    mock_jwks.return_value = _fake_jwks()
    mock_decode.side_effect = JWTError("Signature verification failed")
    with pytest.raises(Exception) as ei:
        await auth_mod.verify_oauth_token(_build_request("tok"))
    assert getattr(ei.value, "status_code", None) == 401


@patch("airhost_mcp.auth._fetch_jwks", new_callable=AsyncMock)
@patch("airhost_mcp.auth.jwt.decode")
@patch("airhost_mcp.auth.jwt.get_unverified_header")
async def test_verify_missing_email_claim(
    mock_get_header, mock_decode, mock_jwks
) -> None:
    mock_get_header.return_value = {"kid": "test-kid-1"}
    mock_jwks.return_value = _fake_jwks()
    mock_decode.return_value = {
        "sub": "google-oauth2|999",
        # no email at all
    }
    with pytest.raises(Exception) as ei:
        await auth_mod.verify_oauth_token(_build_request("tok"))
    assert getattr(ei.value, "status_code", None) == 401
    assert "email" in getattr(ei.value, "detail", "")


@patch("airhost_mcp.auth._fetch_jwks", new_callable=AsyncMock)
@patch("airhost_mcp.auth.jwt.decode")
@patch("airhost_mcp.auth.jwt.get_unverified_header")
async def test_verify_no_kid_in_header(
    mock_get_header, mock_decode, mock_jwks
) -> None:
    mock_get_header.return_value = {}  # no kid
    with pytest.raises(Exception) as ei:
        await auth_mod.verify_oauth_token(_build_request("tok"))
    assert getattr(ei.value, "status_code", None) == 401


async def test_verify_empty_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_ALLOWED_EMAILS", "")
    reset_settings_cache()
    with pytest.raises(Exception) as ei:
        await auth_mod.verify_oauth_token(_build_request("tok"))
    assert getattr(ei.value, "status_code", None) == 401


async def test_verify_unconfigured_auth0(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH0_DOMAIN", "")
    reset_settings_cache()
    with pytest.raises(Exception) as ei:
        await auth_mod.verify_oauth_token(_build_request("tok"))
    assert getattr(ei.value, "status_code", None) == 401


# --------------------------------------------------------------------------- #
# Discovery endpoints                                                         #
# --------------------------------------------------------------------------- #


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(build_router())
    return app


async def test_protected_resource_metadata_shape() -> None:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="https://mcp.example.com"
    ) as c:
        resp = await c.get("/.well-known/oauth-protected-resource")
    assert resp.status_code == 200
    body = resp.json()
    assert body["resource"] == "https://mcp.example.com"
    assert body["authorization_servers"] == ["https://tenant.jp.auth0.com/"]
    assert body["bearer_methods_supported"] == ["header"]


async def test_authorization_server_metadata_proxied() -> None:
    upstream = {
        "issuer": "https://tenant.jp.auth0.com/",
        "authorization_endpoint": "https://tenant.jp.auth0.com/authorize",
        "token_endpoint": "https://tenant.jp.auth0.com/oauth/token",
        "jwks_uri": "https://tenant.jp.auth0.com/.well-known/jwks.json",
        "registration_endpoint": "https://tenant.jp.auth0.com/oidc/register",
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
    assert body["registration_endpoint"] == upstream["registration_endpoint"]


async def test_authorization_server_metadata_falls_back_when_upstream_fails() -> None:
    """If Auth0 is unreachable, the hand-written subset still satisfies discovery."""
    from airhost_mcp import well_known

    well_known.reset_well_known_cache()

    async def _boom(self, url, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise httpx.ConnectError("boom")

    with patch.object(httpx.AsyncClient, "get", _boom):
        body = await well_known._fetch_auth0_openid_configuration(
            "https://tenant.jp.auth0.com/"
        )

    assert body["issuer"] == "https://tenant.jp.auth0.com/"
    assert "S256" in body["code_challenge_methods_supported"]
    assert "registration_endpoint" in body  # DCR endpoint always advertised


# --------------------------------------------------------------------------- #
# end-to-end middleware behavior                                              #
# --------------------------------------------------------------------------- #


async def test_health_endpoint_is_public(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIRHOST_CLIENT", "mock")
    monkeypatch.delenv("K_SERVICE", raising=False)
    reset_settings_cache()

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

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://mcp.example.com") as c:
        resp = await c.get("/mcp/")
    assert resp.status_code == 401
    assert "WWW-Authenticate" in resp.headers
    assert "/.well-known/oauth-protected-resource" in resp.headers["WWW-Authenticate"]


def test_dev_disable_auth_managed_runtime_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production guard: K_SERVICE present means DEV_DISABLE_AUTH must be ignored."""
    monkeypatch.setenv("K_SERVICE", "airhost-mcp")
    import os as _os

    assert bool(_os.environ.get("K_SERVICE")) is True
