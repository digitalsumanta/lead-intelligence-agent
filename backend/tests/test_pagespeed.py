"""PageSpeed provider tests use mocked HTTP responses; no API key/API is needed."""

from __future__ import annotations

import logging
from datetime import timezone

import httpx
import pytest
from sqlalchemy import select

from app.config import settings
from app.domain.enrichment import LeadInput, ProviderErrorType, ProviderResultStatus
from app.models import ApiKey, Evidence, ProviderUsage
from app.models.entities import now_utc
from app.providers.pagespeed import PAGESPEED_RUN_URL, PageSpeedProvider
from app.services.provider_execution import ProviderExecutionService, RetryPolicy


def _retry_policy(max_retries=0):
    return RetryPolicy(
        max_retries=max_retries,
        backoff_base_seconds=0,
        backoff_max_seconds=0,
        jitter_seconds=0,
        max_concurrency=3,
        key_lease_seconds=60,
        failure_cooldown_seconds=0,
        rate_limit_cooldown_seconds=60,
        quota_cooldown_seconds=600,
    )


class MockClient:
    def __init__(self, *, response=None, exception=None, calls=None, timeout=None):
        self.response = response
        self.exception = exception
        self.calls = calls if calls is not None else []
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url, *, params, headers):
        self.calls.append({"url": url, "params": params, "headers": headers, "timeout": self.timeout})
        if self.exception:
            raise self.exception
        return self.response


def _response(status=200, payload=None, headers=None, content=None):
    request = httpx.Request("GET", PAGESPEED_RUN_URL)
    if content is not None:
        return httpx.Response(status, content=content, headers=headers, request=request)
    return httpx.Response(status, json=payload if payload is not None else {}, headers=headers, request=request)


def _payload(*, include_scores=True, include_metrics=True, include_crux=True):
    categories = {
        "performance": {"score": 0.92},
        "accessibility": {"score": 0.88},
        "best-practices": {"score": 0.96},
        "seo": {"score": 0.84},
    } if include_scores else {
        "performance": {"score": None},
        "accessibility": {},
        "best-practices": {"score": "unknown"},
        "seo": {"score": None},
    }
    audits = {
        "largest-contentful-paint": {
            "numericValue": 2100,
            "numericUnit": "millisecond",
            "displayValue": "2.1 s",
            "score": 0.9,
            "scoreDisplayMode": "numeric",
            "title": "Largest Contentful Paint",
        },
        "cumulative-layout-shift": {
            "numericValue": 0.04,
            "numericUnit": "unitless",
            "score": 0.97,
            "scoreDisplayMode": "numeric",
            "title": "Cumulative Layout Shift",
        },
        "first-contentful-paint": {"numericValue": 900, "numericUnit": "millisecond", "score": 0.98, "scoreDisplayMode": "numeric", "title": "First Contentful Paint"},
        "server-response-time": {"numericValue": 350, "numericUnit": "millisecond", "score": 0.8, "scoreDisplayMode": "numeric", "title": "Time to First Byte"},
        "uses-https": {
            "score": 0,
            "scoreDisplayMode": "binary",
            "title": "Uses HTTPS",
            "displayValue": "HTTPS is not enabled",
            "details": {"overallSavingsMs": 25, "overallSavingsBytes": 120},
            "detailsRawProviderPayloadMustNotEscape": {"private": "do-not-copy"},
        },
        "manual-audit": {"score": None, "scoreDisplayMode": "manual", "title": "Manual audit"},
    } if include_metrics else {}
    payload = {
        "analysisUTCTimestamp": "2026-09-26T12:00:00Z",
        "id": "https://example.com/final",
        "lighthouseResult": {
            "requestedUrl": "https://example.com/",
            "finalUrl": "https://example.com/final",
            "fetchTime": "2026-09-26T12:00:00Z",
            "lighthouseVersion": "13.0.0",
            "categories": categories,
            "audits": audits,
        },
    }
    if include_crux:
        payload["loadingExperience"] = {"metrics": {
            "LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 2500, "category": "AVERAGE"},
            "CUMULATIVE_LAYOUT_SHIFT_SCORE": {"percentile": 8, "category": "FAST"},
            "INTERACTION_TO_NEXT_PAINT": {"percentile": 180, "category": "FAST"},
        }}
        payload["originLoadingExperience"] = {"metrics": {
            "FIRST_CONTENTFUL_PAINT_MS": {"percentile": 1300, "category": "AVERAGE"},
        }}
    return payload


