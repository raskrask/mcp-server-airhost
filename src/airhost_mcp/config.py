"""Application configuration loaded from environment variables."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # MCP HTTP server
    mcp_bearer_tokens: str = Field(default="", description="Comma-separated bearer tokens.")
    host: str = "0.0.0.0"
    port: int = 8080
    mcp_mount_path: str = "/mcp"

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

    @field_validator("mcp_bearer_tokens")
    @classmethod
    def _strip_tokens(cls, v: str) -> str:
        return ",".join(t.strip() for t in v.split(",") if t.strip())

    @property
    def bearer_token_set(self) -> set[str]:
        return {t for t in self.mcp_bearer_tokens.split(",") if t}


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
