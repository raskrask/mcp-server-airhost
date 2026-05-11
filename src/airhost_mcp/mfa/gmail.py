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

# gmail.readonly is sufficient for keep/read/archive/trash as long as we use
# the modify scope for label changes. We request modify unconditionally so
# the token stays usable even when mfa_after_fetch changes at runtime.
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

AfterFetch = str  # "keep" | "read" | "archive" | "trash"


class GmailMFAStrategy(MFAStrategy):
    def __init__(
        self,
        *,
        credentials_path: str,
        token_path: str,
        token_secret_name: str = "",
        sender: str,
        subject_regex: str,
        code_regex: str,
        after_fetch: AfterFetch = "keep",
        poll_interval: float = 3.0,
    ) -> None:
        self._credentials_path = credentials_path
        self._token_path = token_path
        self._token_secret_name = token_secret_name
        self._sender = sender
        self._subject_re = re.compile(subject_regex, re.IGNORECASE)
        self._code_re = re.compile(code_regex)
        self._after_fetch = after_fetch
        self._poll_interval = poll_interval

    def _writeback_token(self, creds: Credentials) -> None:
        """Persist a refreshed token.

        Tries the local file first (works in development).  On Cloud Run the
        Secret Manager volume mount is read-only, so falls back to writing a
        new Secret Manager version when ``token_secret_name`` is configured.
        This keeps the refresh token alive across instance restarts.
        """
        token_json = creds.to_json()
        token_file = Path(self._token_path)

        # Local file (works in dev; fails silently on Cloud Run read-only mount).
        try:
            token_file.write_text(token_json, encoding="utf-8")
            logger.debug("gmail token written back to file %s", self._token_path)
            return
        except OSError as exc:
            logger.debug("gmail token file writeback failed (%s); trying Secret Manager", exc)

        # Secret Manager writeback (Cloud Run).
        if not self._token_secret_name:
            logger.warning(
                "gmail token writeback: file is read-only and GMAIL_TOKEN_SECRET_NAME is not set"
            )
            return
        try:
            import os
            from google.cloud import secretmanager  # type: ignore[import-untyped]

            project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCLOUD_PROJECT")
            if not project_id:
                import urllib.request
                req = urllib.request.Request(
                    "http://metadata.google.internal/computeMetadata/v1/project/project-id",
                    headers={"Metadata-Flavor": "Google"},
                )
                project_id = urllib.request.urlopen(req, timeout=3).read().decode()

            sm = secretmanager.SecretManagerServiceClient()
            sm.add_secret_version(
                request={
                    "parent": f"projects/{project_id}/secrets/{self._token_secret_name}",
                    "payload": {"data": token_json.encode("utf-8")},
                }
            )
            logger.info(
                "gmail token written back to Secret Manager secret %r",
                self._token_secret_name,
            )
        except Exception as exc:
            logger.warning("gmail token Secret Manager writeback failed: %s", exc)

    def _get_creds(self) -> Credentials:
        token_file = Path(self._token_path)
        creds: Credentials | None = None
        if token_file.exists():
            creds = Credentials.from_authorized_user_file(str(token_file), GMAIL_SCOPES)
        if creds and creds.valid:
            return creds
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            self._writeback_token(creds)
            return creds
        # First-time consent flow. Local only — Cloud Run should ship a pre-built token.
        flow = InstalledAppFlow.from_client_secrets_file(
            self._credentials_path, GMAIL_SCOPES
        )
        creds = flow.run_local_server(port=0)
        self._writeback_token(creds)
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

    async def _after_fetch_action(self, service: object, msg_id: str) -> None:
        """Apply the configured post-fetch action to the matched MFA email."""
        if self._after_fetch == "keep":
            return
        try:
            if self._after_fetch == "delete":
                # Permanently delete — bypasses Trash and is NOT recoverable.
                await asyncio.to_thread(
                    lambda: service.users()  # type: ignore[union-attr]
                    .messages()
                    .delete(userId="me", id=msg_id)
                    .execute()
                )
                logger.debug("gmail mfa: permanently deleted message %s", msg_id)
                return
            if self._after_fetch == "trash":
                await asyncio.to_thread(
                    lambda: service.users()  # type: ignore[union-attr]
                    .messages()
                    .trash(userId="me", id=msg_id)
                    .execute()
                )
                logger.debug("gmail mfa: trashed message %s", msg_id)
                return
            body: dict = {}
            if self._after_fetch == "read":
                body = {"removeLabelIds": ["UNREAD"]}
            elif self._after_fetch == "archive":
                body = {"removeLabelIds": ["UNREAD", "INBOX"]}
            if body:
                await asyncio.to_thread(
                    lambda: service.users()  # type: ignore[union-attr]
                    .messages()
                    .modify(userId="me", id=msg_id, body=body)
                    .execute()
                )
                logger.debug(
                    "gmail mfa: applied after_fetch=%s to message %s",
                    self._after_fetch,
                    msg_id,
                )
        except Exception as exc:
            # Never let cleanup failure block the login flow.
            logger.warning("gmail mfa after-fetch action failed for %s: %s", msg_id, exc)

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
                        await self._after_fetch_action(service, msg_ref["id"])
                        return code

                # Otherwise fall back to scanning body/snippet/subject.
                body_text = self._extract_body(full.get("payload", {}))
                snippet = full.get("snippet", "")
                for source in (body_text, snippet, subject):
                    m = self._code_re.search(source)
                    if m:
                        await self._after_fetch_action(service, msg_ref["id"])
                        return m.group(1) if m.groups() else m.group(0)
            await asyncio.sleep(self._poll_interval)

        raise MFATimeoutError(
            f"no MFA code matching sender={self._sender!r} arrived within {timeout_seconds}s"
        )
