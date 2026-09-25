"""PageSpeed Insights enrichment adapter using normalized Lighthouse evidence."""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
import math
import time
from typing import Any, Literal
from urllib.parse import urlparse, urlunsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.domain.enrichment import (
    EvidenceInput,
    EvidenceSource,
    EvidenceSourceType,
    LeadInput,
    NormalizedObservation,
    ObservationKind,
    ProviderError,
    ProviderErrorType,
    ProviderIdentity,
    ProviderKind,
    ProviderRequestMetadata,
    ProviderResult,
    ProviderResultStatus,
)
from app.models.entities import now_utc
from app.services.provider_execution import ProviderExecutionError, ProviderExecutionService, RequestOutcome
from app.services.website import UnsafeWebsiteUrl, resolve_public_host, validate_url_syntax


PAGESPEED_RUN_URL = "https://pagespeedonline.googleapis.com/pagespeedonline/v5/runPagespeed"
VALID_CATEGORIES = frozenset({"performance", "accessibility", "best-practices", "seo"})
SCORE_CATEGORIES = {
    "performance": "pagespeed_performance_score",
    "accessibility": "pagespeed_accessibility_score",
    "best-practices": "pagespeed_best_practices_score",
    "seo": "pagespeed_seo_score",
}
LAB_METRICS = {
    "largest-contentful-paint": ("pagespeed_lcp", "millisecond"),
    "cumulative-layout-shift": ("pagespeed_cls", "unitless"),
    "interaction-to-next-paint": ("pagespeed_inp", "millisecond"),
    "first-contentful-paint": ("pagespeed_fcp", "millisecond"),
    "server-response-time": ("pagespeed_ttfb", "millisecond"),
    "time-to-first-byte": ("pagespeed_ttfb", "millisecond"),
    "speed-index": ("pagespeed_speed_index", "millisecond"),
    "total-blocking-time": ("pagespeed_total_blocking_time", "millisecond"),
    "time-to-interactive": ("pagespeed_time_to_interactive", "millisecond"),
}
FIELD_METRICS = {
    "LARGEST_CONTENTFUL_PAINT_MS": ("lcp", "millisecond"),
    "CUMULATIVE_LAYOUT_SHIFT_SCORE": ("cls", "provider_value"),
    "INTERACTION_TO_NEXT_PAINT": ("inp", "millisecond"),
    "FIRST_CONTENTFUL_PAINT_MS": ("fcp", "millisecond"),
    "EXPERIMENTAL_TIME_TO_FIRST_BYTE": ("ttfb", "millisecond"),
    "EXPERIMENTAL_TIME_TO_FIRST_BYTE_MS": ("ttfb", "millisecond"),
}
MAX_AUDIT_FAILURES = 30

PAGESPEED_IDENTITY = ProviderIdentity(
    name="pagespeed",
    kind=ProviderKind.EXTERNAL_DATA,
    implementation="pagespeed_insights_v5",
)


class PageSpeedMetric(BaseModel):
    """Stable representation for lab and field metric measurements."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: float
    unit: str
    data_source: str
    display_value: str | None = None
    category: str | None = None

    def contract_value(self) -> dict[str, str | float | None]:
        return self.model_dump(mode="json", exclude_none=True)


class PageSpeedAuditFailure(BaseModel):
    """Small, normalized Lighthouse failure/opportunity (never raw audit data)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    audit_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=250)
    score: float
    display_value: str | None = Field(default=None, max_length=500)
    savings_ms: float | None = None
    savings_bytes: float | None = None

    def contract_value(self) -> dict[str, str | float | None]:
        return self.model_dump(mode="json", exclude_none=True)


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def normalize_target_url(value: str) -> str:
    """Normalize a supplied website URL after applying the website safety rules."""
    raw = value.strip()
    if not raw:
        raise UnsafeWebsiteUrl("A website URL is required")
    if "://" not in raw:
        raw = f"https://{raw}"
    host = validate_url_syntax(raw)
    parsed = urlparse(raw)
    port = parsed.port
    if ":" in host:  # IPv6 literals need brackets in the authority component.
        host_part = f"[{host}]"
    else:
        host_part = host
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    netloc = host_part if port in {None, default_port} else f"{host_part}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, ""))


