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

    # ---- OAuth 2.1 / Firebase Authentication ----
    # GCP project hosting Firebase Auth. The issuer is
    # ``https://securetoken.google.com/<firebase_project_id>``.
    firebase_project_id: str = ""
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
    airhost_login_url: str = "https://app.airhost.co/login"
    airhost_username: str = ""
    airhost_password: str = ""
    airhost_client: Literal["mock", "browser"] = "mock"
    browser_headless: bool = True

    # MFA
    mfa_strategy: Literal["gmail", "pubsub", "manual"] = "gmail"
    mfa_sender: str = ""
    mfa_subject_regex: str = r"^.*verification.*code.*$"
    mfa_code_regex: str = r"\b(\d{6})\b"
    mfa_timeout_seconds: int = 120

    # Gmail
    gmail_credentials_path: str = "./gmail_credentials.json"
    gmail_token_path: str = "./gmail_token.json"

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
