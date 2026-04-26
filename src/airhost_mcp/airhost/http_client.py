"""HTTP-based Airhost client (skeleton).

This is the seam where the real Airhost integration goes once the actual
endpoints / page structure is known. For now it raises ``NotImplementedError``
so that the server fails loudly if ``AIRHOST_CLIENT=http`` is selected before
the implementation lands.

The class accepts a ``SessionStore`` and an ``MFAStrategy`` so the login flow
can be implemented without restructuring later.
"""

from __future__ import annotations

import logging
from datetime import date

from ..mfa import MFAStrategy
from ..session import SessionStore
from .base import (
    AirhostClient,
    Availability,
    BlockResult,
    Listing,
    Reservation,
    ReservationUpdate,
)

logger = logging.getLogger(__name__)


class HTTPAirhostClient(AirhostClient):
    def __init__(
        self,
        *,
        login_url: str,
        username: str,
        password: str,
        session_store: SessionStore,
        mfa: MFAStrategy,
        mfa_timeout_seconds: int = 120,
    ) -> None:
        self._login_url = login_url
        self._username = username
        self._password = password
        self._session_store = session_store
        self._mfa = mfa
        self._mfa_timeout = mfa_timeout_seconds

    async def _ensure_session(self) -> None:
        # TODO: load session from store; if missing/expired, run login + MFA flow
        # using self._mfa.fetch_code(...) and persist back via self._session_store.
        raise NotImplementedError("HTTPAirhostClient login is not implemented yet")

    async def list_listings(self) -> list[Listing]:
        await self._ensure_session()
        raise NotImplementedError

    async def get_availability(self, listing_id: str, target_date: date) -> Availability:
        await self._ensure_session()
        raise NotImplementedError

    async def get_reservations_on(
        self, listing_id: str, target_date: date
    ) -> list[Reservation]:
        await self._ensure_session()
        raise NotImplementedError

    async def block_date(
        self, listing_id: str, target_date: date, reason: str | None = None
    ) -> BlockResult:
        await self._ensure_session()
        raise NotImplementedError

    async def update_reservation(
        self, reservation_id: str, patch: ReservationUpdate
    ) -> Reservation:
        await self._ensure_session()
        raise NotImplementedError

    async def list_reservations_in_range(
        self,
        listing_id: str | None,
        start_date: date,
        end_date: date,
    ) -> list[Reservation]:
        await self._ensure_session()
        raise NotImplementedError
