"""HTTP entrypoint.

Exposes a FastMCP server over Streamable HTTP, mounted under
``MCP_MOUNT_PATH`` of a FastAPI app. Every request to the mount path is
gated by a bearer token issued by the self-hosted OAuth server.

Run locally::

    uvicorn airhost_mcp.server:app --host 0.0.0.0 --port 8080

Or via the console script::

    airhost-mcp
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request, Response

from .airhost import build_airhost_client
from .auth import verify_oauth_token
from .config import get_settings
from .oauth_server import build_router as build_oauth_router
from .oauth_server import load_refresh_tokens_from_gcs
from .tools import register_tools
from .well_known import build_router as build_well_known_router


def _is_public_path(path: str) -> bool:
    return (
        path.startswith("/.well-known/")
        or path == "/health"
        or path == "/oidc/register"
        or path == "/oauth/authorize"
        or path == "/oauth/token"
    )


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )

    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings

    mcp = FastMCP(
        name="airhost-mcp",
        instructions=(
            "Tools for managing Airhost listings, availability, and reservations. "
            "All write operations affect live Airhost state when the browser client "
            "is selected. The server is single-tenant — credentials are server-side."
        ),
        streamable_http_path=settings.mcp_mount_path,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    client = build_airhost_client(settings)
    register_tools(mcp, client)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Restore persisted OAuth refresh tokens from GCS before accepting traffic.
        if settings.session_gcs_bucket:
            await load_refresh_tokens_from_gcs(settings.session_gcs_bucket)
        async with mcp.session_manager.run():
            yield

    app = FastAPI(title="airhost-mcp", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # /oidc/register: return the pre-configured client_id so Claude's connector
    # does not need to perform Dynamic Client Registration against an external AS.
    @app.post("/oidc/register")
    async def oidc_register(request: Request) -> Response:
        settings = get_settings()
        if not settings.mcp_client_id:
            from fastapi.responses import JSONResponse
            return JSONResponse({"error": "not_supported"}, status_code=404)

        body: dict = {}
        try:
            body = await request.json()
        except Exception:
            pass

        from fastapi.responses import JSONResponse
        return JSONResponse(
            {
                "client_id": settings.mcp_client_id,
                "client_name": body.get("client_name", "airhost-mcp-client"),
                "redirect_uris": body.get("redirect_uris", []),
                "grant_types": body.get("grant_types", ["authorization_code"]),
                "response_types": body.get("response_types", ["code"]),
                "token_endpoint_auth_method": "client_secret_post",
            },
            status_code=201,
        )

    app.include_router(build_well_known_router())
    app.include_router(build_oauth_router())

    in_managed_runtime = bool(os.environ.get("K_SERVICE"))

    @app.middleware("http")
    async def oauth_gate(request, call_next):
        path = request.url.path
        if _is_public_path(path):
            return await call_next(request)

        current = get_settings()
        if current.dev_disable_auth and not in_managed_runtime:
            logger.warning("DEV_DISABLE_AUTH=true — skipping OAuth verification")
            return await call_next(request)

        try:
            await verify_oauth_token(request)
        except Exception as exc:
            from fastapi.responses import JSONResponse

            status_code = getattr(exc, "status_code", 500)
            detail = getattr(exc, "detail", "auth error")
            headers = getattr(exc, "headers", None) or {}
            return JSONResponse(
                {"error": detail}, status_code=status_code, headers=headers
            )
        return await call_next(request)

    app.mount("/", mcp.streamable_http_app())

    return app


logger = logging.getLogger(__name__)
app = create_app()


def main() -> None:
    settings = get_settings()
    port = int(os.environ.get("PORT", settings.port))
    uvicorn.run(
        "airhost_mcp.server:app",
        host=settings.host,
        port=port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
