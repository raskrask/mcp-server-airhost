"""MCP tool definitions. Each tool is a thin async wrapper over AirhostClient."""

from __future__ import annotations

import logging
import time
from datetime import date

from mcp.server.fastmcp import Context, FastMCP

from .airhost import AirhostClient, ReservationUpdate

logger = logging.getLogger(__name__)


def _audit(ctx: Context | None, tool: str, **kwargs: object) -> None:
    """Emit a structured audit log line: who called which tool with what args."""
    email = "unknown"
    if ctx is not None:
        try:
            req = ctx.request_context.request  # type: ignore[attr-defined]
            email = getattr(getattr(req, "state", None), "user_email", "unknown")
        except Exception:
            pass
    extra = " ".join(f"{k}={v!r}" for k, v in kwargs.items() if v is not None)
    logger.info(
        "AUDIT tool=%s user=%s ts=%.3f %s",
        tool,
        email,
        time.time(),
        extra,
    )


def register_tools(mcp: FastMCP, client: AirhostClient) -> None:
    """Attach the 6 Airhost tools to the given FastMCP instance."""

    @mcp.tool()
    async def list_listings(ctx: Context) -> list[dict]:
        """List every Airhost listing the authenticated account can manage."""
        _audit(ctx, "list_listings")
        listings = await client.list_listings()
        return [lst.model_dump(mode="json") for lst in listings]

    @mcp.tool()
    async def get_availability(ctx: Context, listing_id: str, target_date: date) -> dict:
        """Return the availability + nightly rate for a single listing on a single date."""
        _audit(ctx, "get_availability", listing_id=listing_id, target_date=target_date)
        result = await client.get_availability(listing_id, target_date)
        return result.model_dump(mode="json")

    @mcp.tool()
    async def get_reservations_on(
        ctx: Context, listing_id: str, target_date: date
    ) -> list[dict]:
        """Return any reservations occupying the given listing on ``target_date``."""
        _audit(ctx, "get_reservations_on", listing_id=listing_id, target_date=target_date)
        results = await client.get_reservations_on(listing_id, target_date)
        return [r.model_dump(mode="json") for r in results]

    @mcp.tool()
    async def block_date(
        ctx: Context, listing_id: str, target_date: date, reason: str | None = None
    ) -> dict:
        """Block a single date on a listing (mark unavailable). Optionally record a reason."""
        _audit(ctx, "block_date", listing_id=listing_id, target_date=target_date, reason=reason)
        result = await client.block_date(listing_id, target_date, reason)
        return result.model_dump(mode="json")

    @mcp.tool()
    async def update_reservation(
        ctx: Context,
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
        _audit(
            ctx,
            "update_reservation",
            reservation_id=reservation_id,
            guest_name=guest_name,
            check_in=check_in,
            check_out=check_out,
            guests=guests,
            status=status,
        )
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
        ctx: Context,
        start_date: date,
        end_date: date,
        listing_id: str | None = None,
    ) -> list[dict]:
        """List reservations in a date range, optionally filtered by listing.

        Returns basic reservation info (total, rate_plan_name, payment_status).
        For OTA commission (手数料) use list_reservation_details instead.
        ``end_date`` is inclusive.
        """
        _audit(
            ctx,
            "list_reservations_in_range",
            listing_id=listing_id,
            start_date=start_date,
            end_date=end_date,
        )
        results = await client.list_reservations_in_range(listing_id, start_date, end_date)
        return [r.model_dump(mode="json") for r in results]

    @mcp.tool()
    async def get_folio(ctx: Context, reservation_id: str) -> list[dict]:
        """Return the folio (明細) for a reservation: all charges and payments.

        Each item has ``type`` ("invoice_item" or "payment"), ``description``
        (free text, e.g. "1 x Sauna② R971630271"), ``debit`` (charge in JPY),
        and ``credit`` (payment in JPY). Use ``description`` to identify
        specific charges such as sauna fees or pet fees.
        """
        _audit(ctx, "get_folio", reservation_id=reservation_id)
        folios = await client.get_folio(reservation_id)
        return [f.model_dump(mode="json") for f in folios]

    @mcp.tool()
    async def list_reservation_details(
        ctx: Context,
        start_date: date,
        end_date: date,
        listing_id: str | None = None,
    ) -> list[dict]:
        """List reservations with full financial detail including OTA commission (手数料).

        Same as list_reservations_in_range but additionally populates
        ``ota_commission_jpy`` for each OTA booking by fetching and parsing
        the Airhost CSV export. This takes 10-20 s longer due to the async
        export flow. Use this tool when OTA commission data is required;
        use list_reservations_in_range for faster lookups that don't need it.
        ``end_date`` is inclusive.
        """
        _audit(
            ctx,
            "list_reservation_details",
            listing_id=listing_id,
            start_date=start_date,
            end_date=end_date,
        )
        results = await client.list_reservations_with_details(listing_id, start_date, end_date)
        return [r.model_dump(mode="json") for r in results]
