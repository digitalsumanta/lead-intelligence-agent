"""Generic OpenAI-compatible chat-completions adapter."""

from __future__ import annotations

from app.config import settings
from app.domain.ai import AIProviderException, AIProviderError, AIProviderOutcome, AIRequest, AIResponse, AIUsage
from app.providers.base import AIProvider
from app.providers.ai_http import StructuredHttpAIProvider, response_schema
from app.services.provider_execution import ProviderExecutionService, ProviderInvalidResponse


class OpenAICompatibleProvider(StructuredHttpAIProvider, AIProvider):
    name = "openai_compatible"

    def __init__(
        self,
        execution: ProviderExecutionService,
        *,
        model: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
        enabled: bool | None = None,
        client_factory=None,
    ):
        super().__init__(
            execution,
            model=model or settings.ai_openai_compatible_model,
            base_url=base_url or settings.ai_openai_compatible_base_url,
            timeout_seconds=timeout_seconds if timeout_seconds is not None else settings.ai_openai_compatible_timeout_seconds,
            operation="chat_completions",
            **({"client_factory": client_factory} if client_factory else {}),
        )
        self.enabled = settings.ai_openai_compatible_enabled if enabled is None else enabled

    def _url(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _payload(self, request: AIRequest) -> dict:
        messages = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        messages.append({"role": "user", "content": request.user_prompt})
        payload = {"model": request.model or self.model, "messages": messages, "temperature": request.temperature}
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens
        schema = response_schema(request)
        if schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "structured_response", "strict": True, "schema": schema},
            }
        return payload

    def _headers(self, credential: str) -> dict[str, str]:
        return {"content-type": "application/json", "authorization": f"Bearer {credential}"}

    def _extract_payload(self, payload: dict) -> tuple[str, AIUsage, str | None]:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ProviderInvalidResponse("OpenAI-compatible response has no choices")
        message = choices[0].get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            content = "".join(item.get("text", "") for item in content if isinstance(item, dict) and isinstance(item.get("text"), str))
        if not isinstance(content, str):
            raise ProviderInvalidResponse("OpenAI-compatible response has no text content")
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        return content, AIUsage(
            input_tokens=self._nonnegative_int(usage.get("prompt_tokens")),
            output_tokens=self._nonnegative_int(usage.get("completion_tokens")),
            total_tokens=self._nonnegative_int(usage.get("total_tokens")),
        ), payload.get("model") if isinstance(payload.get("model"), str) else None

    @staticmethod
    def _nonnegative_int(value):
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    async def generate(self, request: AIRequest | str) -> AIResponse | str:
        if not self.enabled:
            raise AIProviderException(AIProviderError(provider=self.name, outcome=AIProviderOutcome.PROVIDER_DISABLED, message="OpenAI-compatible provider is disabled"))
        if isinstance(request, str):
            response = await self._generate_request(AIRequest(user_prompt=request), validate_structured=False)
            return response.content
        return await self._generate_request(request)

    async def health_check(self) -> dict:
        return {"provider": self.name, "healthy": self.enabled, "model": self.model, "configured": bool(self.base_url and self.model)}
