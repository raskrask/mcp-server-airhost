"""Pick an Airhost client implementation based on settings."""

from __future__ import annotations

from ..config import Settings
from .base import AirhostClient
from .mock import MockAirhostClient


def build_airhost_client(settings: Settings) -> AirhostClient:
    if settings.airhost_client == "mock":
        return MockAirhostClient()
    if settings.airhost_client == "browser":
        from ..mfa import build_mfa_strategy
        from ..session import build_session_store
        from .browser_client import BrowserAirhostClient

        return BrowserAirhostClient(
            login_url=settings.airhost_login_url,
            username=settings.airhost_username,
            password=settings.airhost_password,
            session_store=build_session_store(settings),
            mfa=build_mfa_strategy(settings),
            mfa_timeout_seconds=settings.mfa_timeout_seconds,
            session_ttl_seconds=settings.session_ttl_seconds,
            headless=settings.browser_headless,
        )
    raise ValueError(f"unknown airhost_client: {settings.airhost_client}")
