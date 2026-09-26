"""AI provider registry, selection, and compatibility construction helpers."""

from __future__ import annotations

from urllib.parse import urlparse

from app.config import settings
from app.domain.ai import AIProviderOutcome, AIProviderSelectionError
from app.services.provider_execution import ProviderExecutionService


class AIProviderRegistry:
    """Construct providers with shared request execution infrastructure."""

    def __init__(self, execution: ProviderExecutionService, config=settings, *, client_factory=None):
        self.execution = execution
        self.config = config
        self.client_factory = client_factory

    def create(self, name: str, model: str | None = None):
        chosen = name.strip().lower()
        if chosen == "ollama":
            from app.providers.ollama import OllamaProvider
            return OllamaProvider(model=model or self.config.ollama_model, base_url=self.config.ollama_base_url, execution=self.execution, **({"client_factory": self.client_factory} if self.client_factory else {}))
        if chosen == "gemini":
            from app.providers.gemini import GeminiProvider
            return GeminiProvider(self.execution, model=model or self.config.ai_gemini_model, base_url=self.config.ai_gemini_base_url, timeout_seconds=self.config.ai_gemini_timeout_seconds, enabled=self.config.ai_gemini_enabled, **({"client_factory": self.client_factory} if self.client_factory else {}))
        if chosen == "openai_compatible":
            from app.providers.openai_compatible import OpenAICompatibleProvider
            return OpenAICompatibleProvider(self.execution, model=model or self.config.ai_openai_compatible_model, base_url=self.config.ai_openai_compatible_base_url, timeout_seconds=self.config.ai_openai_compatible_timeout_seconds, enabled=self.config.ai_openai_compatible_enabled, **({"client_factory": self.client_factory} if self.client_factory else {}))
        raise AIProviderSelectionError(AIProviderOutcome.UNSUPPORTED_CAPABILITY, f"Provider '{chosen}' is not configured or supported in this phase", chosen)


class AIProviderSelector:
    """Select exactly the requested/default provider; never falls back silently."""

    SUPPORTED = {"ollama", "gemini", "openai_compatible"}

    def __init__(self, registry: AIProviderRegistry, config=settings):
        self.registry = registry
        self.config = config

    def select(self, name: str | None = None, model: str | None = None):
        chosen = (name or self.config.ai_provider).strip().lower()
        if chosen not in self.SUPPORTED:
            raise AIProviderSelectionError(AIProviderOutcome.UNSUPPORTED_CAPABILITY, f"Provider '{chosen}' is not configured or supported in this phase", chosen)
        enabled_attr = {"gemini": "ai_gemini_enabled", "openai_compatible": "ai_openai_compatible_enabled"}.get(chosen)
        if enabled_attr and not getattr(self.config, enabled_attr, False):
            raise AIProviderSelectionError(AIProviderOutcome.PROVIDER_DISABLED, f"Provider '{chosen}' is disabled or not configured", chosen)
        model_attr = {"ollama": "ollama_model", "gemini": "ai_gemini_model", "openai_compatible": "ai_openai_compatible_model"}[chosen]
        url_attr = {"ollama": "ollama_base_url", "gemini": "ai_gemini_base_url", "openai_compatible": "ai_openai_compatible_base_url"}[chosen]
        selected_model = (model or getattr(self.config, model_attr)).strip()
        if not selected_model:
            raise AIProviderSelectionError(AIProviderOutcome.PROVIDER_UNAVAILABLE, f"{chosen} model cannot be empty", chosen)
        parsed = urlparse(getattr(self.config, url_attr))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise AIProviderSelectionError(AIProviderOutcome.PROVIDER_UNAVAILABLE, f"{chosen} base URL configuration is invalid", chosen)
        if chosen == "gemini" and self.config.ai_gemini_timeout_seconds <= 0:
            raise AIProviderSelectionError(AIProviderOutcome.PROVIDER_UNAVAILABLE, "Gemini timeout must be positive", chosen)
        if chosen == "openai_compatible" and self.config.ai_openai_compatible_timeout_seconds <= 0:
            raise AIProviderSelectionError(AIProviderOutcome.PROVIDER_UNAVAILABLE, "OpenAI-compatible timeout must be positive", chosen)
        return self.registry.create(chosen, selected_model)


def _selector(execution_service=None, session_factory=None, config=settings, client_factory=None):
    if execution_service is None:
        if session_factory is None:
            from app.database import SessionLocal
            session_factory = SessionLocal
        execution_service = ProviderExecutionService(session_factory)
    return AIProviderSelector(AIProviderRegistry(execution_service, config, client_factory=client_factory), config)


def get_provider(name: str | None = None, model: str | None = None, *, execution_service=None, session_factory=None, config=settings, client_factory=None):
    """Build the selected provider, preserving the established job-facing API."""
    return _selector(execution_service, session_factory, config, client_factory).select(name, model)


def validate_provider_model(name: str | None, model: str | None) -> tuple[str, str]:
    """Validate the selected provider/model before persisting a durable job."""
    chosen = (name or settings.ai_provider).strip().lower()
    provider = _selector(config=settings).select(chosen, model)
    return chosen, provider.model
