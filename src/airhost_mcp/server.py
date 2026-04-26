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

import uvicorn
from fastapi import Depends, FastAPI

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

    # FastMCP — Streamable HTTP transport. Mounted under our chosen path.
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP(
        name="airhost-mcp",
        instructions=(
            "Tools for managing Airhost listings, availability, and reservations. "
            "All write operations affect live Airhost state when the HTTP client is "
            "selected. The server is single-tenant — credentials are server-side."
        ),
    )

    client = build_airhost_client(settings)
    register_tools(mcp, client)

    app = FastAPI(title="airhost-mcp", version="0.1.0")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # Mount the MCP Streamable HTTP app under the configured path.
    # `streamable_http_app()` returns a Starlette app exposing the MCP protocol.
    mcp_app = mcp.streamable_http_app()
    app.mount(
        settings.mcp_mount_path,
        mcp_app,
    )

    # Apply Bearer auth to every request to the MCP mount path. We do this with
    # a dependency-bearing route guard so the dependency runs *before* the
    # mounted sub-app handles the request.
    @app.middleware("http")
    async def bearer_gate(request, call_next):
        path = request.url.path
        mount = settings.mcp_mount_path.rstrip("/")
        if path == mount or path.startswith(mount + "/"):
            # Re-use the FastAPI dependency machinery for consistent errors.
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

    # Reference the dependency to keep the import alive for tooling/IDEs.
    _ = Depends(verify_bearer)

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