def _analysis_time(payload: dict[str, Any], lighthouse: dict[str, Any], fallback: datetime) -> datetime:
    for value in (payload.get("analysisUTCTimestamp"), lighthouse.get("fetchTime")):
        if not isinstance(value, str):
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                return parsed
        except ValueError:
            pass
    return fallback


class PageSpeedProvider:
    """Optional mobile/desktop PSI collector; never persists its API response."""

    name = "pagespeed"

    def __init__(
        self,
        execution: ProviderExecutionService,
        *,
        enabled: bool | None = None,
        request_timeout_seconds: float | None = None,
        operation: str | None = None,
        strategy: Literal["mobile", "desktop"] | None = None,
        categories: tuple[str, ...] | None = None,
        client_factory=httpx.AsyncClient,
    ) -> None:
        self.execution = execution
        self.enabled = settings.pagespeed_enabled if enabled is None else enabled
        self.api_key_configured = bool(settings.pagespeed_api_key.strip())
        self.timeout = request_timeout_seconds if request_timeout_seconds is not None else settings.pagespeed_request_timeout_seconds
        self.operation = operation or settings.pagespeed_operation
        self.strategy = strategy or settings.pagespeed_strategy
        configured_categories = tuple(part.strip() for part in settings.pagespeed_categories.split(",") if part.strip())
        self.categories = categories if categories is not None else configured_categories
        self.client_factory = client_factory
        if self.timeout <= 0:
            raise ValueError("PageSpeed request timeout must be positive")
        if self.strategy not in {"mobile", "desktop"}:
            raise ValueError("PageSpeed strategy must be mobile or desktop")
        if any(category not in VALID_CATEGORIES for category in self.categories):
            raise ValueError("PageSpeed categories contain an unsupported category")
        if len(set(self.categories)) != len(self.categories):
            raise ValueError("PageSpeed categories cannot contain duplicates")

    async def collect(self, lead: LeadInput) -> ProviderResult:
        started_at = now_utc()
        timer = time.perf_counter()
        if not self.enabled:
            return self._failure(lead, "PROVIDER_DISABLED", "PageSpeed provider is disabled", ProviderErrorType.PERMANENT, started_at, timer)
        if not lead.website:
            return self._failure(lead, "MISSING_URL", "Lead has no website URL for PageSpeed analysis", ProviderErrorType.PERMANENT, started_at, timer)
        try:
            tested_url = normalize_target_url(lead.website)
            parsed = urlparse(tested_url)
            host = validate_url_syntax(tested_url)
            await resolve_public_host(host, parsed.port or (443 if parsed.scheme == "https" else 80))
        except (UnsafeWebsiteUrl, ValueError):
            return self._failure(lead, "INVALID_URL", "Lead website is not a safe public HTTP(S) URL", ProviderErrorType.PERMANENT, started_at, timer)
        except (OSError, TimeoutError, asyncio.TimeoutError):
            return self._failure(lead, "URL_VALIDATION_FAILED", "Lead website could not be safely validated", ProviderErrorType.PERMANENT, started_at, timer)

        params: list[tuple[str, str]] = [("url", tested_url), ("strategy", self.strategy)]
        params.extend(("category", category) for category in self.categories)
        response: httpx.Response | None = None
        try:
            async with self.client_factory(timeout=self.timeout) as client:
                async def request(credential: str | None) -> httpx.Response:
                    headers = {"X-Goog-Api-Key": credential} if credential else {}
                    result = await client.get(PAGESPEED_RUN_URL, params=params, headers=headers)
                    if 200 <= result.status_code < 300:
                        result.json()
                    return result

                response = await self.execution.execute(
                    "pagespeed",
                    self.operation,
                    request,
                    requires_api_key=self.api_key_configured,
                )
            payload = response.json()
        except ProviderExecutionError as exc:
            category = {
                RequestOutcome.TRANSIENT: ProviderErrorType.TRANSIENT,
                RequestOutcome.TIMEOUT: ProviderErrorType.TIMEOUT,
                RequestOutcome.RATE_LIMITED: ProviderErrorType.RATE_LIMITED,
                RequestOutcome.AUTHENTICATION: ProviderErrorType.AUTHENTICATION,
                RequestOutcome.QUOTA_EXHAUSTED: ProviderErrorType.QUOTA_EXHAUSTED,
                RequestOutcome.INVALID_RESPONSE: ProviderErrorType.INVALID_RESPONSE,
                RequestOutcome.PERMANENT: ProviderErrorType.PERMANENT,
            }.get(exc.outcome, ProviderErrorType.TRANSIENT)
            return self._failure(
                lead,
                f"PAGESPEED_{exc.outcome.value.upper()}",
                f"PageSpeed request failed ({exc.outcome.value})",
                category,
                started_at,
                timer,
                status=exc.http_status,
                attempt=max(1, exc.attempts),
                retry_after=exc.retry_after_seconds,
                attributes={"strategy": self.strategy},
            )
        except (httpx.TimeoutException, TimeoutError):
            return self._failure(lead, "PAGESPEED_TIMEOUT", "PageSpeed request timed out", ProviderErrorType.TIMEOUT, started_at, timer)
        except (httpx.TransportError, OSError, ConnectionError):
            return self._failure(lead, "PAGESPEED_TRANSPORT", "PageSpeed transport failed", ProviderErrorType.TRANSIENT, started_at, timer)
        except (ValueError, TypeError, json.JSONDecodeError):
            return self._failure(lead, "INVALID_RESPONSE", "PageSpeed returned malformed JSON", ProviderErrorType.INVALID_RESPONSE, started_at, timer)

        if not isinstance(payload, dict):
            return self._failure(lead, "INVALID_RESPONSE", "PageSpeed returned an invalid response structure", ProviderErrorType.INVALID_RESPONSE, started_at, timer, status=response.status_code)
        lighthouse = payload.get("lighthouseResult")
        if not isinstance(lighthouse, dict):
            return self._failure(
                lead,
                "NO_LIGHTHOUSE_DATA",
                "PageSpeed returned no Lighthouse analysis data",
                ProviderErrorType.PERMANENT,
                started_at,
                timer,
                status=response.status_code,
                attributes={"strategy": self.strategy},
            )
        runtime_error = lighthouse.get("runtimeError")
        if isinstance(runtime_error, dict):
            code = runtime_error.get("code") if isinstance(runtime_error.get("code"), str) else "LIGHTHOUSE_RUNTIME_ERROR"
            return self._failure(
                lead,
                "LIGHTHOUSE_RUNTIME_ERROR",
                "Lighthouse could not complete the requested analysis",
                ProviderErrorType.PERMANENT,
                started_at,
                timer,
                status=response.status_code,
                attributes={"strategy": self.strategy, "lighthouse_error_code": code},
            )
        categories = lighthouse.get("categories", {})
        audits = lighthouse.get("audits", {})
        if not isinstance(categories, dict) or not isinstance(audits, dict):
            return self._failure(lead, "INVALID_RESPONSE", "PageSpeed returned invalid Lighthouse data", ProviderErrorType.INVALID_RESPONSE, started_at, timer, status=response.status_code)
        if not categories and not audits:
            return self._failure(
                lead,
                "NO_LIGHTHOUSE_DATA",
                "PageSpeed returned no Lighthouse scores or audits",
                ProviderErrorType.PERMANENT,
                started_at,
                timer,
                status=response.status_code,
                attributes={"strategy": self.strategy},
            )

        fetched_at = now_utc()
        observed_at = _analysis_time(payload, lighthouse, fetched_at)
        analysis_reference = payload.get("analysisUTCTimestamp") or lighthouse.get("fetchTime")
        if not isinstance(analysis_reference, str):
            analysis_reference = None
        return self._normalize_result(
            lead,
            tested_url,
            categories,
            audits,
            payload,
            lighthouse,
            observed_at,
            fetched_at,
            analysis_reference,
            started_at,
            timer,
            response.status_code,
        )

    def _normalize_result(
        self,
        lead: LeadInput,
        tested_url: str,
        categories: dict[str, Any],
        audits: dict[str, Any],
        payload: dict[str, Any],
        lighthouse: dict[str, Any],
        observed_at: datetime,
        fetched_at: datetime,
        analysis_reference: str | None,
        started_at: datetime,
        timer: float,
        status: int,
    ) -> ProviderResult:
        evidence: list[EvidenceInput] = []
        observations: list[NormalizedObservation] = []
        source = EvidenceSource(source_type=EvidenceSourceType.PERFORMANCE_AUDIT, provider=PAGESPEED_IDENTITY)
        lighthouse_version = lighthouse.get("lighthouseVersion") if isinstance(lighthouse.get("lighthouseVersion"), str) else None

        def add(
            evidence_type: str,
            value: str | int | float | bool | dict[str, str | int | float | bool | None],
            *,
            at: datetime = observed_at,
            record_strategy: str | None = self.strategy,
            metadata: dict[str, str | int | float | bool | None] | None = None,
        ) -> None:
            evidence_metadata: dict[str, str | int | float | bool | None] = {
                "strategy": record_strategy,
                "tested_url": tested_url,
            }
            if lighthouse_version:
                evidence_metadata["lighthouse_version"] = lighthouse_version
            if metadata:
                evidence_metadata.update(metadata)
            evidence.append(EvidenceInput(
                lead_id=lead.lead_id,
                enrichment_run_id=lead.enrichment_run_id,
                evidence_type=evidence_type,
                source=source,
                source_url=tested_url,
                external_provider_reference=analysis_reference,
                observed_value=value,
                observed_at=at,
                fetched_at=fetched_at,
                confidence=1.0,
                metadata=evidence_metadata,
            ))
            observations.append(NormalizedObservation(
                key=evidence_type,
                value=value,
                kind=ObservationKind.FACT,
                observed_at=at,
                confidence=1.0,
            ))

        add("pagespeed_tested_url", tested_url, at=fetched_at)
        add("pagespeed_strategy", self.strategy, at=fetched_at)
        final_url = lighthouse.get("finalUrl")
        if isinstance(final_url, str) and final_url:
            try:
                normalized_final_url = normalize_target_url(final_url)
            except (UnsafeWebsiteUrl, ValueError):
                normalized_final_url = None
            if normalized_final_url:
                add("pagespeed_final_url", normalized_final_url)

        for category_id, evidence_type in SCORE_CATEGORIES.items():
            category = categories.get(category_id)
            score = _finite_number(category.get("score")) if isinstance(category, dict) else None
            if score is not None and 0 <= score <= 1:
                add(evidence_type, score, metadata={"score_scale": "fraction_0_to_1"})

        for audit_id, (evidence_type, default_unit) in LAB_METRICS.items():
            audit = audits.get(audit_id)
            numeric = _finite_number(audit.get("numericValue")) if isinstance(audit, dict) else None
            if numeric is None:
                continue
            unit = audit.get("numericUnit") if isinstance(audit.get("numericUnit"), str) else default_unit
            metric = PageSpeedMetric(
                value=numeric,
                unit=unit,
                data_source="lighthouse_lab",
                display_value=audit.get("displayValue") if isinstance(audit.get("displayValue"), str) else None,
            )
            add(evidence_type, metric.contract_value(), metadata={"audit_id": audit_id, "data_source": "lighthouse_lab"})

        for scope, response_key in (("page", "loadingExperience"), ("origin", "originLoadingExperience")):
            loading = payload.get(response_key)
            field_metrics = loading.get("metrics") if isinstance(loading, dict) else None
            if not isinstance(field_metrics, dict):
                continue
            for api_metric, (short_name, unit) in FIELD_METRICS.items():
                metric = field_metrics.get(api_metric)
                percentile = _finite_number(metric.get("percentile")) if isinstance(metric, dict) else None
                if percentile is None:
                    continue
                category = metric.get("category") if isinstance(metric.get("category"), str) else None
                normalized = PageSpeedMetric(
                    value=percentile,
                    unit=unit,
                    data_source=f"crux_{scope}",
                    category=category,
                )
                add(
                    f"pagespeed_field_{short_name}_{scope}",
                    normalized.contract_value(),
                    record_strategy=None,
                    metadata={"data_source": f"crux_{scope}", "metric_id": api_metric},
                )

        audit_failures = []
        for audit_id in sorted(audits):
            audit = audits[audit_id]
            if not isinstance(audit, dict):
                continue
            score = _finite_number(audit.get("score"))
            display_mode = audit.get("scoreDisplayMode")
            title = audit.get("title")
            if score is None or not 0 <= score < 1 or display_mode not in {"numeric", "binary"} or not isinstance(title, str) or not title.strip():
                continue
            details = audit.get("details") if isinstance(audit.get("details"), dict) else {}
            failure = PageSpeedAuditFailure(
                audit_id=str(audit.get("id") or audit_id)[:200],
                title=title.strip()[:250],
                score=score,
                display_value=audit.get("displayValue", "")[:500] if isinstance(audit.get("displayValue"), str) else None,
                savings_ms=_finite_number(details.get("overallSavingsMs")),
                savings_bytes=_finite_number(details.get("overallSavingsBytes")),
            )
            audit_failures.append(failure)
            if len(audit_failures) >= MAX_AUDIT_FAILURES:
                break
        for failure in audit_failures:
            value = failure.contract_value()
            add(
                "pagespeed_audit_failure",
                value,
                metadata={"audit_id": failure.audit_id, "data_source": "lighthouse_audit"},
            )

        return ProviderResult(
            provider=PAGESPEED_IDENTITY,
            status=ProviderResultStatus.SUCCEEDED,
            observations=tuple(observations),
            evidence=tuple(evidence),
            metadata=self._metadata(
                started_at,
                timer,
                status=status,
                attributes={
                    "strategy": self.strategy,
                    "tested_url": tested_url,
                    "lighthouse_version": lighthouse_version,
                    "analysis_reference": analysis_reference,
                },
            ),
        )

    def _metadata(
        self,
        started_at: datetime,
        timer: float,
        *,
        status: int | None = None,
        attempt: int = 1,
        attributes: dict[str, str | int | float | bool | None] | None = None,
    ) -> ProviderRequestMetadata:
        return ProviderRequestMetadata(
            operation=self.operation,
            attempt=max(1, attempt),
            duration_ms=max(0, int((time.perf_counter() - timer) * 1000)),
            started_at=started_at,
            completed_at=now_utc(),
            response_status=status,
            attributes=attributes or {},
        )

    def _failure(
        self,
        lead: LeadInput,
        code: str,
        message: str,
        error_type: ProviderErrorType,
        started_at: datetime,
        timer: float,
        *,
        status: int | None = None,
        attempt: int = 1,
        retry_after: float | None = None,
        attributes: dict[str, str | int | float | bool | None] | None = None,
    ) -> ProviderResult:
        del lead  # Lead identifiers are already represented by the future evidence contracts.
        return ProviderResult(
            provider=PAGESPEED_IDENTITY,
            status=ProviderResultStatus.FAILED,
            metadata=self._metadata(started_at, timer, status=status, attempt=attempt, attributes=attributes),
            errors=(ProviderError(
                error_type=error_type,
                code=code,
                message=message,
                retry_after_seconds=retry_after,
            ),),
        )
