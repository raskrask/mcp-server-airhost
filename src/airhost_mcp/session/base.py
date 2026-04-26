"""Abstract session store interface."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class SessionRecord:
    """Persisted Airhost session.

    ``state`` holds whatever the client implementation needs to reuse a login.
    For the Playwright client this is the ``storage_state`` dict (cookies +
    localStorage origins) returned by ``BrowserContext.storage_state()``.
    ``meta`` is for arbitrary extras (last login timestamp, username, etc.).
    """

    state: dict[str, Any]
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    def is_expired(self, now: float | None = None) -> bool:
        if self.expires_at <= 0:
            return False
        return (now or time.time()) >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionRecord":
        # Backward-compat: older records stored cookies under "cookies".
        state = data.get("state")
        if state is None and "cookies" in data:
            state = {"cookies": data["cookies"], "origins": []}
        return cls(
            state=state or {},
            created_at=data.get("created_at", time.time()),
            expires_at=data.get("expires_at", 0.0),
            meta=data.get("meta", {}),
        )


class SessionStore(ABC):
    """Persist + retrieve session records by a string key (typically the username)."""

    @abstractmethod
    async def load(self, key: str) -> SessionRecord | None: ...

    @abstractmethod
    async def save(self, key: str, record: SessionRecord) -> None: ...

    @abstractmethod
    async def delete(self, key: str) -> None: ...
