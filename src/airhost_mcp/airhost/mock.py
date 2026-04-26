"""Deterministic in-memory mock so the MCP can be wired up before scraping is built.

Behavior is stable across calls within a process: the listings are fixed,
and reservations are seeded by hashing (listing_id, date) so the same date
always returns the same fake reservation. This keeps testing predictable.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import date, timedelta

from .base import (
    AirhostClient,
    Availability,
    BlockResult,
    Listing,
    Reservation,
    ReservationUpdate,
)

_FIXED_LISTINGS: list[Listing] = [
    Listing(
        listing_id="lst_001",
        name="Shibuya Sky Loft",
        address="Shibuya, Tokyo",
        bedrooms=1,
        max_guests=3,
        nightly_rate_jpy=18000,
    ),
    Listing(
        listing_id="lst_002",
        name="Asakusa Riverside Annex",
        address="Asakusa, Tokyo",
        bedrooms=2,
        max_guests=5,
        nightly_rate_jpy=22500,
    ),
    Listing(
        listing_id="lst_003",
        name="Kyoto Machiya Stay",
        address="Higashiyama, Kyoto",
        bedrooms=2,
        max_guests=4,
        nightly_rate_jpy=26000,
    ),
]


def _seed(listing_id: str, target_date: date) -> int:
    h = hashlib.sha256(f"{listing_id}:{target_date.isoformat()}".encode()).hexdigest()
    return int(h[:8], 16)


class MockAirhostClient(AirhostClient):
    def __init__(self) -> None:
        # Mutable layer over the fixed seed so block/update calls "stick"
        # for the lifetime of the process. ``_observed`` mirrors how the real
        # client sees reservations: an id is updatable only after it has been
        # surfaced by a fetch call (list/get).
        self._blocks: dict[tuple[str, date], BlockResult] = {}
        self._observed: dict[str, Reservation] = {}
        self._lock = asyncio.Lock()

    def _observe(self, reservation: Reservation) -> Reservation:
        existing = self._observed.get(reservation.reservation_id)
        if existing is not None:
            return existing
        self._observed[reservation.reservation_id] = reservation
        return reservation

    async def list_listings(self) -> list[Listing]:
        return list(_FIXED_LISTINGS)

    async def get_availability(self, listing_id: str, target_date: date) -> Availability:
        if (listing_id, target_date) in self._blocks:
            return Availability(
                listing_id=listing_id,
                target_date=target_date,
                available=False,
                note="blocked",
            )
        listing = next((l for l in _FIXED_LISTINGS if l.listing_id == listing_id), None)
        if listing is None:
            raise ValueError(f"unknown listing_id: {listing_id}")
        # 70%-ish availability, deterministic on (listing, date).
        seed = _seed(listing_id, target_date)
        return Availability(
            listing_id=listing_id,
            target_date=target_date,
            available=(seed % 10) >= 3,
            nightly_rate_jpy=listing.nightly_rate_jpy,
        )

    def _seeded_reservation(self, listing_id: str, target_date: date) -> Reservation | None:
        seed = _seed(listing_id, target_date)
        # Pretend roughly 30% of dates have a reservation.
        if seed % 10 >= 3:
            return None
        nights = 1 + (seed % 4)
        guests = 1 + (seed % 4)
        listing = next((l for l in _FIXED_LISTINGS if l.listing_id == listing_id), None)
        nightly = listing.nightly_rate_jpy if listing else 20000
        rid = f"res_{listing_id}_{target_date.isoformat()}"
        return Reservation(
            reservation_id=rid,
            listing_id=listing_id,
            guest_name=f"Guest #{seed % 1000:03d}",
            check_in=target_date,
            check_out=target_date + timedelta(days=nights),
            nights=nights,
            guests=guests,
            total_jpy=(nightly or 0) * nights,
            status="confirmed",
            channel=["airbnb", "booking", "direct"][seed % 3],
        )

    async def get_reservations_on(
        self, listing_id: str, target_date: date
    ) -> list[Reservation]:
        if (listing_id, target_date) in self._blocks:
            return []
        res = self._seeded_reservation(listing_id, target_date)
        if res is None:
            return []
        return [self._observe(res)]

    async def block_date(
        self, listing_id: str, target_date: date, reason: str | None = None
    ) -> BlockResult:
        async with self._lock:
            result = BlockResult(
                listing_id=listing_id, target_date=target_date, blocked=True, reason=reason
            )
            self._blocks[(listing_id, target_date)] = result
            return result

    async def update_reservation(
        self, reservation_id: str, patch: ReservationUpdate
    ) -> Reservation:
        async with self._lock:
            existing = self._observed.get(reservation_id)
            if existing is None:
                raise ValueError(
                    f"unknown reservation_id: {reservation_id} "
                    "(fetch it via list/get first to make it visible to the mock)"
                )
            updated = existing.model_copy(update=patch.as_patch())
            if patch.check_in or patch.check_out:
                updated = updated.model_copy(
                    update={"nights": (updated.check_out - updated.check_in).days}
                )
            self._observed[reservation_id] = updated
            return updated

    async def list_reservations_in_range(
        self,
        listing_id: str | None,
        start_date: date,
        end_date: date,
    ) -> list[Reservation]:
        if end_date < start_date:
            raise ValueError("end_date must be >= start_date")
        targets = (
            [listing_id]
            if listing_id
            else [l.listing_id for l in _FIXED_LISTINGS]
        )
        out: list[Reservation] = []
        cur = start_date
        while cur <= end_date:
            for lid in targets:
                if (lid, cur) in self._blocks:
                    continue
                res = self._seeded_reservation(lid, cur)
                if res is None:
                    continue
                out.append(self._observe(res))
            cur += timedelta(days=1)
        return out
