"""Tests for audit logging in tools.py."""

from __future__ import annotations

import logging
from datetime import date
from unittest.mock import MagicMock

import pytest

from airhost_mcp.tools import _audit


class _FakeCtx:
    """Minimal Context stub that carries a user_email on request.state."""

    def __init__(self, email: str = "alice@example.com") -> None:
        state = MagicMock()
        state.user_email = email
        req = MagicMock()
        req.state = state
        request_context = MagicMock()
        request_context.request = req
        self.request_context = request_context


async def test_audit_emits_log_line(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="airhost_mcp.tools"):
        _audit(_FakeCtx("bob@example.com"), "list_listings")

    assert any(
        "AUDIT" in r.message and "list_listings" in r.message and "bob@example.com" in r.message
        for r in caplog.records
    ), f"Expected audit log line not found in: {[r.message for r in caplog.records]}"


async def test_audit_unknown_when_ctx_is_none(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="airhost_mcp.tools"):
        _audit(None, "get_availability", listing_id="lst_001", target_date=date(2026, 5, 1))

    assert any(
        "user=unknown" in r.message and "get_availability" in r.message
        for r in caplog.records
    )


async def test_audit_includes_kwargs(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="airhost_mcp.tools"):
        _audit(_FakeCtx(), "block_date", listing_id="lst_002", target_date=date(2026, 6, 1))

    record = next(r for r in caplog.records if "block_date" in r.message)
    assert "lst_002" in record.message
