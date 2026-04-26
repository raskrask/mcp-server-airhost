"""MFA code retrieval — pluggable strategies (Gmail / Pub/Sub / manual)."""

from .base import MFAStrategy, MFATimeoutError
from .factory import build_mfa_strategy

__all__ = ["MFAStrategy", "MFATimeoutError", "build_mfa_strategy"]
