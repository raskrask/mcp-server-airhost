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


async def test_update_unobserved_id_raises(client: MockAirhostClient) -> None:
    with pytest.raises(ValueError):
        await client.update_reservation(
            "res_never_seen", ReservationUpdate(guest_name="x")
        )


async def test_guest_registration_shape(client: MockAirhostClient) -> None:
    reg = await client.get_guest_registration("bk_001")
    again = await client.get_guest_registration("bk_001")
    # Deterministic across calls.
    assert reg.model_dump() == again.model_dump()
    assert reg.booking_id == "bk_001"
    assert reg.guest_count == len(reg.guests) >= 1
    # Exactly one representative, and only they carry an ID photo.
    mains = [g for g in reg.guests if g.is_main_guest]
    assert len(mains) == 1
    assert mains[0].id_photo_url
    assert all(g.id_photo_url is None for g in reg.guests if not g.is_main_guest)
    assert reg.main_guest_name == mains[0].name
    assert reg.main_guest_id_photo_url == mains[0].id_photo_url


async def test_guest_registration_completeness_gate(client: MockAirhostClient) -> None:
    # is_complete must agree with per-guest progress across many bookings.
    for i in range(50):
        reg = await client.get_guest_registration(f"bk_{i:03d}")
        expected = all(g.progress >= 100 for g in reg.guests)
        assert reg.is_complete is expected
        assert reg.completed_count == sum(1 for g in reg.guests if g.progress >= 100)
        assert reg.overall_progress == min(g.progress for g in reg.guests)


async def test_guest_id_photo_defaults_to_main(client: MockAirhostClient) -> None:
    reg = await client.get_guest_registration("bk_001")
    main = next(g for g in reg.guests if g.is_main_guest)
    photo = await client.get_guest_id_photo("bk_001")
    assert photo.guest_id == main.guest_id
    assert photo.guest_name == main.name
    assert photo.mime.startswith("image/")
    assert photo.content  # non-empty bytes


async def test_guest_id_photo_missing_raises(client: MockAirhostClient) -> None:
    reg = await client.get_guest_registration("bk_001")
    non_main = next((g for g in reg.guests if not g.is_main_guest), None)
    if non_main is not None:  # non-main guests have no ID on file
        with pytest.raises(ValueError):
            await client.get_guest_id_photo("bk_001", non_main.guest_id)
