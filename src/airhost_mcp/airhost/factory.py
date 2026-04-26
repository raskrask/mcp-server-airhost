"""Pick an Airhost client implementation based on settings."""

from __future__ import annotations

from ..config import Settings
from .base import AirhostClient
from .mock import MockAirhostClient


def build_airhost_client(settings: Settings) -> AirhostClient:
    if settings.airhost_client == "mock":
        return MockAirhostClient()
    if settings.airhost_client == "http":
        from ..mfa import build_mfa_strategy
        from ..session import build_session_store
        from .http_client import HTTPAirhostClient

        return HTTPAirhostClient(
            login_url=settings.airhost_login_url,
            username=settings.airhost_username,
            password=settings.airhost_password,
            session_store=build_session_store(settings),
            mfa=build_mfa_strategy(settings),
            mfa_timeout_seconds=settings.mfa_timeout_seconds,
        )
    raise ValueError(f"unknown airhost_client: {settings.airhost_client}")
