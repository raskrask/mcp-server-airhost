"""Domain models + Airhost client contract.

Concrete implementations (mock today, real HTTP/scraper later) implement
``AirhostClient``. Tools call the client, never the implementation directly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

ReservationStatus = Literal["confirmed", "cancelled", "blocked", "pending"]


class Listing(BaseModel):
    listing_id: str
    name: str
    address: str | None = None
    bedrooms: int | None = None
    max_guests: int | None = None
    nightly_rate_jpy: int | None = None
    timezone: str = "Asia/Tokyo"


class Availability(BaseModel):
    listing_id: str
    target_date: date
    available: bool
    nightly_rate_jpy: int | None = None
    note: str | None = None


class Reservation(BaseModel):
    reservation_id: str
    listing_id: str
    guest_name: str
    check_in: date
    check_out: date
    nights: int
    guests: int = 1
    total_jpy: int | None = None
    status: ReservationStatus = "confirmed"
    channel: str | None = None
    notes: str | None = None


class BlockResult(BaseModel):
    listing_id: str
    target_date: date
    blocked: bool
    reason: str | None = None


class ReservationUpdate(BaseModel):
    """Patch payload for an existing reservation. Only set fields are applied."""

    guest_name: str | None = None
    check_in: date | None = None
    check_out: date | None = None
    guests: int | None = None
    total_jpy: int | None = None
    status: ReservationStatus | None = None
    notes: str | None = None

    def as_patch(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class ListingsResult(BaseModel):
    listings: list[Listing] = Field(default_factory=list)


class AirhostClient(ABC):
    """All Airhost interactions go through this interface."""

    @abstractmethod
    async def list_listings(self) -> list[Listing]: ...

    @abstractmethod
    async def get_availability(
        self, listing_id: str, target_date: date
    ) -> Availability: ...

    @abstractmethod
    async def get_reservations_on(
        self, listing_id: str, target_date: date
    ) -> list[Reservation]: ...

    @abstractmethod
    async def block_date(
        self, listing_id: str, target_date: date, reason: str | None = None
    ) -> BlockResult: ...

    @abstractmethod
    async def update_reservation(
        self, reservation_id: str, patch: ReservationUpdate
    ) -> Reservation: ...

    @abstractmethod
    async def list_reservations_in_range(
        self,
        listing_id: str | None,
        start_date: date,
        end_date: date,
    ) -> list[Reservation]: ...