def _lead(website="https://example.com/", **overrides):
    fields = {
        "lead_id": "c74ea03e-6992-4db6-9bd3-c7fe665e65d4",
        "enrichment_run_id": "e74ea03e-6992-4db6-9bd3-c7fe665e65d4",
        "company_name": "Example Company",
        "website": website,
    }
    fields.update(overrides)
    return LeadInput(**fields)


async def _rows(session_factory, model):
    async with session_factory() as session:
        return (await session.scalars(select(model))).all()


async def _provider(
    session_factory,
    monkeypatch,
    *,
    response=None,
    exception=None,
    calls=None,
    retries=0,
    strategy="mobile",
    enabled=True,
    api_key=None,
):
    monkeypatch.setattr(settings, "pagespeed_api_key", api_key or "")

    async def public_dns_noop(_host, _port):
        return None

    monkeypatch.setattr("app.providers.pagespeed.resolve_public_host", public_dns_noop)
    if api_key:
        async with session_factory() as session:
            session.add(ApiKey(provider="pagespeed", key=api_key))
            await session.commit()
    service = ProviderExecutionService(session_factory, retry_policy=_retry_policy(retries))

    def factory(*, timeout):
        return MockClient(response=response, exception=exception, calls=calls, timeout=timeout)

    return PageSpeedProvider(
        service,
        enabled=enabled,
        request_timeout_seconds=4.5,
        operation="run_pagespeed_test",
        strategy=strategy,
        categories=("performance", "seo"),
        client_factory=factory,
    )


@pytest.mark.asyncio
async def test_successful_mobile_analysis_extracts_scores_metrics_audits_and_provenance(session_factory, monkeypatch):
    calls = []
    provider = await _provider(session_factory, monkeypatch, response=_response(payload=_payload()), calls=calls, api_key="pagespeed-secret")
    lead = _lead()

    result = await provider.collect(lead)

    assert result.status == ProviderResultStatus.SUCCEEDED
    assert result.provider.name == "pagespeed"
    evidence = {item.evidence_type: item for item in result.evidence if item.evidence_type != "pagespeed_audit_failure"}
    assert evidence["pagespeed_tested_url"].observed_value == "https://example.com/"
    assert evidence["pagespeed_strategy"].observed_value == "mobile"
    assert evidence["pagespeed_performance_score"].observed_value == 0.92
    assert evidence["pagespeed_accessibility_score"].observed_value == 0.88
    assert evidence["pagespeed_best_practices_score"].observed_value == 0.96
    assert evidence["pagespeed_seo_score"].observed_value == 0.84
    assert evidence["pagespeed_lcp"].observed_value["value"] == 2100
    assert evidence["pagespeed_lcp"].observed_value["unit"] == "millisecond"
    assert evidence["pagespeed_cls"].observed_value["value"] == 0.04
    assert evidence["pagespeed_fcp"].observed_value["value"] == 900
    assert evidence["pagespeed_ttfb"].observed_value["value"] == 350
    assert evidence["pagespeed_field_lcp_page"].observed_value["value"] == 2500
    assert evidence["pagespeed_field_inp_page"].observed_value["category"] == "FAST"
    assert evidence["pagespeed_field_fcp_origin"].metadata["strategy"] is None
    assert evidence["pagespeed_final_url"].observed_value == "https://example.com/final"
    assert result.metadata.attributes["analysis_reference"] == "2026-09-26T12:00:00Z"
    assert len(result.evidence) == len(result.observations)
    assert all(item.source.source_type.value == "performance_audit" for item in result.evidence)
    assert all(item.source.provider.name == "pagespeed" for item in result.evidence)
    assert all(item.source_url == "https://example.com/" for item in result.evidence)
    assert all(item.enrichment_run_id == lead.enrichment_run_id for item in result.evidence)
    assert all(item.confidence == 1.0 for item in result.evidence)
    assert all(item.observed_at.tzinfo is not None and item.fetched_at.tzinfo is not None for item in result.evidence)
    audit_failure = next(item for item in result.evidence if item.evidence_type == "pagespeed_audit_failure" and item.observed_value["audit_id"] == "uses-https")
    assert audit_failure.observed_value["audit_id"] == "uses-https"
    assert audit_failure.observed_value["savings_ms"] == 25
    assert "detailsRawProviderPayloadMustNotEscape" not in str(result.model_dump())

    query = dict(calls[0]["params"])
    assert calls[0]["url"] == PAGESPEED_RUN_URL
    assert query["url"] == "https://example.com/" and query["strategy"] == "mobile"
    assert calls[0]["params"].count(("category", "performance")) == 1
    assert calls[0]["params"].count(("category", "seo")) == 1
    assert calls[0]["headers"] == {"X-Goog-Api-Key": "pagespeed-secret"}
    assert calls[0]["timeout"] == 4.5
    usage = await _rows(session_factory, ProviderUsage)
    assert len(usage) == 1 and usage[0].provider == "pagespeed" and usage[0].operation == "run_pagespeed_test"
    assert usage[0].success and usage[0].http_status == 200 and usage[0].key_fingerprint is not None


