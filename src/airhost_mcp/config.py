"""Application configuration loaded from environment variables."""

from __future__ import annotations

from typing import Literal

from pydantic import field_validator
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

    # ---- Self-hosted OAuth 2.1 ----
    # client_id to register in Claude's MCP connector (not secret).
    mcp_client_id: str = ""
    # client_secret to register in Claude's MCP connector (store in Secret Manager).
    mcp_client_secret: str = ""
    # HMAC-SHA256 signing key for issued JWTs (store in Secret Manager).
    mcp_token_secret: str = ""
    # Access token lifetime in days. Long lifetime avoids re-auth on token expiry
    # since Claude's connector does not reliably implement refresh token rotation.
    mcp_access_token_ttl_days: int = 365
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
    mfa_strategy: Literal["gmail", "manual"] = "gmail"
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
    # Secret Manager secret name for the Gmail token (e.g. "GMAIL_TOKEN").
    # When set, a refreshed token is written back to Secret Manager so it
    # survives instance restarts on Cloud Run (read-only volume mounts cannot
    # be written to directly).  Leave empty for local development.
    gmail_token_secret_name: str = ""
    # What to do with the MFA email after extracting the code.
    #   keep    — leave in inbox untouched (default for safety)
    #   read    — mark as read only
    #   archive — mark read + remove from Inbox label (moves to All Mail)
    #   trash   — move to Trash (recoverable for ~30 days)
    #   delete  — permanently delete (NOT recoverable; bypasses Trash)
    # Requires scope gmail.modify for anything other than "keep".
    mfa_after_fetch: Literal["keep", "read", "archive", "trash", "delete"] = "trash"

    # Session store
    session_store: Literal["local", "gcs"] = "local"
    session_local_dir: str = "./.sessions"
    session_gcs_bucket: str = ""
    session_gcs_prefix: str = "airhost-mcp/sessions/"
    session_ttl_seconds: int = 86400  # 24 h; Airhost sessions survive overnight

    log_level: str = "INFO"

    @field_validator("mcp_client_secret", "mcp_token_secret", mode="before")
    @classmethod
    def strip_secret(cls, v: str) -> str:
        return v.strip() if isinstance(v, str) else v



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
