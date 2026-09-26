"""Shared structured-output and safe HTTP execution helpers for AI adapters."""

from __future__ import annotations

import json
import ipaddress
import re
import time
from datetime import date, datetime, time as time_value
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import httpx
from email_validator import EmailNotValidError, validate_email
from pydantic import BaseModel, ValidationError

from app.domain.ai import (
    AIProviderException,
    AIProviderError,
    AIProviderOutcome,
    AIRequest,
    AIResponse,
    AIRequestMetadata,
    AIUsage,
)
from app.models.entities import now_utc
from app.services.provider_execution import (
    ProviderExecutionError,
    ProviderInvalidResponse,
    ProviderExecutionService,
    RequestOutcome,
)


def response_schema(request: AIRequest) -> dict | None:
    schema = request.response_schema
    if schema is None:
        return None
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        return schema.model_json_schema()
    return schema


def _type_matches(value: Any, expected: str) -> bool:
    if expected == "object": return isinstance(value, dict)
    if expected == "array": return isinstance(value, list)
    if expected == "string": return isinstance(value, str)
    if expected == "integer": return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number": return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean": return isinstance(value, bool)
    if expected == "null": return value is None
    return False


def _json_schema_equal(left: Any, right: Any) -> bool:
    """Compare JSON values without Python's bool-is-an-int equality quirk."""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return Decimal(str(left)) == Decimal(str(right))
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_json_schema_equal(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_json_schema_equal(a, b) for a, b in zip(left, right))
    return type(left) is type(right) and left == right


class UnsupportedJSONSchema(ValueError):
    """The supplied schema uses a keyword or reference we do not implement."""


_SCHEMA_ANNOTATIONS = {"$schema", "$id", "title", "description", "default", "examples", "deprecated", "readOnly", "writeOnly"}
_SUPPORTED_SCHEMA_KEYWORDS = _SCHEMA_ANNOTATIONS | {
    "$defs", "definitions", "$ref", "type", "properties", "required", "items", "enum", "const",
    "additionalProperties", "nullable", "anyOf", "oneOf", "allOf", "minLength", "maxLength", "pattern",
    "format", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minItems",
    "maxItems", "uniqueItems", "minProperties", "maxProperties",
}
_SUPPORTED_FORMATS = {"date-time", "date", "time", "email", "hostname", "ipv4", "ipv6", "uri", "uuid"}
_JSON_TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}


