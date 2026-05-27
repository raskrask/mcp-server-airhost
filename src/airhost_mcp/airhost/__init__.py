"""Airhost client. Defaults to a deterministic mock; real HTTP client TBD."""

from .base import (
    AirhostClient,
    Availability,
    BlockResult,
    GuestIdPhoto,
    GuestRegistrant,
    GuestRegistration,
    Listing,
    Reservation,
    ReservationUpdate,
    RoomType,
    RoomTypeAvailability,
    RoomUnit,
)
from .factory import build_airhost_client

__all__ = [
    "AirhostClient",
    "Availability",
    "BlockResult",
    "GuestIdPhoto",
    "GuestRegistrant",
    "GuestRegistration",
    "Listing",
    "Reservation",
    "ReservationUpdate",
    "RoomType",
    "RoomTypeAvailability",
    "RoomUnit",
    "build_airhost_client",
]