@pytest.mark.asyncio
async def test_desktop_results_remain_strategy_scoped(session_factory, monkeypatch):
    calls = []
    provider = await _provider(session_factory, monkeypatch, response=_response(payload=_payload(include_crux=False)), calls=calls, strategy="desktop")
    result = await provider.collect(_lead())
    assert result.status == ProviderResultStatus.SUCCEEDED
    strategy_evidence = next(item for item in result.evidence if item.evidence_type == "pagespeed_strategy")
    score = next(item for item in result.evidence if item.evidence_type == "pagespeed_performance_score")
    assert strategy_evidence.observed_value == "desktop"
    assert score.metadata["strategy"] == "desktop"
    assert dict(calls[0]["params"])["strategy"] == "desktop"


@pytest.mark.asyncio
async def test_null_and_missing_scores_are_not_converted_to_zero(session_factory, monkeypatch):
    provider = await _provider(session_factory, monkeypatch, response=_response(payload=_payload(include_scores=False, include_metrics=False, include_crux=False)))
    result = await provider.collect(_lead())
    assert result.status == ProviderResultStatus.SUCCEEDED
    evidence_types = {item.evidence_type for item in result.evidence}
    assert "pagespeed_performance_score" not in evidence_types
    assert "pagespeed_accessibility_score" not in evidence_types
    assert "pagespeed_best_practices_score" not in evidence_types
    assert "pagespeed_seo_score" not in evidence_types


@pytest.mark.asyncio
async def test_missing_lighthouse_data_is_typed_as_no_data_not_transport_failure(session_factory, monkeypatch):
    provider = await _provider(session_factory, monkeypatch, response=_response(payload={"loadingExperience": {}}))
    result = await provider.collect(_lead())
    assert result.status == ProviderResultStatus.FAILED
    assert result.errors[0].code == "NO_LIGHTHOUSE_DATA"
    assert result.errors[0].error_type == ProviderErrorType.PERMANENT
    assert (await _rows(session_factory, ProviderUsage))[0].outcome == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [[], {"lighthouseResult": []}, {"lighthouseResult": {"categories": [], "audits": {}}}])
async def test_malformed_response_is_safe(session_factory, monkeypatch, payload):
    provider = await _provider(session_factory, monkeypatch, response=_response(payload=payload))
    result = await provider.collect(_lead())
    assert result.status == ProviderResultStatus.FAILED
    assert result.errors[0].error_type in {ProviderErrorType.INVALID_RESPONSE, ProviderErrorType.PERMANENT}
    assert not result.evidence


@pytest.mark.asyncio
async def test_malformed_json_is_classified_by_provider_execution(session_factory, monkeypatch):
    provider = await _provider(session_factory, monkeypatch, response=_response(content=b"invalid-json"))
    result = await provider.collect(_lead())
    assert result.errors[0].error_type == ProviderErrorType.INVALID_RESPONSE
    assert (await _rows(session_factory, ProviderUsage))[0].outcome == "invalid_response"


@pytest.mark.asyncio
@pytest.mark.parametrize("status,payload,error_type,outcome", [
    (400, {"error": {"message": "bad URL"}}, ProviderErrorType.PERMANENT, "permanent"),
    (401, {"error": {"message": "invalid key"}}, ProviderErrorType.AUTHENTICATION, "authentication"),
    (403, {"error": {"message": "Daily Limit Exceeded: quota exceeded"}}, ProviderErrorType.QUOTA_EXHAUSTED, "quota_exhausted"),
    (429, {"error": {"message": "rate limited"}}, ProviderErrorType.RATE_LIMITED, "rate_limited"),
    (503, {"error": {"message": "temporary service issue"}}, ProviderErrorType.TRANSIENT, "transient"),
])
async def test_http_failures_map_to_provider_error_types(session_factory, monkeypatch, status, payload, error_type, outcome):
    retry_headers = {"Retry-After": "23"} if status == 429 else None
    provider = await _provider(session_factory, monkeypatch, response=_response(status=status, payload=payload, headers=retry_headers), api_key="pagespeed-key")
    result = await provider.collect(_lead())
    assert result.status == ProviderResultStatus.FAILED
    assert result.errors[0].error_type == error_type
    if status == 429:
        assert result.errors[0].retry_after_seconds == 23
    usage = await _rows(session_factory, ProviderUsage)
    assert usage and all(item.outcome == outcome for item in usage)
    assert not result.evidence


