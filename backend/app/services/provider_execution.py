"""Provider-neutral request execution, classification, retry, and telemetry."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from enum import StrEnum
import json
import logging
import random
import time
from typing import Any, Awaitable, Callable, TypeVar

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.domain.enrichment import ProviderErrorType
from app.models import ProviderUsage
from app.models.entities import now_utc
from app.services.key_pool import ApiKeyLease, ApiKeyPool


logger = logging.getLogger("lead_agent.provider_execution")
T = TypeVar("T")


class RequestOutcome(StrEnum):
    SUCCESS = "success"
    TRANSIENT = "transient"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    AUTHENTICATION = "authentication"
    QUOTA_EXHAUSTED = "quota_exhausted"
    INVALID_RESPONSE = "invalid_response"
    PERMANENT = "permanent"


class QuotaScope(StrEnum):
    """Which shared unit owns quota/cooldown state for this operation."""

    KEY = "key"
    PROVIDER_OPERATION = "provider_operation"


_OUTCOME_TO_ERROR = {
    RequestOutcome.TRANSIENT: ProviderErrorType.TRANSIENT,
    RequestOutcome.TIMEOUT: ProviderErrorType.TIMEOUT,
    RequestOutcome.RATE_LIMITED: ProviderErrorType.RATE_LIMITED,
    RequestOutcome.AUTHENTICATION: ProviderErrorType.AUTHENTICATION,
    RequestOutcome.QUOTA_EXHAUSTED: ProviderErrorType.QUOTA_EXHAUSTED,
    RequestOutcome.INVALID_RESPONSE: ProviderErrorType.INVALID_RESPONSE,
    RequestOutcome.PERMANENT: ProviderErrorType.PERMANENT,
}


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded request-level retry and local provider concurrency settings."""

    max_retries: int = settings.provider_max_retries
    backoff_base_seconds: float = settings.provider_backoff_base_seconds
    backoff_max_seconds: float = settings.provider_backoff_max_seconds
    jitter_seconds: float = settings.provider_retry_jitter_seconds
    max_concurrency: int = settings.provider_max_concurrency
    key_lease_seconds: int = settings.provider_key_lease_seconds
    failure_cooldown_seconds: float = settings.provider_failure_cooldown_seconds
    rate_limit_cooldown_seconds: int = settings.provider_rate_limit_cooldown_seconds
    quota_cooldown_seconds: int = settings.provider_quota_cooldown_seconds

    def __post_init__(self):
        if self.max_retries < 0 or self.max_concurrency < 1 or self.key_lease_seconds < 1:
            raise ValueError("retry count, concurrency, and key lease settings are out of range")
        if min(
            self.backoff_base_seconds,
            self.backoff_max_seconds,
            self.jitter_seconds,
            self.failure_cooldown_seconds,
            self.rate_limit_cooldown_seconds,
            self.quota_cooldown_seconds,
        ) < 0:
            raise ValueError("retry and cooldown durations cannot be negative")


class ProviderExecutionError(RuntimeError):
    """Sanitized terminal error that never includes the original exception."""

    def __init__(self, provider: str, operation: str, outcome: RequestOutcome, attempts: int, http_status: int | None = None, retry_after_seconds: float | None = None):
        self.provider = provider
        self.operation = operation
        self.outcome = outcome
        self.attempts = attempts
        self.http_status = http_status
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"Provider request failed: provider={provider}, operation={operation}, outcome={outcome.value}")


@dataclass(frozen=True)
class _ResponseInfo:
    outcome: RequestOutcome
    http_status: int | None = None
    retry_after_seconds: float | None = None
    rate_limit_limit: int | None = None
    rate_limit_remaining: int | None = None
    rate_limit_reset_at: datetime | None = None


def _parse_seconds_or_date(value: str | None) -> tuple[float | None, datetime | None]:
    if not value:
        return None, None
    try:
        seconds = max(0.0, float(value))
        return seconds, now_utc() + timedelta(seconds=seconds)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, (parsed - now_utc()).total_seconds()), parsed
        except (TypeError, ValueError, OverflowError):
            return None, None


def _as_int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _parse_reset_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        numeric = float(value)
        if numeric > 100_000_000:
            return datetime.fromtimestamp(numeric, tz=timezone.utc)
        return now_utc() + timedelta(seconds=max(0.0, numeric))
    except (ValueError, OverflowError, OSError):
        try:
            parsed = parsedate_to_datetime(value)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError, OverflowError):
            return None


