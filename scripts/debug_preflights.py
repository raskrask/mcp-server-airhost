"""Inspect /pms/settings/preflights to see how the CSRF token is delivered."""

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
            # Pre-bind a response handler so we don't miss the early ones.
            captured: list[dict] = []

            async def on_response(response):
                if "api2.airhost.co" in response.url:
                    try:
                        body = await response.text()
                    except Exception:
                        body = "<unreadable>"
                    captured.append(
                        {
                            "url": response.url,
                            "status": response.status,
                            "headers": dict(response.headers),
                            "body": body[:500],
                        }
                    )

            page.on("response", on_response)

            await page.goto(
                "https://pms.airhost.co/ja/booking_calendar",
                wait_until="networkidle",
            )
            await asyncio.sleep(2)

            print("=== preflights response — all headers + csrf hunt ===\n")
            target = next(
                (r for r in captured if "preflights" in r["url"]), None
            )
            if not target:
                print("No preflights response captured")
                return

            print(f"  URL: {target['url']}")
            print(f"  Status: {target['status']}")
            print(f"  ALL headers (no filtering):")
            for k, v in target["headers"].items():
                print(f"    {k}: {v[:200]}")
            print()
            full_body = target["body"]  # already truncated to 500
            print(f"  body length captured: {len(full_body)} chars")

            # Re-fetch the body without truncation by calling it from the page.
            print("\n=== re-fetching preflights body in full + searching for 'csrf' ===")
            full = await page.request.get(
                "https://api2.airhost.co/api/one/pms/settings/preflights"
            )
            text = await full.text()
            print(f"  full body length: {len(text)} chars")
            for kw in ["csrf", "xsrf", "token", "Csrf", "CSRF"]:
                idx = text.find(kw)
                if idx != -1:
                    snippet = text[max(0, idx - 40) : idx + 100]
                    print(f"  ⭐ '{kw}' found at {idx}: ...{snippet}...")
                else:
                    print(f"  '{kw}' not found")

            # Also enumerate Set-Cookie and any other auth headers across
            # all captured responses (in case the token is delivered out-of-band).
            print("\n=== Set-Cookie / auth-ish headers across all responses ===")
            for r in captured[:8]:
                interesting = {
                    k: v for k, v in r["headers"].items()
                    if "cookie" in k.lower()
                    or "csrf" in k.lower()
                    or "xsrf" in k.lower()
                    or "auth" in k.lower()
                    or "token" in k.lower()
                }
                if interesting:
                    short_url = r["url"].split("/api/one/")[-1].split("?")[0]
                    print(f"  {short_url}:")
                    for k, v in interesting.items():
                        print(f"    {k}: {v[:120]}")

    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(_main())
