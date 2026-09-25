"""AI provider construction."""
from app.config import settings
from app.providers.ollama import OllamaProvider
from app.providers.base import OpenAIProvider, GeminiProvider

def get_provider(name: str | None = None, model: str | None = None):
    """Build the requested provider without requiring unused cloud secrets."""
    chosen = (name or settings.ai_provider).lower()
    if chosen == "ollama": return OllamaProvider(model=model)
    if chosen == "openai" and settings.provider_key_map.get("openai"): return OpenAIProvider()
    if chosen == "gemini" and settings.provider_key_map.get("gemini"): return GeminiProvider()
    raise ValueError(f"Provider '{chosen}' is not configured")
