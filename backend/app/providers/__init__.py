"""AI provider construction."""
from app.config import settings
from app.providers.ollama import OllamaProvider

def get_provider(name: str | None = None, model: str | None = None):
    """Build the requested provider without requiring unused cloud secrets."""
    chosen = (name or settings.ai_provider).lower()
    if chosen == "ollama":
        return OllamaProvider(model=model or settings.ollama_model)
    raise ValueError(f"Provider '{chosen}' is not configured or supported in this phase")


def validate_provider_model(name: str | None, model: str | None) -> tuple[str, str]:
    """Validate provider/model before persisting a durable job."""
    chosen = (name or settings.ai_provider).strip().lower()
    if chosen != "ollama":
        raise ValueError(f"Provider '{chosen}' is not configured or supported in this phase")
    selected_model = (model or settings.ollama_model).strip()
    if not selected_model:
        raise ValueError("Ollama model cannot be empty")
    return chosen, selected_model
