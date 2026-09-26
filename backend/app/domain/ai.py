"""Provider-neutral contracts for text generation and structured AI output."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AIContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)


class AIRequest(AIContract):
    model: Annotated[str | None, Field(min_length=1, max_length=200)] = None
    system_prompt: Annotated[str, Field(max_length=100_000)] = ""
    user_prompt: Annotated[str, Field(min_length=1, max_length=1_000_000)]
    temperature: Annotated[float, Field(ge=0, le=2)] = 0.2
    max_output_tokens: Annotated[int | None, Field(gt=0, le=1_000_000)] = None
    response_schema: dict[str, Any] | type[BaseModel] | None = None
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def metadata_must_not_contain_secrets(cls, value):
        sensitive = ("key", "secret", "token", "authorization", "password", "credential")
        if any(any(part in name.lower() for part in sensitive) for name in value):
            raise ValueError("request metadata cannot contain credentials or secrets")
        return value

    @field_validator("response_schema")
    @classmethod
    def schema_must_be_object_or_model(cls, value):
        if value is not None and not isinstance(value, dict) and not (isinstance(value, type) and issubclass(value, BaseModel)):
            raise ValueError("response_schema must be a JSON Schema object or Pydantic model class")
        return value


class AIUsage(AIContract):
    input_tokens: Annotated[int | None, Field(ge=0)] = None
    output_tokens: Annotated[int | None, Field(ge=0)] = None
    total_tokens: Annotated[int | None, Field(ge=0)] = None


class AIRequestMetadata(AIContract):
    operation: Annotated[str, Field(min_length=1, max_length=120)]
    request_id: Annotated[str | None, Field(max_length=200)] = None
    duration_ms: Annotated[int | None, Field(ge=0)] = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    response_status: Annotated[int | None, Field(ge=100, le=599)] = None


class AIResponse(AIContract):
    provider: Annotated[str, Field(min_length=1, max_length=80)]
    model: Annotated[str, Field(min_length=1, max_length=200)]
    content: str
    parsed_json: Any | None = None
    usage: AIUsage = Field(default_factory=AIUsage)
    request_metadata: AIRequestMetadata


class AIProviderOutcome(StrEnum):
    PROVIDER_DISABLED = "provider_disabled"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    AUTHENTICATION_FAILURE = "authentication_failure"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    MALFORMED_RESPONSE = "malformed_response"
    INVALID_STRUCTURED_OUTPUT = "invalid_structured_output"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    KEY_POOL_EXHAUSTED = "key_pool_exhausted"
    QUOTA_EXHAUSTED = "quota_exhausted"
    PERMANENT = "permanent"


class AIProviderError(AIContract):
    provider: Annotated[str, Field(min_length=1, max_length=80)]
    outcome: AIProviderOutcome
    message: Annotated[str, Field(min_length=1, max_length=500)]
    retryable: bool = False
    request_id: Annotated[str | None, Field(max_length=200)] = None
    http_status: Annotated[int | None, Field(ge=100, le=599)] = None
    attempts: Annotated[int | None, Field(ge=0)] = None
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class AIProviderException(RuntimeError):
    """Safe exception wrapper; only the sanitized typed error is exposed."""

    def __init__(self, error: AIProviderError):
        self.error = error
        super().__init__(error.message)


class AIProviderSelectionError(ValueError):
    """Typed provider selection/configuration error; ValueError-compatible for callers."""

    def __init__(self, outcome: AIProviderOutcome, message: str, provider: str | None = None):
        self.outcome = outcome
        self.provider = provider
        super().__init__(message)


@runtime_checkable
class AIProvider(Protocol):
    """Replaceable generation interface implemented by local and cloud adapters."""

    name: str
    model: str

    async def generate(self, request: AIRequest) -> AIResponse:
        ...
