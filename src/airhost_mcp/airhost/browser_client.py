"""Playwright-based Airhost client.

Airhost is a browser-only console (heavy JS, anti-scraping), so we drive a
real Chromium via Playwright. Sessions are persisted as Playwright
``storage_state`` (cookies + localStorage) through the configured
``SessionStore`` so a freshly-started Cloud Run container can resume without
re-doing the password + email-MFA dance.

Once logged in, we don't actually scrape the DOM for most operations —
Airhost's PMS frontend speaks to ``api2.airhost.co/api/one/...`` JSON
endpoints, and Playwright's ``page.request`` reuses the cookie/session jar
from the browser context, so we can call those APIs directly. That's much
more stable than HTML scraping and avoids the React rerender timing fights.
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
    RoomType,
    RoomUnit,
)

logger = logging.getLogger(__name__)


def _safe_int(value: Any) -> int | None:
    """Coerce values that Airhost sometimes returns as strings or floats.

    Examples seen in the wild: ``"1"`` (str), ``1.0`` (float), ``""`` (empty
    string), ``None``. We accept those and only return ints we can trust.
    """
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
        """Run the password + email-MFA login flow against Airhost PMS.

        Airhost's sign-in page (https://pms.airhost.co/ja/sign_in) renders
        the OTP input alongside the email/password fields from first paint;
        the OTP field is just disabled until you submit credentials. So the
        flow is:

          1. Fill ``[data-testid=email]`` and ``[data-testid=password]``.
          2. Capture ``since`` BEFORE clicking submit, so the Gmail poller
             only matches MFA mails sent after this attempt.
          3. Click ``[data-testid=login]``.
          4. Wait for the OTP input to become enabled (it stays in the DOM
             but ``disabled`` toggles between modes).
          5. Fetch the code via the configured MFA strategy.
          6. Fill ``[data-testid=otpCode]``, click submit again.
          7. Wait for navigation away from the sign-in page.
        """
        async with self._login_lock:
            await page.goto(self._login_url, wait_until="domcontentloaded")
            await page.fill('[data-testid="email"]', self._username)
            await page.fill('[data-testid="password"]', self._password)

            since = time.time()
            await page.click('[data-testid="login"]')

            # OTP input is always present in the DOM; we wait for the
            # *enabled* state to avoid filling it before the credentials
            # round-trip completes.
            otp = page.locator('[data-testid="otpCode"]')
            await otp.wait_for(state="visible", timeout=15000)
            # ant-design disables the field via the `disabled` attribute.
            await page.wait_for_function(
                "() => { const el = document.querySelector('[data-testid=\"otpCode\"]'); "
                "return el && !el.disabled; }",
                timeout=15000,
            )

            try:
                code = await self._mfa.fetch_code(
                    since_epoch=since, timeout_seconds=self._mfa_timeout
                )
            except MFATimeoutError:
                logger.error("MFA code did not arrive within %ss", self._mfa_timeout)
                raise

            await otp.fill(code)
            await page.click('[data-testid="login"]')

            # Successful login navigates away from /sign_in. Failures stay
            # on the same URL and surface an ant-design error toast.
            try:
                await page.wait_for_url(
                    lambda url: "/sign_in" not in url, timeout=20000
                )
            except Exception as exc:
                # Look for an error message on the page to give a useful log.
                err = page.locator(".ant-message-error, .ant-form-item-explain-error").first
                detail = await err.text_content() if await err.count() > 0 else None
                raise RuntimeError(
                    f"Airhost login did not navigate away from sign_in (msg={detail!r})"
                ) from exc

            logger.info("airhost login completed for %s", self._username)

    # ----- tool methods -----

    _API_BASE = "https://api2.airhost.co/api/one"

    async def list_listings(self) -> list[Listing]:
        """Fetch every house with its room types nested.

        Two-step fetch (Airhost has no combined endpoint):

        1. ``GET /pms/houses`` — buildings the account manages.
        2. For each house, ``GET /pms/room_types?house_id=...`` — room types
           and their room units.

        Concurrent fetches per-house keep total latency down on accounts
        with many properties. Errors on a single house's room_types fetch
        are logged but don't abort the whole listing — the house is
        returned with an empty ``room_types`` list and the operator can
        retry.
        """
        async with self._page() as page:
            houses_url = (
                f"{self._API_BASE}/pms/houses"
                "?locale=ja&sorts=created_at%3Adesc"
                "&page_num=1&page_size=200"
                "&field_sets_house=tag_list"
            )
            h_resp = await page.request.get(houses_url)
            if not h_resp.ok:
                raise RuntimeError(
                    f"Airhost houses API returned HTTP {h_resp.status}: "
                    f"{(await h_resp.text())[:300]}"
                )
            h_payload = await h_resp.json()
            if not h_payload.get("success"):
                raise RuntimeError(f"Airhost houses API failed: {h_payload}")

            houses = h_payload.get("data", []) or []
            if not houses:
                return []

            # Fetch room_types for each house in parallel.
            room_types_per_house = await asyncio.gather(
                *[self._fetch_room_types(page, h["id"]) for h in houses],
                return_exceptions=True,
            )

            listings: list[Listing] = []
            for house, rts_or_exc in zip(houses, room_types_per_house, strict=True):
                if isinstance(rts_or_exc, BaseException):
                    logger.warning(
                        "room_types fetch failed for house %s: %s",
                        house.get("id"),
                        rts_or_exc,
                    )
                    room_types: list[RoomType] = []
                else:
                    room_types = rts_or_exc

                listings.append(
                    Listing(
                        listing_id=house["id"],
                        name=house.get("internal_name") or house.get("name", ""),
                        address=house.get("address"),
                        property_type=house.get("property_type"),
                        timezone="Asia/Tokyo",
                        checkin_at=house.get("checkin_at"),
                        checkout_at=house.get("checkout_at"),
                        room_types=room_types,
                    )
                )
            return listings

    async def _fetch_room_types(self, page: Page, house_id: str) -> list[RoomType]:
        """Helper: fetch room types (with units) for one house."""
        url = (
            f"{self._API_BASE}/pms/room_types"
            f"?locale=ja&house_id={house_id}"
            "&field_sets_room_type=basic%2Crich"
            "&page_size=200&page_num=1"
        )
        resp = await page.request.get(url)
        if not resp.ok:
            raise RuntimeError(
                f"room_types API HTTP {resp.status}: {(await resp.text())[:300]}"
            )
        payload = await resp.json()
        if not payload.get("success"):
            raise RuntimeError(f"room_types API failed: {payload}")

        out: list[RoomType] = []
        for item in payload.get("data", []) or []:
            settings = item.get("room_settings") or {}
            out.append(
                RoomType(
                    room_type_id=item["id"],
                    name=item.get("name", ""),
                    occupancy=settings.get("occupancy"),
                    bedrooms=_safe_int(settings.get("bedrooms")),
                    bathrooms=_safe_float(settings.get("bathrooms")),
                    nightly_rate_jpy=_safe_int(item.get("min_price")),
                    cleaning_fee_jpy=_safe_int(settings.get("cleaning_fee_to_guest")),
                    room_units=[
                        RoomUnit(
                            room_unit_id=u["id"],
                            room_no=u.get("display_room_no") or u.get("room_no", ""),
                        )
                        for u in (item.get("room_units") or [])
                    ],
                )
            )
        return out

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
