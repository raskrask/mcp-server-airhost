"""Smoke test for the BrowserAirhostClient login flow only.

Drives the Airhost sign-in page end-to-end:
  1. Builds an Airhost browser client from .env settings.
  2. Goes through password + Gmail-MFA login.
  3. On success, prints the post-login URL and persists session_state via
     the configured SessionStore.

Run from the repo root:
    .venv/bin/python -m scripts.login_smoke

The first run will pop a Google OAuth consent window for Gmail readonly
access; subsequent runs reuse gmail_token.json.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from airhost_mcp.airhost.browser_client import BrowserAirhostClient
from airhost_mcp.config import get_settings
from airhost_mcp.mfa import build_mfa_strategy
from airhost_mcp.session import build_session_store


async def _main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )

    settings = get_settings()
    if settings.airhost_client != "browser":
        print("AIRHOST_CLIENT must be 'browser' for this smoke test.", file=sys.stderr)
        return 2
    if not settings.airhost_username or not settings.airhost_password:
        print("AIRHOST_USERNAME / AIRHOST_PASSWORD must be set in .env", file=sys.stderr)
        return 2

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
        # _page() drives ensure_browser + login + storage_state persist.
        # We don't care about any tool call — just that login succeeds.
        async with client._page() as page:
            print(f"\n✅ Logged in. Current URL: {page.url}")
            title = await page.title()
            print(f"   Page title: {title}")
        print("✅ Session persisted via SessionStore.")
        return 0
    except Exception as exc:
        print(f"\n❌ Login failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        await client.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