@pytest.mark.asyncio
async def test_rate_limit_retry_after_cools_leased_key(session_factory, monkeypatch):
    provider = await _provider(session_factory, monkeypatch, response=_response(status=429, payload={"error": "rate limited"}, headers={"Retry-After": "23"}), api_key="cooling-pagespeed-key")
    result = await provider.collect(_lead())
    assert result.errors[0].error_type == ProviderErrorType.RATE_LIMITED
    async with session_factory() as session:
        key = await session.scalar(select(ApiKey).where(ApiKey.provider == "pagespeed"))
        assert key.cooldown_until.replace(tzinfo=timezone.utc) > now_utc()


@pytest.mark.asyncio
async def test_timeout_and_transport_failures_are_normalized(session_factory, monkeypatch):
    for exception, expected in [
        (httpx.ReadTimeout("timeout private detail"), ProviderErrorType.TIMEOUT),
        (httpx.ConnectError("private host detail"), ProviderErrorType.TRANSIENT),
    ]:
        provider = await _provider(session_factory, monkeypatch, exception=exception)
        result = await provider.collect(_lead())
        assert result.status == ProviderResultStatus.FAILED
        assert result.errors[0].error_type == expected


@pytest.mark.asyncio
async def test_disabled_missing_and_unsafe_urls_do_not_call_external_api(session_factory, monkeypatch):
    calls = []
    disabled = await _provider(session_factory, monkeypatch, enabled=False, calls=calls)
    assert (await disabled.collect(_lead())).errors[0].code == "PROVIDER_DISABLED"
    enabled = await _provider(session_factory, monkeypatch, calls=calls)
    assert (await enabled.collect(_lead(website=None))).errors[0].code == "MISSING_URL"
    unsafe = await enabled.collect(_lead(website="http://127.0.0.1/private"))
    assert unsafe.errors[0].code == "INVALID_URL"
    assert calls == []
    assert await _rows(session_factory, ProviderUsage) == []


@pytest.mark.asyncio
async def test_website_urls_are_normalized_and_dns_validation_is_required(session_factory, monkeypatch):
    calls = []
    provider = await _provider(session_factory, monkeypatch, response=_response(payload=_payload()), calls=calls)
    result = await provider.collect(_lead(website="Example.COM/path#section"))
    assert result.status == ProviderResultStatus.SUCCEEDED
    assert dict(calls[0]["params"])["url"] == "https://example.com/path"

    async def reject_private(_host, _port):
        from app.services.website import UnsafeWebsiteUrl
        raise UnsafeWebsiteUrl("private resolution")

    monkeypatch.setattr("app.providers.pagespeed.resolve_public_host", reject_private)
    before = len(calls)
    result = await provider.collect(_lead())
    assert result.errors[0].code == "INVALID_URL"
    assert len(calls) == before


@pytest.mark.asyncio
async def test_credentials_and_raw_response_are_not_logged_or_persisted(session_factory, monkeypatch, caplog):
    secret = "sensitive-pagespeed-api-key"
    payload = _payload()
    payload["unselectedPrivateRawResponse"] = {"token": secret}
    provider = await _provider(session_factory, monkeypatch, response=_response(payload=payload), api_key=secret)
    with caplog.at_level(logging.WARNING):
        result = await provider.collect(_lead())
    assert secret not in result.model_dump_json() and secret not in caplog.text
    assert secret not in str(await _rows(session_factory, ProviderUsage))
    assert await _rows(session_factory, Evidence) == []  # Collection returns contracts; it never persists API payloads.
    assert "unselectedPrivateRawResponse" not in str(result.model_dump())


@pytest.mark.asyncio
async def test_configured_api_key_is_registered_for_provider_execution(monkeypatch, session_factory):
    from app.services.key_config import sync_environment_keys

    monkeypatch.setattr(settings, "pagespeed_api_key", "environment-pagespeed-key")
    async with session_factory() as session:
        await sync_environment_keys(session)
    async with session_factory() as session:
        key = await session.scalar(select(ApiKey).where(ApiKey.provider == "pagespeed"))
    assert key is not None and key.key == "environment-pagespeed-key"
