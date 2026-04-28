"""Diagnose Airhost's CSRF auth: dump cookies + try various header names."""

from __future__ import annotations

import asyncio
import json
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
            # 1. Dump all cookies for the airhost domain.
            cookies = await page.context.cookies()
            print("=== cookies ===")
            for c in cookies:
                # mask values
                v = c["value"]
                masked = v[:8] + "…" + v[-6:] if len(v) > 16 else "***"
                print(f"  {c['name']:30s} domain={c['domain']:25s} value={masked}")

            # 2. Trigger 401 to grab a fresh csrf_token.
            url = (
                "https://api2.airhost.co/api/one/pms/"
                "booking_calendar/bookings/query?locale=ja"
            )
            body = {
                "start_date": "2026-05-15",
                "end_date": "2026-05-15",
                "house_id": "46349408-127c-4401-ae23-28b10b61ce15",
                "house_tags": [],
                "room_unit_ids": [],
            }
            r0 = await page.request.post(url, data=body)
            err = await r0.json()
            token = err.get("data", {}).get("csrf_token")
            print(f"\n=== 401 response delivered csrf_token (len={len(token or '')}) ===")

            # 3. Try a series of header name variants with the new token.
            variants = [
                {"X-CSRF-Token": token},
                {"X-Csrf-Token": token},
                {"X-XSRF-Token": token},
                {"X-Authenticity-Token": token},
                {"Authenticity-Token": token},
                {"X-Csrf": token},
            ]
            for headers in variants:
                r = await page.request.post(url, data=body, headers=headers)
                key = list(headers.keys())[0]
                print(f"  header={key:25s} → HTTP {r.status}")
                if r.ok:
                    print(f"    ✅ that one worked")
                    break

            # 4. Also try: csrf_token in body.
            for body_with_token in [
                {**body, "csrf_token": token},
                {**body, "authenticity_token": token},
                {**body, "_csrf": token},
            ]:
                r = await page.request.post(url, data=body_with_token)
                key = next(k for k in body_with_token if "csrf" in k or "token" in k or "_csrf" == k)
                print(f"  body field={key:25s} → HTTP {r.status}")
                if r.ok:
                    print("    ✅ body field works")
                    break

    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(_main())
