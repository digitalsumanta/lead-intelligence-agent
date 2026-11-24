"""Environment-backed application settings."""
import os
from pathlib import Path
from typing import Literal
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration; secrets are sourced only from the environment."""
    # LEAD_AGENT_DISABLE_ENV_FILE=1 skips the repo-root .env (used by the test suite so local
    # developer settings such as live dispatch or cloud providers never leak into tests).
    model_config = SettingsConfigDict(
        env_file=None if os.environ.get("LEAD_AGENT_DISABLE_ENV_FILE") else Path(__file__).resolve().parents[2] / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
    app_name: str = "Lead Intelligence Agent"
    database_url: str = "postgresql+asyncpg://lead_agent@localhost:5432/lead_agent"
    ai_provider: str = Field(default="ollama", validation_alias=AliasChoices("AI_DEFAULT_PROVIDER", "AI_PROVIDER"))
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5-coder:latest"
    ai_ollama_timeout_seconds: float = Field(
        default=450.0,
        validation_alias=AliasChoices("ai_ollama_timeout_seconds", "AI_OLLAMA_TIMEOUT_SECONDS", "OLLAMA_TIMEOUT_SECONDS"),
    )
    ai_gemini_enabled: bool = False
    ai_gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    ai_gemini_model: str = "gemini-2.5-flash"
    ai_gemini_timeout_seconds: float = 90
    ai_openai_compatible_enabled: bool = False
    ai_openai_compatible_base_url: str = "http://localhost:1234/v1"
    ai_openai_compatible_model: str = "local-model"
    ai_openai_compatible_timeout_seconds: float = 90
    openai_api_key: str = ""
    gemini_api_key: str = Field(default="", validation_alias=AliasChoices("AI_GEMINI_API_KEY", "GEMINI_API_KEY"))
    openai_api_keys: str = ""
    gemini_api_keys: str = Field(default="", validation_alias=AliasChoices("AI_GEMINI_API_KEYS", "GEMINI_API_KEYS"))
    ai_openai_compatible_api_key: str = ""
    ai_openai_compatible_api_keys: str = ""
    google_places_api_key: str = ""
    google_places_enabled: bool = False
    google_places_request_timeout_seconds: float = 10
    google_places_operation: str = "places_search_text"
    pagespeed_api_key: str = ""
    pagespeed_enabled: bool = False
    pagespeed_operation: str = "run_pagespeed"
    pagespeed_request_timeout_seconds: float = 45
    pagespeed_strategy: Literal["mobile", "desktop"] = "mobile"
    pagespeed_categories: str = "performance,accessibility,best-practices,seo"
    cors_origins: str = "http://localhost:3000,http://localhost:3001"
    max_csv_bytes: int = 5_000_000
    max_batch_size: int = 100
    request_timeout_seconds: float = Field(
        default=450.0,
        validation_alias=AliasChoices("request_timeout_seconds", "REQUEST_TIMEOUT_SECONDS", "AI_REQUEST_TIMEOUT_SECONDS"),
    )
    max_concurrency: int = 2
    provider_max_retries: int = 2
    provider_backoff_base_seconds: float = 0.5
    provider_backoff_max_seconds: float = 8.0
    provider_retry_jitter_seconds: float = 0.2
    provider_max_concurrency: int = 4
    provider_key_lease_seconds: int = 500
    provider_failure_cooldown_seconds: float = 0.5
    provider_rate_limit_cooldown_seconds: int = 60
    provider_quota_cooldown_seconds: int = 3600
    outreach_email_provider: str = ""
    outreach_email_api_url: str = ""
    outreach_email_api_key: str = ""
    outreach_email_from: str = ""
    outreach_email_enabled: bool = False
    outreach_resend_api_key: str = ""
    outreach_resend_from: str = ""
    outreach_resend_api_url: str = "https://api.resend.com/emails"
    outreach_sendgrid_api_key: str = ""
    outreach_sendgrid_from: str = ""
    outreach_sendgrid_api_url: str = "https://api.sendgrid.com/v3/mail/send"
    outreach_resend_webhook_secret: str = ""
    outreach_sendgrid_webhook_public_key: str = ""

    outreach_dispatch_timeout_seconds: float = 10.0

    outreach_dispatch_connect_timeout_seconds: float = 3.0
    outreach_dispatch_read_timeout_seconds: float = 7.0
    outreach_dispatch_max_workers: int = 2
    outreach_email_rate_limit_per_second: float = 5.0
    email_provider: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = ""
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False
    smtp_timeout_seconds: float = 20
    outreach_whatsapp_provider: str = ""
    outreach_whatsapp_api_url: str = ""
    outreach_whatsapp_api_key: str = ""
    outreach_whatsapp_phone_number_id: str = ""
    outreach_whatsapp_enabled: bool = False
    outreach_dispatch_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("outreach_dispatch_enabled", "OUTREACH_DISPATCH_ENABLED", "DISPATCH_ENABLED"),
    )
    outreach_canary_mode: bool = Field(
        default=False,
        validation_alias=AliasChoices("outreach_canary_mode", "OUTREACH_CANARY_MODE", "CANARY_MODE"),
    )
    # --- Email compliance (CAN-SPAM / Gmail-Yahoo bulk sender rules / RFC 8058 one-click) ---
    # When enforced, every outbound email must carry a working unsubscribe mechanism and the
    # sender's postal address; sends fail closed if this configuration is incomplete.
    outreach_compliance_enforced: bool = Field(default=True, validation_alias=AliasChoices("OUTREACH_COMPLIANCE_ENFORCED"))
    outreach_unsubscribe_secret: str = Field(default="", validation_alias=AliasChoices("OUTREACH_UNSUBSCRIBE_SECRET"))
    # Public base URL of this backend (e.g. https://api.example.com); links go to {base}/unsubscribe/{token}.
    outreach_unsubscribe_base_url: str = Field(default="", validation_alias=AliasChoices("OUTREACH_UNSUBSCRIBE_BASE_URL"))
    # Optional mailbox for mailto: unsubscribe requests (e.g. unsubscribe@example.com).
    outreach_unsubscribe_mailto: str = Field(default="", validation_alias=AliasChoices("OUTREACH_UNSUBSCRIBE_MAILTO"))
    outreach_sender_business_name: str = Field(default="", validation_alias=AliasChoices("OUTREACH_SENDER_BUSINESS_NAME"))
    outreach_sender_postal_address: str = Field(default="", validation_alias=AliasChoices("OUTREACH_SENDER_POSTAL_ADDRESS"))
    outreach_canary_allowed_domains: str = Field(
        default="internal-test.leadintel.example.com",
        validation_alias=AliasChoices("outreach_canary_allowed_domains", "OUTREACH_CANARY_ALLOWED_DOMAINS", "CANARY_ALLOWED_DOMAINS"),
    )
    api_auth_token: str = ""
    api_auth_user_id: str = "local_api_user"
    # --- Dashboard user sessions (cookie auth). API_AUTH_TOKEN remains for scripts/automation only. ---
    auth_session_ttl_hours: float = Field(default=12.0, gt=0, le=720, validation_alias=AliasChoices("AUTH_SESSION_TTL_HOURS"))
    auth_cookie_name: str = Field(default="lead_agent_session", validation_alias=AliasChoices("AUTH_COOKIE_NAME"))
    # None = automatic: Secure cookies in production/staging (browsers accept them on http://localhost too).
    auth_cookie_secure: bool | None = Field(default=None, validation_alias=AliasChoices("AUTH_COOKIE_SECURE"))
    auth_max_failed_logins: int = Field(default=5, ge=1, le=100, validation_alias=AliasChoices("AUTH_MAX_FAILED_LOGINS"))
    auth_lockout_minutes: int = Field(default=15, ge=1, le=1440, validation_alias=AliasChoices("AUTH_LOCKOUT_MINUTES"))
    api_auth_role: str = Field(default="viewer", validation_alias=AliasChoices("API_AUTH_ROLE"))
    app_env: str = Field(default="development", validation_alias=AliasChoices("APP_ENV", "ENVIRONMENT"))
    supervisor_strict_separation: bool = Field(default=True, validation_alias=AliasChoices("SUPERVISOR_STRICT_SEPARATION"))
    allow_dev_shared_approval: bool = Field(default=False, validation_alias=AliasChoices("SUPERVISOR_ALLOW_DEV_SHARED_APPROVAL", "ALLOW_DEV_SHARED_APPROVAL"))
    allow_dev_role_override: bool = Field(default=False, validation_alias=AliasChoices("SUPERVISOR_ALLOW_DEV_ROLE_OVERRIDE", "ALLOW_DEV_ROLE_OVERRIDE"))
    agent_contact_cooldown_minutes: int = Field(default=1440, ge=0, le=525600)
    supervisor_lease_duration_seconds: int = Field(default=300, ge=10, le=86400, validation_alias=AliasChoices("SUPERVISOR_LEASE_DURATION_SECONDS"))
    supervisor_heartbeat_timeout_seconds: int = Field(default=60, ge=5, le=3600, validation_alias=AliasChoices("SUPERVISOR_HEARTBEAT_TIMEOUT_SECONDS"))

    @property
    def is_production(self) -> bool:
        """Determines whether the application is running in a production or staging environment."""
        env_val = (self.app_env or "").strip().lower()
        return env_val in {"production", "prod", "staging", "stage"}

    @property
    def effective_auth_cookie_secure(self) -> bool:
        return self.is_production if self.auth_cookie_secure is None else bool(self.auth_cookie_secure)

    @property
    def is_unrestricted_live_dispatch(self) -> bool:
        """True when real outreach can be sent without the canary recipient/flight restrictions."""
        return bool(getattr(self, "outreach_dispatch_enabled", False)) and not bool(getattr(self, "outreach_canary_mode", False))

    @property
    def effective_allow_dev_role_override(self) -> bool:
        """Client-controlled role overrides are forbidden in production/staging and whenever
        unrestricted live dispatch is enabled, regardless of the configured flag."""
        if self.is_production or self.is_unrestricted_live_dispatch:
            return False
        return bool(self.allow_dev_role_override)

    @property
    def effective_allow_dev_shared_approval(self) -> bool:
        """Shared development identity approvals are forbidden in production/staging and whenever
        unrestricted live dispatch is enabled, regardless of the configured flag."""
        if self.is_production or self.is_unrestricted_live_dispatch:
            return False
        return bool(self.allow_dev_shared_approval)

    @property
    def effective_outreach_dispatch_enabled(self) -> bool:
        """Fail-closed evaluation of global dispatch authorization."""
        if not hasattr(self, "outreach_dispatch_enabled"):
            return False
        return bool(self.outreach_dispatch_enabled)

    def is_channel_enabled(self, channel: str | None) -> bool:
        """Evaluates whether an individual channel toggle permits outreach."""
        ch = (channel or "").strip().upper()
        if ch == "EMAIL":
            return bool(self.outreach_email_enabled)
        elif ch == "WHATSAPP":
            return bool(self.outreach_whatsapp_enabled)
        return False

    def is_dispatch_permitted(self, channel: str | None) -> bool:
        """
        Two-tier gating rule:
        PermitDispatch(channel) <=> effective_outreach_dispatch_enabled AND is_channel_enabled(channel).
        Fails closed if global switch or channel switch is disabled.
        """
        return self.effective_outreach_dispatch_enabled and self.is_channel_enabled(channel)

    @property
    def canary_allowed_domain_list(self) -> list[str]:
        """Return configured canary allowed domains as a clean list."""
        if not self.outreach_canary_allowed_domains:
            return []
        return [d.strip().lower() for d in self.outreach_canary_allowed_domains.split(",") if d.strip()]

    @property
    def cors_origin_list(self) -> list[str]:
        """Return configured origins as a clean list."""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def provider_key_map(self) -> dict[str, list[str]]:
        """Return non-empty provider credentials from singular and plural env settings."""
        result = {}
        for provider, values in (
            ("openai", [self.openai_api_key, *self.openai_api_keys.split(",")]),
            ("gemini", [self.gemini_api_key, *self.gemini_api_keys.split(",")]),
            ("openai_compatible", [self.ai_openai_compatible_api_key, *self.ai_openai_compatible_api_keys.split(",")]),
            ("google_places", [self.google_places_api_key]),
            ("pagespeed", [self.pagespeed_api_key]),
        ):
            cleaned = list(dict.fromkeys(value.strip() for value in values if value.strip()))
            if cleaned: result[provider] = cleaned
        return result


settings = Settings()
