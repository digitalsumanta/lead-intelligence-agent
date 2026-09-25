"""Google Places adapter tests use mocked HTTP responses only."""

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
from app.providers.google_places import GooglePlacesProvider, PLACES_FIELD_MASK, PLACES_SEARCH_TEXT_URL
from app.services.provider_execution import ProviderExecutionService, RetryPolicy


def _retry_policy(max_retries=0):
    return RetryPolicy(
        max_retries=max_retries,
        backoff_base_seconds=0,
        backoff_max_seconds=0,
        jitter_seconds=0,
        max_concurrency=4,
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

    async def post(self, url, *, headers, json):
        self.calls.append({"url": url, "headers": headers, "json": json})
        if self.exception:
            raise self.exception
        return self.response


def _response(status=200, payload=None, headers=None, content=None):
    request = httpx.Request("POST", PLACES_SEARCH_TEXT_URL)
    if content is not None:
        return httpx.Response(status, content=content, headers=headers, request=request)
    return httpx.Response(status, json=payload if payload is not None else {}, headers=headers, request=request)


async def _configured_provider(session_factory, monkeypatch, *, response=None, exception=None, calls=None, retries=0, enabled=True):
    monkeypatch.setattr(settings, "google_places_api_key", "configured-google-secret")
    async with session_factory() as session:
        session.add(ApiKey(provider="google_places", key="google-secret-value"))
        await session.commit()
    service = ProviderExecutionService(session_factory, retry_policy=_retry_policy(retries))

    def factory(*, timeout):
        return MockClient(response=response, exception=exception, calls=calls, timeout=timeout)

    return GooglePlacesProvider(service, enabled=enabled, request_timeout_seconds=3.5, operation="text_search_test", client_factory=factory)


def _lead(**overrides):
    data = {
        "lead_id": "c74ea03e-6992-4db6-9bd3-c7fe665e65d4",
        "enrichment_run_id": "e74ea03e-6992-4db6-9bd3-c7fe665e65d4",
        "company_name": "Northwind Dental",
        "website": "https://northwinddental.example",
        "phone": "+1 415 555 0100",
        "address": "10 Market Street",
        "city": "San Francisco",
        "state": "CA",
        "country": "USA",
    }
    data.update(overrides)
    return LeadInput(**data)


def _place(**overrides):
    data = {
        "id": "place-123",
        "displayName": {"text": "Northwind Dental"},
        "formattedAddress": "10 Market Street, San Francisco, CA, USA",
        "internationalPhoneNumber": "+1 415-555-0100",
        "nationalPhoneNumber": "(415) 555-0100",
        "websiteUri": "https://northwinddental.example/",
        "rating": 4.7,
        "userRatingCount": 128,
        "businessStatus": "OPERATIONAL",
        "googleMapsUri": "https://maps.google.com/?cid=123",
        "types": ["dentist", "health"],
        "primaryTypeDisplayName": {"text": "Dentist"},
        "regularOpeningHours": {"weekdayDescriptions": ["Monday: 9 AM to 5 PM"]},
    }
    data.update(overrides)
    return data


async def _rows(session_factory, model):
    async with session_factory() as session:
        return (await session.scalars(select(model))).all()


@pytest.mark.asyncio
async def test_strong_business_match_normalizes_fields_and_maps_provenance(session_factory, monkeypatch):
    calls = []
    provider = await _configured_provider(session_factory, monkeypatch, response=_response(payload={"places": [_place()]}), calls=calls)
    lead = _lead()

    result = await provider.collect(lead)

    assert result.status == ProviderResultStatus.SUCCEEDED
    attrs = result.metadata.attributes
    assert attrs["match_status"] == "strong_match" and attrs["place_id"] == "place-123"
    assert attrs["matched_business_name"] == "Northwind Dental" and attrs["match_confidence"] >= 0.72
    assert len(result.evidence) == len(result.observations)
    evidence = {item.evidence_type: item for item in result.evidence}
    assert evidence["google_place_id"].observed_value == "place-123"
    assert evidence["google_rating"].observed_value == 4.7
    assert evidence["google_review_count"].observed_value == 128
    assert evidence["google_business_status"].observed_value == "OPERATIONAL"
    assert evidence["google_address"].observed_value == _place()["formattedAddress"]
    assert evidence["google_phone"].observed_value == "+1 415-555-0100"
    assert evidence["google_website"].observed_value == _place()["websiteUri"]
    assert evidence["google_category"].observed_value == ["Dentist", "dentist", "health"]
    assert evidence["google_opening_hours"].observed_value == ["Monday: 9 AM to 5 PM"]
    assert all(item.source.provider.name == "google_places" for item in result.evidence)
    assert all(item.source.source_type.value == "business_directory" for item in result.evidence)
    assert all(item.external_provider_reference == "place-123" for item in result.evidence)
    assert all(item.enrichment_run_id == lead.enrichment_run_id for item in result.evidence)
    assert all(item.confidence >= 0.72 for item in result.evidence)
    assert calls[0]["url"] == PLACES_SEARCH_TEXT_URL
    assert calls[0]["headers"]["X-Goog-FieldMask"] == PLACES_FIELD_MASK
    assert calls[0]["headers"]["X-Goog-Api-Key"] == "google-secret-value"
    assert "api_key" not in str(calls[0]["json"]).lower()
    assert calls[0]["json"]["textQuery"] == "Northwind Dental, 10 Market Street, San Francisco, CA, USA"
    assert provider.client_factory(timeout=3.5).timeout == 3.5
    usage = await _rows(session_factory, ProviderUsage)
    assert len(usage) == 1 and usage[0].provider == "google_places" and usage[0].operation == "text_search_test"
    assert usage[0].success and usage[0].http_status == 200 and usage[0].key_fingerprint is not None


@pytest.mark.asyncio
async def test_exact_name_plus_city_is_a_strong_match_without_website_or_phone(session_factory, monkeypatch):
    provider = await _configured_provider(session_factory, monkeypatch, response=_response(payload={"places": [_place(websiteUri=None, nationalPhoneNumber=None, internationalPhoneNumber=None)]}))
    result = await provider.collect(_lead(website=None, phone=None))
    assert result.status == ProviderResultStatus.SUCCEEDED
    assert result.metadata.attributes["match_confidence"] >= 0.72


@pytest.mark.asyncio
async def test_ambiguous_matches_emit_no_business_evidence(session_factory, monkeypatch):
    provider = await _configured_provider(session_factory, monkeypatch, response=_response(payload={"places": [
        _place(websiteUri=None, nationalPhoneNumber=None, internationalPhoneNumber=None, formattedAddress="1 First Street"),
        _place(id="place-456", websiteUri=None, nationalPhoneNumber=None, internationalPhoneNumber=None, formattedAddress="2 Second Street"),
    ]}))
    result = await provider.collect(_lead(website=None, phone=None, address=None, city=None, state=None, country=None))
    assert result.status == ProviderResultStatus.FAILED
    assert result.errors[0].code == "AMBIGUOUS_MATCH"
    assert result.metadata.attributes["match_status"] == "ambiguous_match"
    assert result.metadata.attributes["candidate_place_id"] == "place-123"
    assert not result.evidence and not result.observations


@pytest.mark.asyncio
async def test_no_match_is_distinct_from_provider_failure_and_emits_no_evidence(session_factory, monkeypatch):
    provider = await _configured_provider(session_factory, monkeypatch, response=_response(payload={"places": [_place(displayName={"text": "Different Company"}, websiteUri=None, internationalPhoneNumber=None)]}))
    result = await provider.collect(_lead(website=None, phone=None, address=None, city=None, state=None, country=None))
    assert result.status == ProviderResultStatus.FAILED
    assert result.errors[0].code == "NO_MATCH" and result.errors[0].error_type == ProviderErrorType.PERMANENT
    assert result.metadata.attributes["match_status"] == "no_match"
    assert not result.evidence


@pytest.mark.asyncio
async def test_conflicting_website_and_phone_prevent_an_otherwise_similar_match(session_factory, monkeypatch):
    provider = await _configured_provider(session_factory, monkeypatch, response=_response(payload={"places": [
        _place(websiteUri="https://different-business.example", internationalPhoneNumber="+1 212-555-0100"),
    ]}))
    result = await provider.collect(_lead())
    assert result.status == ProviderResultStatus.FAILED
    assert result.metadata.attributes["match_status"] == "no_match"
    assert not result.evidence and not result.observations


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [[], {"places": "invalid"}, {"places": [{"id": "missing-name"}]}])
async def test_malformed_response_shapes_are_invalid_response(session_factory, monkeypatch, payload):
    provider = await _configured_provider(session_factory, monkeypatch, response=_response(payload=payload))
    result = await provider.collect(_lead())
    assert result.status == ProviderResultStatus.FAILED
    assert result.errors[0].error_type == ProviderErrorType.INVALID_RESPONSE
    assert not result.evidence


