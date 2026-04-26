"""Pick a session store implementation based on settings."""

from __future__ import annotations

from ..config import Settings
from .base import SessionStore
from .local import LocalSessionStore


def build_session_store(settings: Settings) -> SessionStore:
    if settings.session_store == "local":
        return LocalSessionStore(settings.session_local_dir)
    if settings.session_store == "gcs":
        # Imported lazily so local dev doesn't require google-cloud-storage to start.
        from .gcs import GCSSessionStore

        return GCSSessionStore(
            bucket=settings.session_gcs_bucket,
            prefix=settings.session_gcs_prefix,
        )
    raise ValueError(f"unknown session_store: {settings.session_store}")