def _classify_http(status: int, headers, body: str = "") -> _ResponseInfo:
    lower_body = body[:4096].lower()
    quota_markers = ("quota_exceeded", "quota exceeded", "resource_exhausted", "billing limit", "daily limit")
    retry_after, retry_reset = _parse_seconds_or_date(headers.get("Retry-After"))
    reset_at = _parse_reset_at(headers.get("X-RateLimit-Reset"))
    if reset_at is None:
        reset_at = retry_reset

    if 200 <= status < 300:
        outcome = RequestOutcome.SUCCESS
    elif status in {401, 403}:
        outcome = RequestOutcome.QUOTA_EXHAUSTED if any(marker in lower_body for marker in quota_markers) else RequestOutcome.AUTHENTICATION
    elif status == 402 or (status == 429 and any(marker in lower_body for marker in quota_markers)):
        outcome = RequestOutcome.QUOTA_EXHAUSTED
    elif status == 429 or status == 425:
        outcome = RequestOutcome.RATE_LIMITED
    elif status == 408:
        outcome = RequestOutcome.TIMEOUT
    elif 500 <= status <= 599:
        outcome = RequestOutcome.TRANSIENT
    elif status >= 400:
        outcome = RequestOutcome.PERMANENT
    else:
        outcome = RequestOutcome.PERMANENT
    return _ResponseInfo(
        outcome=outcome,
        http_status=status,
        retry_after_seconds=retry_after,
        rate_limit_limit=_as_int(headers.get("X-RateLimit-Limit")),
        rate_limit_remaining=_as_int(headers.get("X-RateLimit-Remaining")),
        rate_limit_reset_at=reset_at,
    )


def _exception_info(exc: Exception) -> _ResponseInfo:
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        try:
            body = response.text
        except (UnicodeError, httpx.ResponseNotRead):
            body = ""
        return _classify_http(response.status_code, response.headers, body)
    if isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError, TimeoutError)):
        return _ResponseInfo(RequestOutcome.TIMEOUT)
    if isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError, httpx.DecodingError)):
        return _ResponseInfo(RequestOutcome.INVALID_RESPONSE)
    if isinstance(exc, (httpx.TransportError, OSError, ConnectionError)):
        return _ResponseInfo(RequestOutcome.TRANSIENT)
    return _ResponseInfo(RequestOutcome.PERMANENT)


def _outcome_for_error(error_type: ProviderErrorType) -> RequestOutcome:
    return next(outcome for outcome, category in _OUTCOME_TO_ERROR.items() if category == error_type)


