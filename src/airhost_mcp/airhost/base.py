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


# Airhost's data hierarchy is House > RoomType > RoomUnit. We surface the full
# tree to the MCP client so it can reason about cross-room moves within the
# same building (e.g. fitting a 1/1-1/3 stay across rooms 101 and 102 when
# neither has the full window free on its own).
class RoomUnit(BaseModel):
    room_unit_id: str
    room_no: str  # display label, e.g. "101"


class RoomType(BaseModel):
    room_type_id: str
    name: str
    occupancy: int | None = None  # max_guests for this room type
    bedrooms: int | None = None
    bathrooms: float | None = None
    nightly_rate_jpy: int | None = None  # min_price
    cleaning_fee_jpy: int | None = None
    room_units: list[RoomUnit] = Field(default_factory=list)


class Listing(BaseModel):
    """A House (building). Aggregates one or more RoomTypes."""

    listing_id: str  # = house_id
    name: str
    address: str | None = None
    property_type: str | None = None  # e.g. "apartment", "hotel"
    timezone: str = "Asia/Tokyo"
    checkin_at: str | None = None
    checkout_at: str | None = None
    room_types: list[RoomType] = Field(default_factory=list)


class RoomTypeAvailability(BaseModel):
    """Per-RoomType slice of availability on a single date.

    ``total_units`` and ``available_units`` make cross-room moves visible:
    e.g. for a 3-night stay we can see "101 has 1 free unit on day 1, 103
    has 1 free unit on day 2, both empty on day 3" and route the guest.
    """

    room_type_id: str
    name: str
    total_units: int
    available_units: int
    nightly_rate_jpy: int | None = None


class Availability(BaseModel):
    listing_id: str  # = house_id
    target_date: date
    available: bool  # True iff any room_type has available_units > 0
    nightly_rate_jpy: int | None = None  # cheapest available room type's rate
    note: str | None = None
    room_types: list[RoomTypeAvailability] = Field(default_factory=list)


class Reservation(BaseModel):
    reservation_id: str
    listing_id: str  # = house_id
    # The hierarchy below the building. Set when known (always set when the
    # data comes from the Airhost booking-calendar API).
    room_type_id: str | None = None
    room_unit_id: str | None = None
    # The channel-side identifier — e.g. Airbnb's "HMxxxx", Booking.com's
    # numeric ref, jalan's UID. Useful for cross-referencing OTAs.
    external_uid: str | None = None
    guest_name: str
    check_in: date
    check_out: date
    nights: int
    guests: int = 1
    total_jpy: int | None = None
    status: ReservationStatus = "confirmed"
    channel: str | None = None  # e.g. "Airbnb", "Booking.com", "じゃらん"
    notes: str | None = None  # populated from block_reason for blocked entries


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
