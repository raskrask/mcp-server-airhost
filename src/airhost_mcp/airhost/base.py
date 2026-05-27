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
    nightly_rate_jpy: int | None = None  # 基本料金 (date-specific, seasonal)
    extra_guest_price_jpy: int | None = None  # 人数料金: charged per guest above guests_included
    guests_included: int | None = None  # guests covered by nightly_rate_jpy


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
    amount_due_jpy: int | None = None  # outstanding balance (booking - paid)
    payment_status: str | None = None  # e.g. "paid", "balance_due"
    rate_plan_name: str | None = None  # e.g. "素泊まりプラン", "朝食付きプラン"
    ota_commission_jpy: int | None = None  # OTA commission fee (手数料) in JPY
    status: ReservationStatus = "confirmed"
    channel: str | None = None  # e.g. "Airbnb", "Booking.com", "じゃらん"
    notes: str | None = None  # populated from block_reason for blocked entries


class FolioTransaction(BaseModel):
    transaction_id: str
    type: str  # "invoice_item" | "payment"
    description: str
    debit: float  # charge in JPY (0 for payments)
    credit: float  # payment in JPY (0 for charges)
    display_date: date | None = None
    state: str | None = None  # payments: "completed" etc.
    order_id: str | None = None


class Folio(BaseModel):
    folio_id: str
    booking_id: str
    title: str | None = None
    total_debit: float
    total_credit: float
    balance: float
    currency: str = "JPY"
    closed: bool = False
    transactions: list[FolioTransaction] = Field(default_factory=list)


class GuestRegistrant(BaseModel):
    """One guest's online check-in (宿泊者名簿) entry.

    Holds personal data. ``progress`` is Airhost's own 0–100 completion for
    this guest. The ID document image (本人確認書類) is only collected from
    the representative (``is_main_guest``), so ``id_photo_url`` is None for
    the others. ``id_photo_url`` points at an authenticated Airhost blob —
    fetching it needs the logged-in session, so the URL is not usable by an
    unauthenticated client.
    """

    guest_id: str
    name: str
    is_main_guest: bool = False
    progress: int = 0  # 0–100, Airhost-computed
    resident_status: str | None = None  # "local" | "foreign"
    checkin_status: str | None = None  # e.g. "before_checkin"
    nationality: str | None = None
    id_photo_url: str | None = None  # representative only; needs auth to fetch


class GuestRegistration(BaseModel):
    """Booking-level rollup of guest online check-in status.

    ``is_complete`` is the gate for "all guest info submitted" (every guest
    at progress 100) — the signal used to decide key handover. Holds personal
    data via ``guests`` and ``main_guest_*``.
    """

    booking_id: str
    guest_count: int
    completed_count: int
    is_complete: bool
    overall_progress: int  # min progress across guests (0 when no guests)
    main_guest_name: str | None = None
    main_guest_id_photo_url: str | None = None
    guests: list[GuestRegistrant] = Field(default_factory=list)


class GuestIdPhoto(BaseModel):
    """Raw bytes of a guest's ID document image (本人確認書類).

    Returned by ``get_guest_id_photo`` so the image can be handed to a vision
    model. The bytes are personal data — do not persist them. ``content`` is
    the raw blob; ``mime`` is its Content-Type as reported by Airhost.
    """

    booking_id: str
    guest_id: str
    guest_name: str
    mime: str
    content: bytes


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

    @abstractmethod
    async def list_reservations_with_details(
        self,
        listing_id: str | None,
        start_date: date,
        end_date: date,
    ) -> list[Reservation]:
        """Like list_reservations_in_range but also populates ota_commission_jpy.

        Slower (requires CSV export over ActionCable). Call explicitly when
        OTA commission data is needed.
        """
        ...

    @abstractmethod
    async def get_folio(self, reservation_id: str) -> list[Folio]:
        """Return folio(s) for a reservation, including all transactions.

        Each transaction has a ``type`` of "invoice_item" (charges) or
        "payment" and a free-text ``description`` (e.g. "1 x Sauna② R971…").
        """
        ...

    @abstractmethod
    async def get_guest_registration(self, booking_id: str) -> GuestRegistration:
        """Return online check-in (宿泊者名簿) status for one booking.

        Includes each guest's completion progress and the representative's
        ID document image URL. Returns personal data.
        """
        ...

    @abstractmethod
    async def get_guest_id_photo(
        self, booking_id: str, guest_id: str | None = None
    ) -> GuestIdPhoto:
        """Download a guest's ID document image (本人確認書類) as raw bytes.

        Resolves the photo URL from the booking's guest forms and fetches the
        authenticated Airhost blob. ``guest_id`` defaults to the representative
        (main guest), who is normally the only one with an ID on file.
        Returns personal data.
        """
        ...
