"""HTTP entrypoint.

Exposes a FastMCP server over Streamable HTTP, mounted under
``MCP_MOUNT_PATH`` of a FastAPI app. Every request to the mount path is
gated by Bearer-token auth.

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
from .auth import verify_bearer
from .config import get_settings
from .tools import register_tools


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
        streamable_http_path="/",
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

    # Apply Bearer auth to every request to the MCP mount path.
    @app.middleware("http")
    async def bearer_gate(request, call_next):
        path = request.url.path
        mount = settings.mcp_mount_path.rstrip("/")
        if path == mount or path.startswith(mount + "/"):
            try:
                verify_bearer(request)
            except Exception as exc:  # HTTPException
                from fastapi.responses import JSONResponse

                status_code = getattr(exc, "status_code", 500)
                detail = getattr(exc, "detail", "auth error")
                headers = getattr(exc, "headers", None) or {}
                return JSONResponse(
                    {"error": detail}, status_code=status_code, headers=headers
                )
        return await call_next(request)

    app.mount(settings.mcp_mount_path, mcp.streamable_http_app())

    return app


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