@pytest.mark.asyncio
async def test_malformed_json_is_classified_inside_provider_execution(session_factory, monkeypatch):
    provider = await _configured_provider(session_factory, monkeypatch, response=_response(content=b"not-json"))
    result = await provider.collect(_lead())
    assert result.errors[0].error_type == ProviderErrorType.INVALID_RESPONSE
    usage = (await _rows(session_factory, ProviderUsage))[0]
    assert usage.outcome == "invalid_response" and usage.http_status is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body,error_type,outcome", [
    (401, {"error": "bad credentials"}, ProviderErrorType.AUTHENTICATION, "authentication"),
    (403, {"error": "daily quota exceeded"}, ProviderErrorType.QUOTA_EXHAUSTED, "quota_exhausted"),
    (429, {"error": "rate limit"}, ProviderErrorType.RATE_LIMITED, "rate_limited"),
    (503, {"error": "temporary"}, ProviderErrorType.TRANSIENT, "transient"),
    (400, {"error": "invalid request"}, ProviderErrorType.PERMANENT, "permanent"),
])
async def test_http_error_classification_and_usage(session_factory, monkeypatch, status, body, error_type, outcome):
    provider = await _configured_provider(session_factory, monkeypatch, response=_response(status=status, payload=body, headers={"Retry-After": "17"} if status == 429 else None), retries=1)
    result = await provider.collect(_lead())
    assert result.status == ProviderResultStatus.FAILED
    assert result.errors[0].error_type == error_type
    if status == 429:
        assert result.errors[0].retry_after_seconds == 17
    usage = await _rows(session_factory, ProviderUsage)
    assert all(row.outcome == outcome for row in usage)
    assert not result.evidence


