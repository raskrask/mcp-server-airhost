"""Generic smoke driver for individual BrowserAirhostClient tools.

Usage:
    .venv/bin/python -m scripts.tools_smoke list_listings
    .venv/bin/python -m scripts.tools_smoke get_availability <listing_id> 2026-05-01
    .venv/bin/python -m scripts.tools_smoke get_reservations_on <listing_id> 2026-05-01
    .venv/bin/python -m scripts.tools_smoke list_reservations_in_range 2026-05-01 2026-05-31
    .venv/bin/python -m scripts.tools_smoke list_reservations_in_range 2026-05-01 2026-05-31 <listing_id>

Reuses the persisted Playwright session from .sessions/, so login is
skipped when the storage_state is still fresh.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import traceback
from datetime import date

from airhost_mcp.airhost.browser_client import BrowserAirhostClient
from airhost_mcp.config import get_settings
from airhost_mcp.mfa import build_mfa_strategy
from airhost_mcp.session import build_session_store


def _build_client() -> BrowserAirhostClient:
    settings = get_settings()
    if settings.airhost_client != "browser":
        raise SystemExit("AIRHOST_CLIENT must be 'browser' for this smoke")
    return BrowserAirhostClient(
        login_url=settings.airhost_login_url,
        username=settings.airhost_username,
        password=settings.airhost_password,
        session_store=build_session_store(settings),
        mfa=build_mfa_strategy(settings),
        mfa_timeout_seconds=settings.mfa_timeout_seconds,
        session_ttl_seconds=settings.session_ttl_seconds,
        headless=settings.browser_headless,
    )


def _dump(obj) -> str:
    if hasattr(obj, "model_dump"):
        return json.dumps(obj.model_dump(mode="json"), ensure_ascii=False, indent=2)
    if isinstance(obj, list):
        return json.dumps(
            [o.model_dump(mode="json") if hasattr(o, "model_dump") else o for o in obj],
            ensure_ascii=False,
            indent=2,
        )
    return json.dumps(obj, ensure_ascii=False, indent=2)


async def _run(tool: str, args: list[str]) -> int:
    client = _build_client()
    try:
        if tool == "list_listings":
            result = await client.list_listings()
        elif tool == "get_availability":
            result = await client.get_availability(args[0], date.fromisoformat(args[1]))
        elif tool == "get_reservations_on":
            result = await client.get_reservations_on(args[0], date.fromisoformat(args[1]))
        elif tool == "list_reservations_in_range":
            listing_id = args[2] if len(args) > 2 else None
            result = await client.list_reservations_in_range(
                listing_id, date.fromisoformat(args[0]), date.fromisoformat(args[1])
            )
        elif tool == "block_date":
            reason = args[2] if len(args) > 2 else None
            result = await client.block_date(
                args[0], date.fromisoformat(args[1]), reason
            )
        else:
            print(f"Unknown tool: {tool}", file=sys.stderr)
            return 2

        print(f"\n✅ {tool}:\n{_dump(result)}")
        return 0
    except Exception as exc:
        print(f"\n❌ {tool} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 1
    finally:
        await client.aclose()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    return asyncio.run(_run(sys.argv[1], sys.argv[2:]))


if __name__ == "__main__":
    raise SystemExit(main())
