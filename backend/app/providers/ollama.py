"""Ollama local model integration, with optional shared execution telemetry."""

from __future__ import annotations

import httpx

from app.config import settings
from app.domain.ai import AIProviderException, AIProviderError, AIProviderOutcome, AIRequest, AIResponse, AIRequestMetadata, AIUsage
from app.models.entities import now_utc
from app.providers.ai_http import parse_structured, response_schema, validate_request_schema
from app.providers.base import AIProvider
from app.services.provider_execution import ProviderExecutionError, ProviderExecutionService, ProviderInvalidResponse


class OllamaProvider(AIProvider):
    """Generate using Ollama's local API; Ollama never requires an API key."""

    name = "ollama"

    def __init__(self, base_url: str | None = None, model: str | None = None, *, execution: ProviderExecutionService | None = None, client_factory=None, timeout_seconds: float | None = None):
        self.base_url = (base_url or settings.ollama_base_url).rstrip("/")
        self.model = model or settings.ollama_model
        self.execution = execution
        self.client_factory = client_factory or httpx.AsyncClient
        self.timeout = timeout_seconds if timeout_seconds is not None else getattr(settings, "ai_ollama_timeout_seconds", settings.request_timeout_seconds)

    async def generate(self, prompt: str | AIRequest) -> str | AIResponse:
        if isinstance(prompt, str):
            return await self._generate(prompt, "json")
        return await self._generate_request(prompt)

    async def generate_structured(self, prompt: str, schema: dict) -> str:
        validate_request_schema(AIRequest(user_prompt=prompt, response_schema=schema), self.name)
        return await self._generate(prompt, schema)

    async def _request(self, payload: dict):
        async def send(_credential):
            async with self.client_factory(timeout=self.timeout, follow_redirects=False) as client:
                response = await client.post(f"{self.base_url}/api/generate", json=payload)
            if 200 <= response.status_code < 300:
                try:
                    body = response.json()
                except (ValueError, UnicodeError):
                    raise ProviderInvalidResponse("Ollama returned invalid JSON") from None
                if not isinstance(body, dict) or not isinstance(body.get("response"), str):
                    raise ProviderInvalidResponse("Ollama response was malformed")
            return response

        if self.execution is None:
            async with self.client_factory(timeout=self.timeout, follow_redirects=False) as client:
                response = await client.post(f"{self.base_url}/api/generate", json=payload)
            response.raise_for_status()
            try:
                body = response.json()
            except (ValueError, UnicodeError):
                raise AIProviderException(AIProviderError(provider=self.name, outcome=AIProviderOutcome.MALFORMED_RESPONSE, message="Ollama returned malformed JSON")) from None
            if not isinstance(body, dict) or not isinstance(body.get("response"), str):
                raise AIProviderException(AIProviderError(provider=self.name, outcome=AIProviderOutcome.MALFORMED_RESPONSE, message="Ollama response was malformed"))
            return response, body
        try:
            response = await self.execution.execute(self.name, "generate", send, requires_api_key=False, model=payload["model"])
        except ProviderExecutionError as exc:
            outcome = {"timeout": AIProviderOutcome.TIMEOUT, "rate_limited": AIProviderOutcome.RATE_LIMIT, "authentication": AIProviderOutcome.AUTHENTICATION_FAILURE, "invalid_response": AIProviderOutcome.MALFORMED_RESPONSE, "transient": AIProviderOutcome.PROVIDER_UNAVAILABLE}.get(exc.outcome.value, AIProviderOutcome.PERMANENT)
            raise AIProviderException(AIProviderError(provider=self.name, outcome=outcome, message="Ollama request failed", retryable=outcome in {AIProviderOutcome.TIMEOUT, AIProviderOutcome.RATE_LIMIT, AIProviderOutcome.PROVIDER_UNAVAILABLE}, attempts=exc.attempts, http_status=exc.http_status)) from None
        return response, response.json()

    async def _generate(self, prompt: str, response_format) -> str:
        _, body = await self._request({"model": self.model, "prompt": prompt, "stream": False, "format": response_format, "options": {"temperature": 0.2}})
        return body["response"]

    async def _generate_request(self, request: AIRequest) -> AIResponse:
        validate_request_schema(request, self.name)
        model = request.model or self.model
        started = now_utc()
        payload = {"model": model, "prompt": (request.system_prompt + "\n" if request.system_prompt else "") + request.user_prompt, "stream": False, "options": {"temperature": request.temperature}}
        if request.max_output_tokens is not None:
            payload["options"]["num_predict"] = request.max_output_tokens
        schema = response_schema(request)
        if schema is not None:
            payload["format"] = schema
        response, body = await self._request(payload)
        content = body["response"]
        parsed = parse_structured(content, request, self.name)
        prompt_tokens, output_tokens = body.get("prompt_eval_count"), body.get("eval_count")
        valid_prompt = prompt_tokens if isinstance(prompt_tokens, int) and prompt_tokens >= 0 else None
        valid_output = output_tokens if isinstance(output_tokens, int) and output_tokens >= 0 else None
        usage = AIUsage(input_tokens=valid_prompt, output_tokens=valid_output, total_tokens=valid_prompt + valid_output if valid_prompt is not None and valid_output is not None else None)
        return AIResponse(provider=self.name, model=model, content=content, parsed_json=parsed, usage=usage, request_metadata=AIRequestMetadata(operation="generate", started_at=started, completed_at=now_utc(), response_status=response.status_code))

    async def health_check(self) -> dict:
        try:
            async with self.client_factory(timeout=3, follow_redirects=False) as client:
                response = await client.get(f"{self.base_url}/api/tags")
                response.raise_for_status()
                payload = response.json()
            models = payload.get("models", []) if isinstance(payload, dict) else []
            available = any(
                isinstance(item, dict) and self.model in {item.get("name"), item.get("model")}
                for item in models
            )
            return {"provider": self.name, "healthy": True, "model": self.model, "model_available": available}
        except (httpx.HTTPError, ValueError):
            return {"provider": self.name, "healthy": False, "model": self.model, "error": "Ollama endpoint is unavailable"}
