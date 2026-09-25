"""Environment-backed application settings."""
from pathlib import Path
from typing import Literal
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration; secrets are sourced only from the environment."""
    model_config = SettingsConfigDict(env_file=Path(__file__).resolve().parents[2] / ".env", env_file_encoding="utf-8", extra="ignore")
    app_name: str = "Lead Intelligence Agent"
    database_url: str = "postgresql+asyncpg://lead_agent@localhost:5432/lead_agent"
    ai_provider: str = "ollama"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5-coder:latest"
    openai_api_key: str = ""
    gemini_api_key: str = ""
    openai_api_keys: str = ""
    gemini_api_keys: str = ""
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
    cors_origins: str = "http://localhost:3000"
    max_csv_bytes: int = 5_000_000
    max_batch_size: int = 100
    request_timeout_seconds: float = 300
    max_concurrency: int = 2
    provider_max_retries: int = 2
    provider_backoff_base_seconds: float = 0.5
    provider_backoff_max_seconds: float = 8.0
    provider_retry_jitter_seconds: float = 0.2
    provider_max_concurrency: int = 4
    provider_key_lease_seconds: int = 360
    provider_failure_cooldown_seconds: float = 0.5
    provider_rate_limit_cooldown_seconds: int = 60
    provider_quota_cooldown_seconds: int = 3600
    api_auth_token: str = ""

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
            ("google_places", [self.google_places_api_key]),
            ("pagespeed", [self.pagespeed_api_key]),
        ):
            cleaned = list(dict.fromkeys(value.strip() for value in values if value.strip()))
            if cleaned: result[provider] = cleaned
        return result


settings = Settings()
