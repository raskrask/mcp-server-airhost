"""Find the real source of Airhost's x-csrf-token."""

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
            await page.goto(
                "https://pms.airhost.co/ja/dashboard", wait_until="networkidle"
            )

            # 1. meta tags
            print("=== meta tags ===")
            metas = await page.evaluate(
                "() => Array.from(document.querySelectorAll('meta')).map("
                "m => ({name: m.name || m.getAttribute('property'), content: m.content}))"
            )
            for m in metas:
                if m.get("name"):
                    print(f"  {m['name']}: {(m.get('content') or '')[:100]}")

            # 2. cookies, looking specifically for csrf/xsrf-related
            print("\n=== cookies (csrf/xsrf only) ===")
            cookies = await page.context.cookies()
            for c in cookies:
                n = c["name"].lower()
                if "csrf" in n or "xsrf" in n:
                    print(f"  {c['name']:40s} domain={c['domain']:25s} value={c['value'][:80]}")

            # 3. window global keys
            print("\n=== window globals containing csrf/xsrf/token ===")
            keys = await page.evaluate(
                "() => Object.keys(window).filter("
                "k => /csrf|xsrf|token/i.test(k))"
            )
            for k in keys[:50]:
                v = await page.evaluate(
                    f"() => {{const v = window[{k!r}]; "
                    "return typeof v === 'string' ? v : typeof v;}}"
                )
                v_short = v if isinstance(v, str) and len(v) < 120 else f"<{v}>"
                print(f"  window.{k}: {v_short}")

            # 4. Trigger an actual API call from inside the page (so the
            # frontend's own header injection fires) and capture its headers.
            print("\n=== headers used by an in-page fetch to /pms/account ===")
            captured = await page.evaluate(
                """async () => {
                    // Patch fetch briefly to capture the next request
                    const orig = window.fetch;
                    let captured_headers = null;
                    window.fetch = async function(input, init) {
                        if (typeof input === 'string' && input.includes('api2.airhost.co')) {
                            captured_headers = init?.headers || {};
                        }
                        return orig.apply(this, arguments);
                    };
                    // Force a known API call by reusing whatever the dashboard does
                    try {
                        await fetch('https://api2.airhost.co/api/one/pms/account?locale=ja', {
                            credentials: 'include',
                            headers: {'accept': 'application/json'}
                        });
                    } catch (e) {}
                    window.fetch = orig;
                    return captured_headers;
                }"""
            )
            print(f"  {captured}")

            # 5. Look at every script tag for inline state init.
            print("\n=== inline scripts containing 'csrf' ===")
            scripts = await page.evaluate(
                "() => Array.from(document.querySelectorAll('script')).map("
                "s => s.textContent || '').filter(t => /csrf|csrfToken/i.test(t)).map("
                "t => t.slice(0, 300))"
            )
            for s in scripts[:5]:
                print(f"  ...{s}")

    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(_main())
