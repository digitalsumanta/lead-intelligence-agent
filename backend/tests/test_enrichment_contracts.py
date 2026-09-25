"""Validation tests for the transport- and database-neutral enrichment domain."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.domain.enrichment import (
    EnrichmentRun,
    EnrichmentRunStatus,
    EvidenceInput,
    EvidenceRecord,
    EvidenceSource,
    EvidenceSourceType,
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


NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
LEAD_ID = uuid4()
PROVIDER = ProviderIdentity(name="website", kind=ProviderKind.WEBSITE_ANALYZER)
METADATA = ProviderRequestMetadata(operation="inspect", started_at=NOW, completed_at=NOW)


def evidence(**overrides) -> EvidenceInput:
    values = {
        "lead_id": LEAD_ID,
        "evidence_type": "website.title",
        "source": EvidenceSource(
            source_type=EvidenceSourceType.WEBSITE,
            provider=PROVIDER,
        ),
        "source_url": "https://example.test/",
        "observed_value": "Example Co",
        "observed_at": NOW,
        "fetched_at": NOW,
        "expires_at": NOW + timedelta(days=1),
        "confidence": 1.0,
    }
    values.update(overrides)
    return EvidenceInput(**values)


def test_successful_provider_result_contains_normalized_observation_and_evidence():
    result = ProviderResult(
        provider=PROVIDER,
        status=ProviderResultStatus.SUCCEEDED,
        observations=(NormalizedObservation(
            key="website.https",
            value=True,
            kind=ObservationKind.DETERMINISTIC,
            observed_at=NOW,
            confidence=1,
        ),),
        evidence=(evidence(),),
        metadata=METADATA,
    )

    assert result.status == ProviderResultStatus.SUCCEEDED
    assert result.observations[0].value is True
    assert result.evidence[0].source.source_type == EvidenceSourceType.WEBSITE


def test_partial_provider_result_requires_output_and_a_typed_error():
    result = ProviderResult(
        provider=ProviderIdentity(name="pagespeed", kind=ProviderKind.EXTERNAL_DATA),
        status=ProviderResultStatus.PARTIAL,
        observations=(NormalizedObservation(
            key="performance.mobile.score",
            value=0.8,
            kind=ObservationKind.FACT,
            observed_at=NOW,
        ),),
        metadata=METADATA,
        errors=(ProviderError(error_type=ProviderErrorType.RATE_LIMITED, message="One category was unavailable"),),
    )

    assert result.observations and result.errors[0].error_type == ProviderErrorType.RATE_LIMITED


@pytest.mark.parametrize("error_type", list(ProviderErrorType))
def test_each_provider_error_category_is_representable(error_type):
    error = ProviderError(error_type=error_type, message="Provider failure")
    restored = ProviderError.model_validate_json(error.model_dump_json())
    assert restored.error_type == error_type


def test_evidence_requires_identity_source_and_timezone_aware_freshness_fields():
    item = evidence(enrichment_run_id=uuid4(), external_provider_reference="listing-42")
    assert item.lead_id == LEAD_ID
    assert item.enrichment_run_id is not None
    assert item.fetched_at.tzinfo is not None and item.expires_at is not None
    with pytest.raises(ValidationError, match="timezone-aware"):
        evidence(observed_at=datetime(2026, 9, 26))


@pytest.mark.parametrize("confidence", [-0.01, 1.01])
def test_evidence_and_observation_confidence_are_bounded(confidence):
    with pytest.raises(ValidationError):
        evidence(confidence=confidence)
    with pytest.raises(ValidationError):
        NormalizedObservation(key="x", value=1, kind=ObservationKind.FACT, observed_at=NOW, confidence=confidence)


def test_evidence_rejects_expiry_before_fetch_and_bad_content_hash():
    with pytest.raises(ValidationError, match="expires_at"):
        evidence(expires_at=NOW - timedelta(seconds=1))
    with pytest.raises(ValidationError, match="content_hash"):
        evidence(content_hash="not-a-sha256-hash")


def test_evidence_record_round_trips_json_with_persisted_identity():
    record = EvidenceRecord.model_validate({**evidence().model_dump(), "id": uuid4()})
    restored = EvidenceRecord.model_validate_json(record.model_dump_json())

    assert restored == record
    assert restored.id == record.id


def test_ai_model_cannot_be_marked_as_verified_evidence():
    ai_identity = ProviderIdentity(name="ollama", kind=ProviderKind.AI_MODEL)
    with pytest.raises(ValidationError, match="AI model output"):
        EvidenceSource(source_type=EvidenceSourceType.OTHER_VERIFIED_SOURCE, provider=ai_identity)

    with pytest.raises(ValidationError, match="AI inference providers"):
        ProviderResult(provider=ai_identity, status=ProviderResultStatus.SUCCEEDED, metadata=METADATA)


def test_enrichment_run_contract_captures_lifecycle_and_rejects_unrequested_stage():
    run = EnrichmentRun(
        lead_id=LEAD_ID,
        processing_job_id=uuid4(),
        status=EnrichmentRunStatus.RUNNING,
        requested_stages=("website", "places"),
        completed_stages=("website",),
        started_at=NOW,
        pipeline_version="phase2-v1",
    )
    assert run.completed_stages == ("website",)
    with pytest.raises(ValidationError, match="requested stages"):
        EnrichmentRun(
            lead_id=LEAD_ID,
            status=EnrichmentRunStatus.RUNNING,
            requested_stages=("website",),
            completed_stages=("pagespeed",),
            pipeline_version="phase2-v1",
        )
