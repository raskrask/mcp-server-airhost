"""Pick an MFA strategy based on settings."""

from __future__ import annotations

from ..config import Settings
from .base import MFAStrategy


def build_mfa_strategy(settings: Settings) -> MFAStrategy:
    if settings.mfa_strategy == "gmail":
        from .gmail import GmailMFAStrategy

        return GmailMFAStrategy(
            credentials_path=settings.gmail_credentials_path,
            token_path=settings.gmail_token_path,
            sender=settings.mfa_sender,
            subject_regex=settings.mfa_subject_regex,
            code_regex=settings.mfa_code_regex,
            after_fetch=settings.mfa_after_fetch,
        )
    if settings.mfa_strategy == "manual":
        from .manual import ManualMFAStrategy

        return ManualMFAStrategy()
    raise ValueError(f"unknown MFA strategy: {settings.mfa_strategy}")
