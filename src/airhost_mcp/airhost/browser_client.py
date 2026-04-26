"""Playwright-based Airhost client.

Airhost is a browser-only console (heavy JS, anti-scraping), so we drive a
real Chromium via Playwright. Sessions are persisted as Playwright
``storage_state`` (cookies + localStorage) through the configured
``SessionStore`` so a freshly-started Cloud Run container can resume without
re-doing the password + email-MFA dance.

Implementation status: scaffolding only. The login flow and the per-tool
selectors / endpoints depend on the actual Airhost UI and are filled in once
the real pages are inspected. Methods raise ``NotImplementedError`` until then.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import date
from typing import Any, AsyncIterator

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from ..mfa import MFAStrategy, MFATimeoutError
from ..session import SessionRecord, SessionStore
from .base import (
    AirhostClient,
    Availability,
    BlockResult,
    Listing,
    Reservation,
    ReservationUpdate,
)

logger = logging.getLogger(__name__)


class BrowserAirhostClient(AirhostClient):
    def __init__(
        self,
        *,
        login_url: str,
        username: str,
        password: str,
        session_store: SessionStore,
        mfa: MFAStrategy,
        mfa_timeout_seconds: int = 120,
        session_ttl_seconds: int = 3600,
        headless: bool = True,
    ) -> None:
        self._login_url = login_url
        self._username = username
        self._password = password
        self._session_store = session_store
        self._mfa = mfa
        self._mfa_timeout = mfa_timeout_seconds
        self._session_ttl = session_ttl_seconds
        self._headless = headless

        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._login_lock = asyncio.Lock()

    # ----- lifecycle -----

    async def _ensure_browser(self) -> Browser:
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self._headless,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        return self._browser

    async def aclose(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    # ----- session / login -----

    @asynccontextmanager
    async def _page(self) -> AsyncIterator[Page]:
        """Yield a logged-in page. Reuses persisted storage_state when fresh."""
        browser = await self._ensure_browser()
        record = await self._session_store.load(self._username)
        storage_state: dict[str, Any] | None = None
        if record and not record.is_expired():
            storage_state = record.state or None

        context: BrowserContext = await browser.new_context(storage_state=storage_state)
        try:
            page = await context.new_page()
            if storage_state is None:
                await self._login(page, context)
            yield page
            # Save updated storage_state on successful exit.
            await self._persist_session(context)
        finally:
            await context.close()

    async def _persist_session(self, context: BrowserContext) -> None:
        state = await context.storage_state()
        record = SessionRecord(
            state=state,
            expires_at=time.time() + self._session_ttl,
            meta={"username": self._username},
        )
        await self._session_store.save(self._username, record)

    async def _login(self, page: Page, context: BrowserContext) -> None:
        """Run the password + email-MFA login flow.

        Selectors are placeholders — replace once the real Airhost login page
        structure is known.
        """
        async with self._login_lock:
            since = time.time()
            await page.goto(self._login_url)
            # TODO: replace selectors once Airhost login DOM is inspected.
            await page.fill("input[name='email']", self._username)
            await page.fill("input[name='password']", self._password)
            await page.click("button[type='submit']")

            # Detect MFA prompt. Adjust selector + URL pattern to actual UI.
            await page.wait_for_load_state("networkidle")
            mfa_input = page.locator("input[name='mfa_code'], input[name='code']").first
            if await mfa_input.count() > 0:
                try:
                    code = await self._mfa.fetch_code(
                        since_epoch=since, timeout_seconds=self._mfa_timeout
                    )
                except MFATimeoutError:
                    raise
                await mfa_input.fill(code)
                await page.click("button[type='submit']")
                await page.wait_for_load_state("networkidle")

            # TODO: assert we landed on the dashboard, raise on auth failure.
            logger.info("airhost login completed for %s", self._username)

    # ----- tool methods (TBD against real UI) -----

    async def list_listings(self) -> list[Listing]:
        async with self._page() as page:  # noqa: F841 (page used by impl)
            raise NotImplementedError("scrape Airhost listings page")

    async def get_availability(self, listing_id: str, target_date: date) -> Availability:
        async with self._page() as page:  # noqa: F841
            raise NotImplementedError

    async def get_reservations_on(
        self, listing_id: str, target_date: date
    ) -> list[Reservation]:
        async with self._page() as page:  # noqa: F841
            raise NotImplementedError

    async def block_date(
        self, listing_id: str, target_date: date, reason: str | None = None
    ) -> BlockResult:
        async with self._page() as page:  # noqa: F841
            raise NotImplementedError

    async def update_reservation(
        self, reservation_id: str, patch: ReservationUpdate
    ) -> Reservation:
        async with self._page() as page:  # noqa: F841
            raise NotImplementedError

    async def list_reservations_in_range(
        self,
        listing_id: str | None,
        start_date: date,
        end_date: date,
    ) -> list[Reservation]:
        async with self._page() as page:  # noqa: F841
            raise NotImplementedError