class ProviderExecutionService:
    """Execute provider calls under bounded concurrency with safe telemetry."""

    def __init__(
        self,
        session_factory: async_sessionmaker,
        *,
        key_pool: ApiKeyPool | None = None,
        retry_policy: RetryPolicy | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        random_value: Callable[[], float] = random.random,
    ):
        self.session_factory = session_factory
        self.key_pool = key_pool or ApiKeyPool()
        self.policy = retry_policy or RetryPolicy()
        self._sleep = sleep
        self._random_value = random_value
        self._semaphores: dict[str, asyncio.Semaphore] = {}

    def _semaphore(self, provider: str) -> asyncio.Semaphore:
        if provider not in self._semaphores:
            self._semaphores[provider] = asyncio.Semaphore(self.policy.max_concurrency)
        return self._semaphores[provider]

    async def execute(
        self,
        provider: str,
        operation: str,
        request: Callable[[str | None], Awaitable[T]],
        *,
        requires_api_key: bool = True,
        quota_scope: QuotaScope = QuotaScope.KEY,
        model: str = "",
        job_id=None,
        quota_units: float | None = None,
        cost_units: float | None = None,
    ) -> T:
        """Call an operation; API-key-free providers such as Ollama are valid."""
        if not provider.strip() or not operation.strip():
            raise ValueError("provider and operation are required")
        last_info = _ResponseInfo(RequestOutcome.TRANSIENT)
        last_attempt = 0
        async with self._semaphore(provider):
            for attempt in range(1, self.policy.max_retries + 2):
                last_attempt = attempt
                lease = None
                if quota_scope == QuotaScope.PROVIDER_OPERATION:
                    async with self.session_factory() as state_session:
                        hold = await self.key_pool.provider_operation_hold(state_session, provider, operation)
                    if hold is not None:
                        outcome = _outcome_for_error(hold[0])
                        now = now_utc()
                        await self._record_usage(
                            provider, operation, model, job_id, None,
                            _ResponseInfo(outcome), attempt - 1, started_at=now,
                            completed_at=now, quota_units=None, cost_units=None, duration_ms=0,
                        )
                        raise ProviderExecutionError(provider, operation, outcome, attempt - 1) from None
                if requires_api_key:
                    async with self.session_factory() as key_session:
                        lease = await self.key_pool.lease(
                            key_session,
                            provider,
                            operation,
                            self.policy.key_lease_seconds,
                        )
                    if lease is None:
                        info = last_info if last_attempt > 1 else _ResponseInfo(RequestOutcome.TRANSIENT)
                        await self._record_usage(
                            provider, operation, model, job_id, None, info, attempt - 1,
                            started_at=now_utc(), completed_at=now_utc(), quota_units=None, cost_units=None,
                        )
                        raise ProviderExecutionError(
                            provider, operation, info.outcome, attempt - 1,
                            info.http_status, info.retry_after_seconds,
                        ) from None

                started = now_utc()
                timer = time.perf_counter()
                try:
                    result = await request(lease.credential if lease is not None else None)
                    if isinstance(result, httpx.Response):
                        try:
                            body = result.text
                        except (UnicodeError, httpx.ResponseNotRead):
                            body = ""
                        last_info = _classify_http(result.status_code, result.headers, body)
                    else:
                        last_info = _ResponseInfo(RequestOutcome.SUCCESS)
                    if last_info.outcome == RequestOutcome.SUCCESS:
                        if lease is not None:
                            async with self.session_factory() as key_session:
                                await self.key_pool.release_success(key_session, lease)
                        completed = now_utc()
                        await self._record_usage(
                            provider, operation, model, job_id, lease, last_info, attempt - 1,
                            started_at=started, completed_at=completed, quota_units=quota_units, cost_units=cost_units,
                            duration_ms=int((time.perf_counter() - timer) * 1000),
                        )
                        return result
                except Exception as exc:
                    last_info = _exception_info(exc)

                completed = now_utc()
                duration_ms = int((time.perf_counter() - timer) * 1000)
                if lease is not None:
                    cooldown = self._cooldown(last_info)
                    async with self.session_factory() as key_session:
                        await self.key_pool.release_failure(
                            key_session,
                            lease,
                            _OUTCOME_TO_ERROR[last_info.outcome],
                            cooldown_seconds=cooldown,
                            retry_after_seconds=last_info.retry_after_seconds,
                            quota_cooldown_seconds=self.policy.quota_cooldown_seconds,
                            apply_key_cooldown=quota_scope == QuotaScope.KEY,
                        )
                    if quota_scope == QuotaScope.PROVIDER_OPERATION:
                        async with self.session_factory() as state_session:
                            await self.key_pool.set_provider_operation_hold(
                                state_session,
                                provider,
                                operation,
                                _OUTCOME_TO_ERROR[last_info.outcome],
                                cooldown_seconds=cooldown,
                                retry_after_seconds=last_info.retry_after_seconds,
                                quota_cooldown_seconds=self.policy.quota_cooldown_seconds,
                            )
                await self._record_usage(
                    provider, operation, model, job_id, lease, last_info, attempt - 1,
                    started_at=started, completed_at=completed, quota_units=quota_units, cost_units=cost_units,
                    duration_ms=duration_ms,
                )
                self._log_failure(provider, operation, lease, last_info, attempt)

                if not self._retryable(last_info.outcome) or attempt > self.policy.max_retries:
                    break
                delay = self._delay(attempt, last_info)
                if delay:
                    await self._sleep(delay)

        raise ProviderExecutionError(
            provider, operation, last_info.outcome, last_attempt,
            last_info.http_status, last_info.retry_after_seconds,
        ) from None

    def _retryable(self, outcome: RequestOutcome) -> bool:
        return outcome in {RequestOutcome.TRANSIENT, RequestOutcome.TIMEOUT, RequestOutcome.RATE_LIMITED}

    def _cooldown(self, info: _ResponseInfo) -> float:
        if info.outcome == RequestOutcome.RATE_LIMITED:
            return self.policy.rate_limit_cooldown_seconds
        if info.outcome in {RequestOutcome.TRANSIENT, RequestOutcome.TIMEOUT, RequestOutcome.INVALID_RESPONSE}:
            return self.policy.failure_cooldown_seconds
        return 0

    def _delay(self, attempt: int, info: _ResponseInfo) -> float:
        # A rate-limited key is cooled and the next attempt can rotate to another key.
        # Retry-After governs that key's eligibility; do not sleep the whole pool.
        base = min(self.policy.backoff_base_seconds * (2 ** max(attempt - 1, 0)), self.policy.backoff_max_seconds)
        jitter = self.policy.jitter_seconds * self._random_value()
        return base + jitter

    async def _record_usage(
        self,
        provider: str,
        operation: str,
        model: str,
        job_id,
        lease: ApiKeyLease | None,
        info: _ResponseInfo,
        retry_count: int,
        *,
        started_at: datetime,
        completed_at: datetime,
        quota_units: float | None,
        cost_units: float | None,
        duration_ms: int | None = None,
    ) -> None:
        async with self.session_factory() as usage_session:
            usage_session.add(ProviderUsage(
                provider=provider,
                model=model,
                job_id=job_id,
                success=info.outcome == RequestOutcome.SUCCESS,
                duration_ms=duration_ms,
                operation=operation,
                key_fingerprint=lease.fingerprint if lease is not None else None,
                outcome=info.outcome.value,
                http_status=info.http_status,
                retry_count=retry_count,
                error_category=None if info.outcome == RequestOutcome.SUCCESS else info.outcome.value,
                rate_limit_limit=info.rate_limit_limit,
                rate_limit_remaining=info.rate_limit_remaining,
                rate_limit_reset_at=info.rate_limit_reset_at,
                quota_units=quota_units,
                cost_units=cost_units,
                started_at=started_at,
                completed_at=completed_at,
            ))
            await usage_session.commit()

    @staticmethod
    def _log_failure(provider: str, operation: str, lease: ApiKeyLease | None, info: _ResponseInfo, attempt: int) -> None:
        logger.warning(
            "provider request failed",
            extra={
                "provider": provider,
                "operation": operation,
                "outcome": info.outcome.value,
                "attempt": attempt,
                "key_fingerprint": lease.fingerprint if lease is not None else None,
                "http_status": info.http_status,
            },
        )
