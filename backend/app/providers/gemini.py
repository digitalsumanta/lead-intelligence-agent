"""Google Gemini HTTP adapter using ProviderExecutionService for every request."""

from __future__ import annotations

from urllib.parse import quote

from app.config import settings
from app.domain.ai import AIProviderException, AIProviderOutcome, AIRequest, AIResponse, AIUsage
from app.providers.base import AIProvider
from app.providers.ai_http import StructuredHttpAIProvider, resolve_schema_reference, validate_request_schema
from app.services.provider_execution import ProviderExecutionService, ProviderInvalidResponse


class GeminiProvider(StructuredHttpAIProvider, AIProvider):
    name = "gemini"

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
            model=model or settings.ai_gemini_model,
            base_url=base_url or settings.ai_gemini_base_url,
            timeout_seconds=timeout_seconds if timeout_seconds is not None else settings.ai_gemini_timeout_seconds,
            operation="generate_content",
            **({"client_factory": client_factory} if client_factory else {}),
        )
        self.enabled = settings.ai_gemini_enabled if enabled is None else enabled

    def _url_for_model(self, model: str) -> str:
        model_path = model.removeprefix("models/")
        return f"{self.base_url}/models/{quote(model_path, safe='')}:generateContent"

    def _url(self) -> str:
        return self._url_for_model(self.model)

    def _payload(self, request: AIRequest) -> dict:
        body = {
            "contents": [{"role": "user", "parts": [{"text": request.user_prompt}]}],
            "generationConfig": {"temperature": request.temperature},
        }
        if request.system_prompt:
            body["systemInstruction"] = {"parts": [{"text": request.system_prompt}]}
        if request.max_output_tokens is not None:
            body["generationConfig"]["maxOutputTokens"] = request.max_output_tokens
        schema = self._schema_for(request)
        if schema is not None:
            body["generationConfig"].update({"responseMimeType": "application/json", "responseSchema": schema})
        return body

    def _validate_request_capability(self, request: AIRequest) -> None:
        validate_request_schema(request, self.name)
        self._schema_for(request)

    @staticmethod
    def _schema_for(request):
        from app.providers.ai_http import response_schema
        schema = response_schema(request)
        if schema is None:
            return None
        def convert(node):
            if isinstance(node, bool) or not isinstance(node, dict):
                raise ValueError("Gemini does not support boolean schema nodes")
            if "$ref" in node:
                return convert(resolve_schema_reference(node["$ref"], schema))
            if "allOf" in node:
                raise ValueError("Gemini does not support allOf response schemas")
            if "anyOf" in node or "oneOf" in node:
                if "anyOf" in node and "oneOf" in node:
                    raise ValueError("Gemini does not support combined response schema unions")
                alternatives = node.get("anyOf", node.get("oneOf"))
                if not isinstance(alternatives, list) or any(not isinstance(alternative, dict) for alternative in alternatives):
                    raise ValueError("Gemini does not support boolean union branches")
                non_null = [alternative for alternative in alternatives if alternative.get("type") != "null"]
                if len(non_null) != 1 or ("oneOf" in node and len(alternatives) != 2):
                    raise ValueError("Gemini structured schema contains an unsupported union")
                converted = convert(non_null[0])
                converted["nullable"] = len(non_null) != len(alternatives)
                return converted
            result = {}
            for key in ("type", "format", "description", "enum", "required"):
                if key in node:
                    result[key] = node[key]
            if isinstance(node.get("type"), list):
                types = [item for item in node["type"] if item != "null"]
                if len(types) != 1:
                    raise ValueError("Gemini structured schema contains an unsupported type union")
                result["type"] = types[0]
                result["nullable"] = len(types) != len(node["type"])
            if isinstance(node.get("properties"), dict):
                result["properties"] = {name: convert(value) for name, value in node["properties"].items()}
            if isinstance(node.get("items"), dict):
                result["items"] = convert(node["items"])
            return result

        try:
            return convert(schema)
        except (ValueError, TypeError, RecursionError):
            from app.domain.ai import AIProviderError
            raise AIProviderException(AIProviderError(provider="gemini", outcome=AIProviderOutcome.UNSUPPORTED_CAPABILITY, message="Gemini cannot represent the requested response schema")) from None

    def _headers(self, credential: str) -> dict[str, str]:
        return {"content-type": "application/json", "x-goog-api-key": credential}

    def _extract_payload(self, payload: dict) -> tuple[str, AIUsage, str | None]:
        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
            raise ProviderInvalidResponse("Gemini returned no candidate")
        content = candidates[0].get("content")
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list):
            raise ProviderInvalidResponse("Gemini returned no content parts")
        text_parts = [part.get("text") for part in parts if isinstance(part, dict) and isinstance(part.get("text"), str)]
        if not text_parts:
            raise ProviderInvalidResponse("Gemini returned no text content")
        usage = payload.get("usageMetadata") if isinstance(payload.get("usageMetadata"), dict) else {}
        return "".join(text_parts), AIUsage(
            input_tokens=self._positive_int(usage.get("promptTokenCount")),
            output_tokens=self._positive_int(usage.get("candidatesTokenCount")),
            total_tokens=self._positive_int(usage.get("totalTokenCount")),
        ), payload.get("modelVersion") if isinstance(payload.get("modelVersion"), str) else None

    @staticmethod
    def _positive_int(value):
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    async def generate(self, request: AIRequest | str) -> AIResponse | str:
        if not self.enabled:
            from app.domain.ai import AIProviderError
            raise AIProviderException(AIProviderError(provider=self.name, outcome=AIProviderOutcome.PROVIDER_DISABLED, message="Gemini provider is disabled"))
        if isinstance(request, str):
            response = await self._generate_request(AIRequest(user_prompt=request), validate_structured=False)
            return response.content
        return await self._generate_request(request)

    async def health_check(self) -> dict:
        return {"provider": self.name, "healthy": self.enabled, "model": self.model, "configured": bool(self.base_url and self.model)}