@pytest.mark.asyncio
async def test_rate_limit_honors_retry_after_cooldown(session_factory, monkeypatch):
    provider = await _configured_provider(session_factory, monkeypatch, response=_response(status=429, payload={"error": "rate limit"}, headers={"Retry-After": "17"}))
    result = await provider.collect(_lead())
    assert result.errors[0].retry_after_seconds == 17
    async with session_factory() as session:
        key = await session.scalar(select(ApiKey).where(ApiKey.provider == "google_places"))
        assert key.cooldown_until is not None
        assert key.cooldown_until.replace(tzinfo=timezone.utc) > now_utc()


@pytest.mark.asyncio
async def test_timeout_and_transport_errors_are_normalized(session_factory, monkeypatch):
    for exception, expected in [(httpx.ReadTimeout("private timeout detail"), ProviderErrorType.TIMEOUT), (httpx.ConnectError("private endpoint detail"), ProviderErrorType.TRANSIENT)]:
        provider = await _configured_provider(session_factory, monkeypatch, exception=exception)
        result = await provider.collect(_lead())
        assert result.status == ProviderResultStatus.FAILED and result.errors[0].error_type == expected


@pytest.mark.asyncio
async def test_disabled_and_unconfigured_provider_do_not_make_requests(session_factory, monkeypatch):
    monkeypatch.setattr(settings, "google_places_api_key", "")
    disabled = GooglePlacesProvider(ProviderExecutionService(session_factory), enabled=False)
    result = await disabled.collect(_lead())
    assert result.errors[0].code == "PROVIDER_DISABLED"
    enabled_without_key = GooglePlacesProvider(ProviderExecutionService(session_factory), enabled=True)
    result = await enabled_without_key.collect(_lead())
    assert result.errors[0].code == "MISSING_API_KEY"
    assert await _rows(session_factory, ProviderUsage) == []


@pytest.mark.asyncio
async def test_secrets_and_raw_response_are_not_returned_or_persisted(session_factory, monkeypatch, caplog):
    secret = "google-secret-value"
    payload = {"places": [_place(), {"privateProviderField": secret}]}
    provider = await _configured_provider(session_factory, monkeypatch, response=_response(payload=payload))
    with caplog.at_level(logging.WARNING):
        result = await provider.collect(_lead())
    assert secret not in result.model_dump_json() and secret not in caplog.text
    assert secret not in str(await _rows(session_factory, ProviderUsage))
    evidence_rows = await _rows(session_factory, Evidence)
    assert evidence_rows == []  # The adapter returns evidence; it never persists raw HTTP objects or evidence.
    assert all("privateProviderField" not in str(item.observed_value) for item in result.evidence)


@pytest.mark.asyncio
async def test_environment_key_sync_registers_google_key(monkeypatch, session_factory):
    from app.services.key_config import sync_environment_keys

    monkeypatch.setattr(settings, "google_places_api_key", "env-google-secret")
    async with session_factory() as session:
        await sync_environment_keys(session)
    async with session_factory() as session:
        key = await session.scalar(select(ApiKey).where(ApiKey.provider == "google_places"))
    assert key is not None and key.key == "env-google-secret"
