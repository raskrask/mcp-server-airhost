"""Manual MFA — read the code from stdin. Dev / debugging only."""

from __future__ import annotations

import asyncio

from .base import MFAStrategy, MFATimeoutError


class ManualMFAStrategy(MFAStrategy):
    async def fetch_code(self, *, since_epoch: float, timeout_seconds: int) -> str:
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, lambda: input("Enter MFA code: ").strip()),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise MFATimeoutError("manual MFA entry timed out") from exc
