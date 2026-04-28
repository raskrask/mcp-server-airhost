"""Find where Airhost stores the x-ah-tenant id used in API calls."""

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
            # Visit the dashboard so any session-bootstrap API calls fire.
            await page.goto(
                "https://pms.airhost.co/ja/dashboard", wait_until="networkidle"
            )

            print("=== localStorage ===")
            ls = await page.evaluate(
                """() => {
                    const out = {};
                    for (let i = 0; i < localStorage.length; i++) {
                        const k = localStorage.key(i);
                        out[k] = localStorage.getItem(k);
                    }
                    return out;
                }"""
            )
            for k, v in ls.items():
                preview = v if len(v) < 120 else v[:80] + f"... ({len(v)} chars)"
                print(f"  {k}: {preview}")

            print("\n=== sessionStorage ===")
            ss = await page.evaluate(
                """() => {
                    const out = {};
                    for (let i = 0; i < sessionStorage.length; i++) {
                        const k = sessionStorage.key(i);
                        out[k] = sessionStorage.getItem(k);
                    }
                    return out;
                }"""
            )
            for k, v in ss.items():
                preview = v if len(v) < 120 else v[:80] + f"... ({len(v)} chars)"
                print(f"  {k}: {preview}")

            # Hunt for the tenant id pattern across whatever stores it.
            print("\n=== searching for tenant-id-like patterns ===")
            target_patterns = ["tenant", "ah-tenant", "account_id"]
            for store_name, store in [("localStorage", ls), ("sessionStorage", ss)]:
                for k, v in store.items():
                    if any(p in k.lower() for p in target_patterns):
                        print(f"  ⭐ {store_name}[{k}] = {v[:200]}")
                    elif any(p in (v or "").lower() for p in target_patterns):
                        print(f"  candidate {store_name}[{k}] contains 'tenant'")

            # Try common current-user endpoints to see if tenant id leaks in
            # an API response.
            print("\n=== probing user/account endpoints ===")
            candidates = [
                "https://api2.airhost.co/api/one/pms/users/me?locale=ja",
                "https://api2.airhost.co/api/one/users/me?locale=ja",
                "https://api2.airhost.co/api/one/pms/account?locale=ja",
                "https://api2.airhost.co/api/one/pms/accounts/current?locale=ja",
                "https://api2.airhost.co/api/one/pms/tenants/current?locale=ja",
                "https://api2.airhost.co/api/one/pms/me?locale=ja",
            ]
            for url in candidates:
                resp = await page.request.get(url)
                short = url.split("/api/one/")[-1]
                if resp.ok:
                    try:
                        body = await resp.json()
                        text = json.dumps(body)[:300]
                    except Exception:
                        text = (await resp.text())[:300]
                    print(f"  ✅ 200 {short}: {text}")
                else:
                    print(f"  ❌ {resp.status} {short}")

    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(_main())
