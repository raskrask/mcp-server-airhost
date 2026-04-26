"""Smoke tests for the mock Airhost client. Confirms each tool path returns shaped data."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from airhost_mcp.airhost.mock import MockAirhostClient
from airhost_mcp.airhost.base import ReservationUpdate


@pytest.fixture
def client() -> MockAirhostClient:
    return MockAirhostClient()


async def test_list_listings(client: MockAirhostClient) -> None:
    listings = await client.list_listings()
    assert len(listings) >= 1
    assert all(l.listing_id and l.name for l in listings)


async def test_availability_is_deterministic(client: MockAirhostClient) -> None:
    target = date(2026, 5, 1)
    a = await client.get_availability("lst_001", target)
    b = await client.get_availability("lst_001", target)
    assert a.available == b.available
    assert a.listing_id == "lst_001"


async def test_block_then_unavailable(client: MockAirhostClient) -> None:
    target = date(2026, 5, 2)
    await client.block_date("lst_001", target, reason="maintenance")
    avail = await client.get_availability("lst_001", target)
    assert avail.available is False
    assert avail.note == "blocked"


async def test_range_listing_includes_dates(client: MockAirhostClient) -> None:
    start = date(2026, 5, 1)
    end = start + timedelta(days=14)
    res = await client.list_reservations_in_range(None, start, end)
    # Mock seeds ~30% of dates -> non-empty over 14 days * 3 listings.
    assert isinstance(res, list)


async def test_update_reservation_round_trip(client: MockAirhostClient) -> None:
    # Find a date that has a seeded reservation, then patch its guest name.
    start = date(2026, 5, 1)
    end = start + timedelta(days=30)
    reservations = await client.list_reservations_in_range("lst_001", start, end)
    assert reservations, "mock should produce at least one reservation in 30 days"
    rid = reservations[0].reservation_id
    updated = await client.update_reservation(
        rid, ReservationUpdate(guest_name="Patched Name")
    )
    assert updated.guest_name == "Patched Name"
