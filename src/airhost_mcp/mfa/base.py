"""MFA strategy contract.

Strategies are responsible for *waiting for* the MFA code that Airhost emails
during login and returning the 6-digit value. Implementations differ in how
they receive the email (poll Gmail, listen on Pub/Sub, ask the operator).
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class MFATimeoutError(RuntimeError):
    """Raised when no MFA code arrives within the configured window."""


class MFAStrategy(ABC):
    @abstractmethod
    async def fetch_code(self, *, since_epoch: float, timeout_seconds: int) -> str:
        """Block until a code arrives (or timeout) and return the digits.

        Implementations should ignore emails older than ``since_epoch`` to
        avoid replaying a code from a previous login.
        """
