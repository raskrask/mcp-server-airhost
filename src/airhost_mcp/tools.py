"""MCP tool definitions. Each tool is a thin async wrapper over AirhostClient."""

from __future__ import annotations

from datetime import date

from mcp.server.fastmcp import FastMCP

from .airhost import AirhostClient, ReservationUpdate


def register_tools(mcp: FastMCP, client: AirhostClient) -> None:
    """Attach the 6 Airhost tools to the given FastMCP instance."""

    @mcp.tool()
    async def list_listings() -> list[dict]:
        """List every Airhost listing the authenticated account can manage."""
        listings = await client.list_listings()
        return [l.model_dump(mode="json") for l in listings]

    @mcp.tool()
    async def get_availability(listing_id: str, target_date: date) -> dict:
        """Return the availability + nightly rate for a single listing on a single date."""
        result = await client.get_availability(listing_id, target_date)
        return result.model_dump(mode="json")

    @mcp.tool()
    async def get_reservations_on(listing_id: str, target_date: date) -> list[dict]:
        """Return any reservations occupying the given listing on ``target_date``."""
        results = await client.get_reservations_on(listing_id, target_date)
        return [r.model_dump(mode="json") for r in results]

    @mcp.tool()
    async def block_date(
        listing_id: str, target_date: date, reason: str | None = None
    ) -> dict:
        """Block a single date on a listing (mark unavailable). Optionally record a reason."""
        result = await client.block_date(listing_id, target_date, reason)
        return result.model_dump(mode="json")

    @mcp.tool()
    async def update_reservation(
        reservation_id: str,
        guest_name: str | None = None,
        check_in: date | None = None,
        check_out: date | None = None,
        guests: int | None = None,
        total_jpy: int | None = None,
        status: str | None = None,
        notes: str | None = None,
    ) -> dict:
        """Patch fields on an existing reservation. Only provided fields change."""
        patch = ReservationUpdate(
            guest_name=guest_name,
            check_in=check_in,
            check_out=check_out,
            guests=guests,
            total_jpy=total_jpy,
            status=status,  # type: ignore[arg-type]
            notes=notes,
        )
        result = await client.update_reservation(reservation_id, patch)
        return result.model_dump(mode="json")

    @mcp.tool()
    async def list_reservations_in_range(
        start_date: date,
        end_date: date,
        listing_id: str | None = None,
    ) -> list[dict]:
        """List reservations in a date range, optionally filtered by listing.

        Useful for revenue / occupancy analysis. ``end_date`` is inclusive.
        """
        results = await client.list_reservations_in_range(listing_id, start_date, end_date)
        return [r.model_dump(mode="json") for r in results]
