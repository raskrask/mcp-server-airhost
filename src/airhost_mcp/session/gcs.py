"""Google Cloud Storage session store. Used in Cloud Run."""

from __future__ import annotations

import asyncio
import json

from google.cloud import storage  # type: ignore[import-untyped]

from .base import SessionRecord, SessionStore


class GCSSessionStore(SessionStore):
    def __init__(self, bucket: str, prefix: str = "") -> None:
        if not bucket:
            raise ValueError("GCS bucket name is required")
        self._client = storage.Client()
        self._bucket = self._client.bucket(bucket)
        self._prefix = prefix.rstrip("/") + "/" if prefix else ""

    def _blob_name(self, key: str) -> str:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in key)
        return f"{self._prefix}{safe}.json"

    async def load(self, key: str) -> SessionRecord | None:
        blob = self._bucket.blob(self._blob_name(key))
        exists = await asyncio.to_thread(blob.exists)
        if not exists:
            return None
        data = await asyncio.to_thread(blob.download_as_bytes)
        return SessionRecord.from_dict(json.loads(data.decode("utf-8")))

    async def save(self, key: str, record: SessionRecord) -> None:
        blob = self._bucket.blob(self._blob_name(key))
        payload = json.dumps(record.to_dict(), ensure_ascii=False).encode("utf-8")
        await asyncio.to_thread(
            blob.upload_from_string, payload, content_type="application/json"
        )

    async def delete(self, key: str) -> None:
        blob = self._bucket.blob(self._blob_name(key))
        exists = await asyncio.to_thread(blob.exists)
        if exists:
            await asyncio.to_thread(blob.delete)
