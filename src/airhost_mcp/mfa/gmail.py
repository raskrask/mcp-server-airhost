"""Gmail-based MFA: poll the inbox for the verification email and parse the code."""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build  # type: ignore[import-untyped]

from .base import MFAStrategy, MFATimeoutError

logger = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GmailMFAStrategy(MFAStrategy):
    def __init__(
        self,
        *,
        credentials_path: str,
        token_path: str,
        sender: str,
        subject_regex: str,
        code_regex: str,
        poll_interval: float = 3.0,
    ) -> None:
        self._credentials_path = credentials_path
        self._token_path = token_path
        self._sender = sender
        self._subject_re = re.compile(subject_regex, re.IGNORECASE)
        self._code_re = re.compile(code_regex)
        self._poll_interval = poll_interval

    def _get_creds(self) -> Credentials:
        token_file = Path(self._token_path)
        creds: Credentials | None = None
        if token_file.exists():
            creds = Credentials.from_authorized_user_file(str(token_file), GMAIL_SCOPES)
        if creds and creds.valid:
            return creds
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_file.write_text(creds.to_json(), encoding="utf-8")
            return creds
        # First-time consent flow. Local only — Cloud Run should ship a pre-built token.
        flow = InstalledAppFlow.from_client_secrets_file(
            self._credentials_path, GMAIL_SCOPES
        )
        creds = flow.run_local_server(port=0)
        token_file.write_text(creds.to_json(), encoding="utf-8")
        return creds

    def _search_query(self, since_epoch: float) -> str:
        # Gmail's search uses "after:" with seconds-since-epoch.
        parts = [f"after:{int(since_epoch)}"]
        if self._sender:
            parts.append(f"from:{self._sender}")
        return " ".join(parts)

    @staticmethod
    def _decode_part(data: str) -> str:
        return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="replace")

    def _extract_body(self, payload: dict) -> str:
        if not payload:
            return ""
        body = payload.get("body", {})
        if body.get("data"):
            return self._decode_part(body["data"])
        for part in payload.get("parts", []) or []:
            text = self._extract_body(part)
            if text:
                return text
        return ""

    async def fetch_code(self, *, since_epoch: float, timeout_seconds: int) -> str:
        creds = await asyncio.to_thread(self._get_creds)
        service = await asyncio.to_thread(
            build, "gmail", "v1", credentials=creds, cache_discovery=False
        )

        deadline = time.time() + timeout_seconds
        query = self._search_query(since_epoch)
        logger.debug("gmail mfa polling query: %s", query)

        while time.time() < deadline:
            resp = await asyncio.to_thread(
                lambda: service.users()
                .messages()
                .list(userId="me", q=query, maxResults=10)
                .execute()
            )
            for msg_ref in resp.get("messages", []) or []:
                full = await asyncio.to_thread(
                    lambda mid=msg_ref["id"]: service.users()
                    .messages()
                    .get(userId="me", id=mid, format="full")
                    .execute()
                )
                headers = {
                    h["name"].lower(): h["value"]
                    for h in full.get("payload", {}).get("headers", [])
                }
                subject = headers.get("subject", "")
                subject_match = self._subject_re.search(subject)
                if not subject_match:
                    continue

                # If the subject regex itself captured the code (e.g. Airhost:
                # "ログインコードは 123456 です"), return it directly without
                # parsing the body. This avoids a second Gmail .get() round-trip
                # in the common case.
                if subject_match.groups():
                    code = subject_match.group(1)
                    if code:
                        return code

                # Otherwise fall back to scanning body/snippet/subject.
                body = self._extract_body(full.get("payload", {}))
                snippet = full.get("snippet", "")
                for source in (body, snippet, subject):
                    m = self._code_re.search(source)
                    if m:
                        return m.group(1) if m.groups() else m.group(0)
            await asyncio.sleep(self._poll_interval)

        raise MFATimeoutError(
            f"no MFA code matching sender={self._sender!r} arrived within {timeout_seconds}s"
        )
