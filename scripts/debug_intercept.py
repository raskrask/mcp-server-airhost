"""Intercept the headers Airhost's own frontend sends to api2.airhost.co.

Drives the calendar page in a logged-in Chromium and prints every
request to api2.airhost.co with all headers. The actual x-csrf-token
value (and any other auth headers) the frontend uses will appear here.
"""

from __future__ import annotations

import asyncio
import logging

from airhost_mcp.airhost.browser_client import BrowserAirhostClient
from airhost_mcp.config import get_settings
from airhost_mcp.mfa import build_mfa_strategy
from airhost_mcp.session import build_session_store


async def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    client = BrowserAirhostClient(
        login_url=settings.airhost_login_url,
        username=settings.airhost_username,
        password=settings.airhost_password,
        session_store=build_session_store(settings),
        mfa=build_mfa_strategy(settings),
        mfa_timeout_seconds=settings.mfa_timeout_seconds,
        session_ttl_seconds=settings.session_ttl_seconds,
        headless=settings.browser_headless,
    )

    try:
        async with client._page() as page:
            seen: list[dict] = []

            def on_request(request) -> None:
                if "api2.airhost.co" in request.url:
                    seen.append(
                        {
                            "method": request.method,
                            "url": request.url,
                            "headers": dict(request.headers),
                        }
                    )

            page.on("request", on_request)

            print("Navigating to /ja/booking_calendar to trigger API requests...")
            await page.goto(
                "https://pms.airhost.co/ja/booking_calendar",
                wait_until="networkidle",
            )
            await asyncio.sleep(2)  # let stragglers fire

            print(f"\n=== captured {len(seen)} api2.airhost.co requests ===\n")
            for i, r in enumerate(seen[:10]):
                print(f"--- [{i}] {r['method']} {r['url']}")
                for k in sorted(r["headers"]):
                    if k.startswith(":"):
                        continue
                    val = r["headers"][k]
                    short = val if len(val) < 120 else val[:80] + "..."
                    if k.lower() in ("cookie", "user-agent", "accept-language",
                                      "accept-encoding", "sec-ch-ua", "sec-ch-ua-mobile",
                                      "sec-ch-ua-platform", "sec-fetch-site",
                                      "sec-fetch-mode", "sec-fetch-dest", "priority"):
                        continue
                    print(f"  {k}: {short}")

    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(_main())
