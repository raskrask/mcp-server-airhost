"""Application configuration loaded from environment variables."""

from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # MCP HTTP server
    host: str = "0.0.0.0"
    port: int = 8080
    mcp_mount_path: str = "/mcp"

    # ---- OAuth 2.1 / Auth0 ----
    # Auth0 tenant domain, e.g. "mot-cozy-space.jp.auth0.com".
    auth0_domain: str = ""
    # API identifier configured in Auth0; the JWT ``aud`` claim must match.
    auth0_audience: str = ""
    # Optional explicit issuer override. Defaults to "https://{auth0_domain}/".
    auth0_issuer: str = ""
    # Comma-separated allowlist of authorized email addresses. Compared
    # case-insensitively against the verified ``email`` claim.
    mcp_allowed_emails: str = ""
    # Public origin (and optional path) of this MCP server, used in OAuth
    # protected-resource metadata. If empty, derived from the first incoming
    # request's ``base_url`` and cached.
    mcp_public_url: str = ""
    # Local-dev escape hatch: skip auth entirely. NEVER set true in production.
    # The middleware additionally refuses to honor this when the Cloud Run
    # K_SERVICE env var is present (see ``server.py``).
    dev_disable_auth: bool = False

    # Airhost
    airhost_login_url: str = "https://pms.airhost.co/ja/sign_in"
    airhost_username: str = ""
    airhost_password: str = ""
    airhost_client: Literal["mock", "browser"] = "mock"
    browser_headless: bool = True

    # MFA
    mfa_strategy: Literal["gmail", "pubsub", "manual"] = "gmail"
    # Airhost MFA mail comes in two flavors:
    #   * Normal:    Subject "[Airhost One] ログインコードは 123456 です。"
    #                — code is in the subject; we capture it.
    #   * New device: Subject "新しいデバイスのログイン確認"
    #                — code is in the body ("OTP認証コード: 615436");
    #                  subject regex matches but doesn't capture, so we fall
    #                  through to mfa_code_regex against the body.
    mfa_sender: str = "noreply@airhost.co"
    mfa_subject_regex: str = (
        r"(?:ログインコードは\s+(\d{6})|新しいデバイス|認証コード|OTP)"
    )
    mfa_code_regex: str = r"\b(\d{6})\b"
    mfa_timeout_seconds: int = 120

    # Gmail
    gmail_credentials_path: str = "./gmail_credentials.json"
    gmail_token_path: str = "./gmail_token.json"
    # What to do with the MFA email after extracting the code.
    #   keep    — leave in inbox untouched (default for safety)
    #   read    — mark as read only
    #   archive — mark read + remove from Inbox label (moves to All Mail)
    #   trash   — move to Trash
    # Requires scope gmail.modify for anything other than "keep".
    mfa_after_fetch: Literal["keep", "read", "archive", "trash"] = "keep"

    # Pub/Sub (future)
    pubsub_project_id: str = ""
    pubsub_subscription: str = ""

    # Session store
    session_store: Literal["local", "gcs"] = "local"
    session_local_dir: str = "./.sessions"
    session_gcs_bucket: str = ""
    session_gcs_prefix: str = "airhost-mcp/sessions/"
    session_ttl_seconds: int = 3600

    log_level: str = "INFO"

    @property
    def allowed_email_set(self) -> frozenset[str]:
        """Lower-cased, stripped allowlist of accepted user emails."""
        return frozenset(
            e.strip().lower() for e in self.mcp_allowed_emails.split(",") if e.strip()
        )


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings_cache() -> None:
    """Test helper: drop the memoized settings so the next ``get_settings()``
    call re-reads the process environment."""
    global _settings
    _settings = None
