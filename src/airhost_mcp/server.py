"""HTTP entrypoint.

Exposes a FastMCP server over Streamable HTTP, mounted under
``MCP_MOUNT_PATH`` of a FastAPI app. Every request to the mount path is
gated by an OAuth 2.1 bearer token issued by Auth0.

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
from fastapi import FastAPI

from .airhost import build_airhost_client
from .auth import verify_oauth_token
from .config import get_settings
from .tools import register_tools
from .well_known import build_router as build_well_known_router


def _is_well_known_path(path: str) -> bool:
    return path.startswith("/.well-known/")


def _is_health_path(path: str) -> bool:
    return path == "/health"


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )

    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings

    # streamable_http_path="/" so the mount point itself serves MCP. Without
    # this, FastMCP appends its own "/mcp" and we end up at "/mcp/mcp".
    #
    # DNS rebinding protection is off: it only allows localhost by default,
    # which would block Cloud Run / any reverse proxy. Bearer auth (above)
    # already gates every request, so disabling the host check is safe here.
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

    # FastMCP's session manager owns a task group that must run inside an
    # async context. Hook it into FastAPI's lifespan.
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with mcp.session_manager.run():
            yield

    app = FastAPI(title="airhost-mcp", version="0.1.0", lifespan=lifespan)

    # Cloud Run reserves the literal path "/healthz" at the Frontend layer
    # (it 404s before reaching the container). Use "/health" instead.
    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # OAuth discovery: /.well-known/oauth-protected-resource +
    # /.well-known/oauth-authorization-server. Mounted on the FastAPI app
    # itself so they sit at the public origin root, not under /mcp.
    app.include_router(build_well_known_router())

    # Detect the Cloud Run / managed runtime so DEV_DISABLE_AUTH cannot
    # accidentally be honored in production. Cloud Run injects K_SERVICE for
    # every revision; its presence means "we are running on Cloud Run".
    in_managed_runtime = bool(os.environ.get("K_SERVICE"))

    @app.middleware("http")
    async def oauth_gate(request, call_next):
        path = request.url.path
        if _is_health_path(path) or _is_well_known_path(path):
            return await call_next(request)

        current = get_settings()
        if current.dev_disable_auth and not in_managed_runtime:
            # Local-dev escape hatch. Logged loudly on the first request per
            # process so it's obvious in stderr.
            logger.warning("DEV_DISABLE_AUTH=true — skipping OAuth verification")
            return await call_next(request)

        try:
            await verify_oauth_token(request)
        except Exception as exc:  # HTTPException
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