def resolve_schema_reference(reference: str, root: dict | bool) -> dict | bool:
    if not reference.startswith("#/"):
        raise UnsupportedJSONSchema("Only local JSON Schema references are supported")
    target: Any = root
    for raw_part in reference[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(target, dict) or part not in target:
            raise UnsupportedJSONSchema("JSON Schema reference could not be resolved")
        target = target[part]
    if not isinstance(target, (dict, bool)):
        raise UnsupportedJSONSchema("JSON Schema reference does not point to a schema")
    return target


def validate_schema_support(schema: dict | bool) -> None:
    """Reject schema constructs not enforced by the local validator."""
    root = schema
    visited_refs: set[str] = set()
    try:
        json.dumps(schema, allow_nan=False)
    except (TypeError, ValueError):
        raise UnsupportedJSONSchema("JSON Schema must be valid JSON data") from None

    def visit(node: Any) -> None:
        if isinstance(node, bool):
            return
        if not isinstance(node, dict):
            raise UnsupportedJSONSchema("JSON Schema nodes must be objects or booleans")
        unsupported = set(node) - _SUPPORTED_SCHEMA_KEYWORDS
        if unsupported:
            raise UnsupportedJSONSchema("Unsupported JSON Schema keyword")
        dialect = node.get("$schema")
        if dialect is not None and (not isinstance(dialect, str) or not any(value in dialect for value in ("2019-09", "2020-12"))):
            raise UnsupportedJSONSchema("Unsupported JSON Schema dialect")
        if "$ref" in node:
            reference = node["$ref"]
            if not isinstance(reference, str):
                raise UnsupportedJSONSchema("JSON Schema reference must be a string")
            target = resolve_schema_reference(reference, root)
            if reference not in visited_refs:
                visited_refs.add(reference)
                visit(target)
        if "type" in node:
            types = node["type"] if isinstance(node["type"], list) else [node["type"]]
            if not types or any(not isinstance(item, str) or item not in _JSON_TYPES for item in types):
                raise UnsupportedJSONSchema("Unsupported JSON Schema type")
        if "format" in node and (not isinstance(node["format"], str) or node["format"] not in _SUPPORTED_FORMATS):
            raise UnsupportedJSONSchema("Unsupported JSON Schema format")
        if "pattern" in node:
            if not isinstance(node["pattern"], str):
                raise UnsupportedJSONSchema("JSON Schema pattern must be a string")
            try:
                if re.search(r"\(\?(?:P[<=!]|\(|[aiLmsux-])|\\[AZGKbB]|\\g[<{]", node["pattern"]):
                    raise UnsupportedJSONSchema("JSON Schema pattern uses unsupported regular-expression syntax")
                re.compile(node["pattern"])
            except re.error:
                raise UnsupportedJSONSchema("JSON Schema pattern is invalid") from None
        if "properties" in node:
            if not isinstance(node["properties"], dict):
                raise UnsupportedJSONSchema("JSON Schema properties must be an object")
            for child in node["properties"].values():
                visit(child)
        for key in ("$defs", "definitions"):
            if key in node:
                if not isinstance(node[key], dict):
                    raise UnsupportedJSONSchema("JSON Schema definitions must be an object")
                for child in node[key].values():
                    visit(child)
        for key in ("items", "additionalProperties"):
            if isinstance(node.get(key), (dict, bool)):
                visit(node[key])
            elif key == "items" and key in node:
                raise UnsupportedJSONSchema("Tuple-style items schemas are unsupported")
            elif key == "additionalProperties" and key in node:
                raise UnsupportedJSONSchema("additionalProperties must be a boolean or schema")
        for key in ("anyOf", "oneOf", "allOf"):
            if key in node:
                branches = node[key]
                if not isinstance(branches, list) or not branches:
                    raise UnsupportedJSONSchema(f"JSON Schema {key} must be a non-empty array")
                for branch in branches:
                    visit(branch)
        required = node.get("required", [])
        if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
            raise UnsupportedJSONSchema("JSON Schema required must be an array of strings")
        enum = node.get("enum")
        if enum is not None and not isinstance(enum, list):
            raise UnsupportedJSONSchema("JSON Schema enum must be an array")
        for key in ("minLength", "maxLength", "minItems", "maxItems", "minProperties", "maxProperties"):
            if key in node and (not isinstance(node[key], int) or isinstance(node[key], bool) or node[key] < 0):
                raise UnsupportedJSONSchema(f"JSON Schema {key} must be a non-negative integer")
        for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"):
            if key in node and (not isinstance(node[key], (int, float)) or isinstance(node[key], bool)):
                raise UnsupportedJSONSchema(f"JSON Schema {key} must be numeric")
        if node.get("multipleOf") == 0:
            raise UnsupportedJSONSchema("JSON Schema multipleOf must be greater than zero")
        for key in ("nullable", "uniqueItems"):
            if key in node and not isinstance(node[key], bool):
                raise UnsupportedJSONSchema(f"JSON Schema {key} must be boolean")
    visit(schema)


def _format_matches(value: str, format_name: str) -> bool:
    try:
        if format_name == "date-time":
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value) is None:
                return False
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        elif format_name == "date":
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
                return False
            date.fromisoformat(value)
        elif format_name == "time":
            if re.fullmatch(r"\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value) is None:
                return False
            time_value.fromisoformat(value.replace("Z", "+00:00"))
        elif format_name == "email":
            validate_email(value, check_deliverability=False)
        elif format_name == "hostname":
            labels = value.split(".")
            return bool(len(value) <= 253 and all(len(label) <= 63 for label in labels) and re.fullmatch(r"(?=.{1,253}$)(?!-)[A-Za-z0-9-]+(?<!-)(\.(?!-)[A-Za-z0-9-]+(?<!-))*", value))
        elif format_name in {"ipv4", "ipv6"}:
            return ipaddress.ip_address(value).version == (4 if format_name == "ipv4" else 6)
        elif format_name == "uri":
            parsed = urlparse(value)
            return bool(parsed.scheme and not re.search(r"\s|%(?![0-9A-Fa-f]{2})", value) and (not value.startswith(parsed.scheme + "://") or parsed.hostname))
        elif format_name == "uuid":
            if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", value) is None:
                return False
            UUID(value)
        return True
    except (ValueError, TypeError, OverflowError, EmailNotValidError):
        return False


def _validate_schema(value: Any, schema: dict | bool, path: str = "$", root: dict | bool | None = None) -> None:
    root = schema if root is None else root
    if schema is False:
        raise ValueError(f"{path} is disallowed")
    if schema is True:
        return
    if "$ref" in schema:
        _validate_schema(value, resolve_schema_reference(schema["$ref"], root), path, root)
        siblings = {key: child for key, child in schema.items() if key != "$ref"}
        if siblings:
            _validate_schema(value, siblings, path, root)
        return
    if schema.get("nullable") and value is None:
        return
    for keyword in ("allOf", "anyOf", "oneOf"):
        if keyword not in schema:
            continue
        successes = 0
        for branch in schema[keyword]:
            try:
                _validate_schema(value, branch, path, root)
                successes += 1
            except ValueError:
                pass
        if (keyword == "allOf" and successes != len(schema[keyword])) or (keyword == "anyOf" and successes < 1) or (keyword == "oneOf" and successes != 1):
            raise ValueError(f"{path} does not satisfy {keyword}")
    if "const" in schema and not _json_schema_equal(value, schema["const"]):
        raise ValueError(f"{path} is not the required constant")
    expected = schema.get("type")
    types = expected if isinstance(expected, list) else [expected] if expected else []
    if types and not any(_type_matches(value, item) for item in types):
        raise ValueError(f"{path} has an invalid type")
    if "enum" in schema and not any(_json_schema_equal(value, allowed) for allowed in schema["enum"]):
        raise ValueError(f"{path} is outside the allowed values")
    if isinstance(value, dict):
        required = schema.get("required", [])
        if any(key not in value for key in required):
            raise ValueError(f"{path} is missing required fields")
        properties = schema.get("properties", {})
        if len(value) < schema.get("minProperties", 0) or len(value) > schema.get("maxProperties", float("inf")):
            raise ValueError(f"{path} has an invalid number of properties")
        for key, child_value in value.items():
            if key in properties:
                _validate_schema(child_value, properties[key], f"{path}.{key}", root)
            elif schema.get("additionalProperties") is False:
                raise ValueError(f"{path} contains an unsupported property")
            elif isinstance(schema.get("additionalProperties"), (dict, bool)):
                _validate_schema(child_value, schema["additionalProperties"], f"{path}.{key}", root)
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", float("inf")):
            raise ValueError(f"{path} has an invalid number of items")
        if schema.get("uniqueItems") and any(_json_schema_equal(value[left], value[right]) for left in range(len(value)) for right in range(left + 1, len(value))):
            raise ValueError(f"{path} contains duplicate items")
        if isinstance(schema.get("items"), (dict, bool)):
            for index, item in enumerate(value):
                _validate_schema(item, schema["items"], f"{path}[{index}]", root)
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", float("inf")):
            raise ValueError(f"{path} has an invalid length")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            raise ValueError(f"{path} does not match its pattern")
        if "format" in schema and not _format_matches(value, schema["format"]):
            raise ValueError(f"{path} has an invalid format")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ValueError(f"{path} is below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValueError(f"{path} is above maximum")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            raise ValueError(f"{path} is not above exclusiveMinimum")
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            raise ValueError(f"{path} is not below exclusiveMaximum")
        if "multipleOf" in schema:
            try:
                if Decimal(str(value)) % Decimal(str(schema["multipleOf"])) != 0:
                    raise ValueError(f"{path} is not a multipleOf value")
            except (InvalidOperation, ZeroDivisionError):
                raise ValueError(f"{path} is not a multipleOf value") from None


def validate_request_schema(request: AIRequest, provider: str) -> None:
    schema = response_schema(request)
    if schema is None:
        return
    try:
        validate_schema_support(schema)
    except (UnsupportedJSONSchema, RecursionError):
        raise AIProviderException(AIProviderError(provider=provider, outcome=AIProviderOutcome.UNSUPPORTED_CAPABILITY, message="Requested JSON Schema contains unsupported constraints")) from None


def parse_structured(content: str, request: AIRequest, provider: str = "ai_provider") -> Any:
    """Parse JSON and validate it against Pydantic or the enforced JSON Schema subset."""
    if request.response_schema is None:
        return None
    validate_request_schema(request, provider)
    def reject_constant(_value):
        raise ValueError("invalid JSON constant")

    def reject_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    try:
        parsed = json.loads(content, parse_constant=reject_constant, object_pairs_hook=reject_duplicate_keys)
        if isinstance(request.response_schema, type) and issubclass(request.response_schema, BaseModel):
            return request.response_schema.model_validate(parsed).model_dump(mode="json")
        _validate_schema(parsed, request.response_schema)
        return parsed
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError, RecursionError):
        raise AIProviderException(
            error=AIProviderError(
                provider=provider,
                outcome=AIProviderOutcome.INVALID_STRUCTURED_OUTPUT,
                message="Provider output did not satisfy the requested structured schema",
            )
        ) from None


def provider_error(provider: str, error: ProviderExecutionError, *, structured: bool = False) -> AIProviderException:
    outcome_map = {
        RequestOutcome.TRANSIENT: AIProviderOutcome.PROVIDER_UNAVAILABLE,
        RequestOutcome.TIMEOUT: AIProviderOutcome.TIMEOUT,
        RequestOutcome.RATE_LIMITED: AIProviderOutcome.RATE_LIMIT,
        RequestOutcome.AUTHENTICATION: AIProviderOutcome.AUTHENTICATION_FAILURE,
        RequestOutcome.QUOTA_EXHAUSTED: AIProviderOutcome.QUOTA_EXHAUSTED,
        RequestOutcome.INVALID_RESPONSE: AIProviderOutcome.MALFORMED_RESPONSE,
        RequestOutcome.PERMANENT: AIProviderOutcome.UNSUPPORTED_CAPABILITY if structured and error.http_status == 400 else AIProviderOutcome.PERMANENT,
    }
    outcome = AIProviderOutcome.KEY_POOL_EXHAUSTED if error.attempts == 0 else outcome_map[error.outcome]
    messages = {
        AIProviderOutcome.KEY_POOL_EXHAUSTED: "No available credential is configured for this provider",
        AIProviderOutcome.PROVIDER_UNAVAILABLE: "AI provider is unavailable",
        AIProviderOutcome.AUTHENTICATION_FAILURE: "AI provider authentication failed",
        AIProviderOutcome.RATE_LIMIT: "AI provider rate limit was reached",
        AIProviderOutcome.TIMEOUT: "AI provider request timed out",
        AIProviderOutcome.MALFORMED_RESPONSE: "AI provider returned a malformed response",
        AIProviderOutcome.UNSUPPORTED_CAPABILITY: "AI provider rejected the requested structured-output capability",
        AIProviderOutcome.QUOTA_EXHAUSTED: "AI provider quota is exhausted",
        AIProviderOutcome.PERMANENT: "AI provider rejected the request",
    }
    return AIProviderException(
        AIProviderError(
            provider=provider,
            outcome=outcome,
            message=messages[outcome],
            retryable=outcome in {AIProviderOutcome.PROVIDER_UNAVAILABLE, AIProviderOutcome.RATE_LIMIT, AIProviderOutcome.TIMEOUT},
            http_status=error.http_status,
            attempts=error.attempts,
            metadata={"operation": error.operation, "retry_after_seconds": error.retry_after_seconds},
        )
    )


class StructuredHttpAIProvider:
    """Base for fixed-endpoint AI adapters; all HTTP calls pass through execution."""

    name = "ai_http"

    def __init__(self, execution: ProviderExecutionService, *, model: str, base_url: str, timeout_seconds: float, operation: str, client_factory=httpx.AsyncClient):
        self.execution = execution
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.operation = operation
        self.client_factory = client_factory

    def _url(self) -> str:
        raise NotImplementedError

    def _payload(self, request: AIRequest) -> dict:
        raise NotImplementedError

    def _headers(self, credential: str) -> dict[str, str]:
        raise NotImplementedError

    def _extract_payload(self, payload: dict) -> tuple[str, AIUsage, str | None]:
        raise NotImplementedError

    def _validate_request_capability(self, request: AIRequest) -> None:
        validate_request_schema(request, self.name)

    async def _generate_request(self, request: AIRequest, *, validate_structured: bool = True) -> AIResponse:
        model = request.model or self.model
        actual_request = request.model_copy(update={"model": model})
        schema = response_schema(actual_request)
        self._validate_request_capability(actual_request)
        started_at = now_utc()
        timer = time.perf_counter()

        async def send(credential: str | None):
            if credential is None:
                raise ProviderInvalidResponse("Provider execution did not supply a credential")
            async with self.client_factory(timeout=self.timeout_seconds, follow_redirects=False) as client:
                response = await client.post(self._url_for_model(model), json=self._payload(actual_request), headers=self._headers(credential))
            if response.status_code < 200 or response.status_code >= 300:
                return response
            try:
                body = response.json()
            except (ValueError, UnicodeError):
                raise ProviderInvalidResponse("Provider returned invalid JSON") from None
            if not isinstance(body, dict):
                raise ProviderInvalidResponse("Provider response was not a JSON object")
            content, usage, response_model = self._extract_payload(body)
            return response.status_code, response.headers.get("x-request-id") or response.headers.get("request-id"), content, usage, response_model

        try:
            result = await self.execution.execute(
                self.name,
                self.operation,
                send,
                requires_api_key=True,
                model=model,
            )
        except ProviderExecutionError as exc:
            raise provider_error(self.name, exc, structured=schema is not None) from None
        status, request_id, content, usage, response_model = result
        parsed = parse_structured(content, actual_request, self.name) if validate_structured else None
        completed_at = now_utc()
        return AIResponse(
            provider=self.name,
            model=response_model or model,
            content=content,
            parsed_json=parsed,
            usage=usage,
            request_metadata=AIRequestMetadata(
                operation=self.operation,
                request_id=request_id,
                duration_ms=int((time.perf_counter() - timer) * 1000),
                started_at=started_at,
                completed_at=completed_at,
                response_status=status,
            ),
        )

    async def generate_structured(self, prompt: str, schema: dict) -> str:
        """Legacy adapter entry point; return raw text for the legacy validator/retry loop."""
        response = await self._generate_request(AIRequest(user_prompt=prompt, response_schema=schema), validate_structured=False)
        return response.content

    def _url_for_model(self, model: str) -> str:
        return self._url()
