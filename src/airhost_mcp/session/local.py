"""Local-filesystem session store. Used for development."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from .base import SessionRecord, SessionStore


def _safe_filename(key: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in key) + ".json"


class LocalSessionStore(SessionStore):
    def __init__(self, directory: str) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self._dir / _safe_filename(key)

    async def load(self, key: str) -> SessionRecord | None:
        path = self._path(key)
        if not path.exists():
            return None
        data = await asyncio.to_thread(path.read_text, "utf-8")
        return SessionRecord.from_dict(json.loads(data))

    async def save(self, key: str, record: SessionRecord) -> None:
        path = self._path(key)
        payload = json.dumps(record.to_dict(), ensure_ascii=False, indent=2)
        await asyncio.to_thread(path.write_text, payload, "utf-8")

    async def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            await asyncio.to_thread(path.unlink)
