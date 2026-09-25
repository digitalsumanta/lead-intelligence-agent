"""Ollama local model integration."""
import httpx
from app.config import settings
from app.providers.base import AIProvider

class OllamaProvider(AIProvider):
    """Generate with Ollama's local chat API."""
    name = "ollama"
    def __init__(self, base_url: str | None = None, model: str | None = None):
        self.base_url = (base_url or settings.ollama_base_url).rstrip("/")
        self.model = model or settings.ollama_model
    async def generate(self, prompt: str) -> str:
        return await self._generate(prompt, "json")
    async def generate_structured(self, prompt: str, schema: dict) -> str:
        """Use Ollama's JSON Schema constrained output to satisfy the validator."""
        return await self._generate(prompt, schema)
    async def _generate(self, prompt: str, response_format) -> str:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            response = await client.post(f"{self.base_url}/api/generate", json={"model": self.model, "prompt": prompt, "stream": False, "format": response_format, "options": {"temperature": 0.2}})
            response.raise_for_status()
            return str(response.json().get("response", ""))
    async def health_check(self) -> dict:
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                response = await client.get(f"{self.base_url}/api/tags")
                response.raise_for_status()
            return {"provider": self.name, "healthy": True, "model": self.model}
        except httpx.HTTPError:
            return {"provider": self.name, "healthy": False, "model": self.model, "error": "Ollama endpoint is unavailable"}
