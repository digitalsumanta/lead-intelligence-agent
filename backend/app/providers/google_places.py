"""Google Places (New) enrichment adapter.

The adapter owns HTTP details and normalization. Credential leasing, retries,
key cooldowns, and usage recording remain the responsibility of
ProviderExecutionService.
"""

from __future__ import annotations

from datetime import datetime
from difflib import SequenceMatcher
import math
import re
import time
from typing import Any
from urllib.parse import urlparse

import httpx

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


PLACES_SEARCH_TEXT_URL = "https://places.googleapis.com/v1/places:searchText"
PLACES_FIELD_MASK = ",".join((
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.nationalPhoneNumber",
    "places.internationalPhoneNumber",
    "places.websiteUri",
    "places.rating",
    "places.userRatingCount",
    "places.businessStatus",
    "places.googleMapsUri",
    "places.types",
    "places.primaryTypeDisplayName",
    "places.regularOpeningHours.weekdayDescriptions",
))
MATCH_THRESHOLD = 0.72
AMBIGUITY_MARGIN = 0.08

GOOGLE_PLACES_IDENTITY = ProviderIdentity(
    name="google_places",
    kind=ProviderKind.EXTERNAL_DATA,
    implementation="places_api_new",
)


def _clean_text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _name_key(value: str | None) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (value or "").casefold()))


def _host(value: str | None) -> str | None:
    if not value:
        return None
    candidate = value.strip()
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    try:
        hostname = (urlparse(candidate).hostname or "").casefold().removeprefix("www.")
    except ValueError:
        return None
    return hostname or None


def _phone_key(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def _place_name(place: dict[str, Any]) -> str | None:
    display = place.get("displayName")
    if isinstance(display, dict):
        return _clean_text(display.get("text"))
    return None


def _place_fields(place: dict[str, Any]) -> dict[str, Any]:
    """Select and normalize only fields used for lead intelligence."""
    primary = place.get("primaryTypeDisplayName")
    categories: list[str] = []
    if isinstance(primary, dict) and (label := _clean_text(primary.get("text"))):
        categories.append(label)
    types = place.get("types")
    if isinstance(types, list):
        categories.extend(item for item in types if isinstance(item, str) and item not in categories)
    hours = place.get("regularOpeningHours")
    weekdays = hours.get("weekdayDescriptions") if isinstance(hours, dict) else None
    rating = place.get("rating")
    if not isinstance(rating, (int, float)) or isinstance(rating, bool) or not math.isfinite(rating) or not 0 <= rating <= 5:
        rating = None
    review_count = place.get("userRatingCount")
    if not isinstance(review_count, int) or isinstance(review_count, bool) or review_count < 0:
        review_count = None
    return {
        "google_place_id": _clean_text(place.get("id")),
        "google_business_name": _place_name(place),
        "google_address": _clean_text(place.get("formattedAddress")),
        "google_phone": _clean_text(place.get("internationalPhoneNumber")) or _clean_text(place.get("nationalPhoneNumber")),
        "google_website": _clean_text(place.get("websiteUri")),
        "google_rating": rating,
        "google_review_count": review_count,
        "google_business_status": _clean_text(place.get("businessStatus")),
        "google_maps_url": _clean_text(place.get("googleMapsUri")),
        "google_category": categories or None,
        "google_opening_hours": [item.strip() for item in weekdays if isinstance(item, str) and item.strip()] if isinstance(weekdays, list) else None,
    }


def _match_score(lead: LeadInput, fields: dict[str, Any]) -> tuple[float, float]:
    expected_name = _name_key(lead.company_name)
    actual_name = _name_key(fields.get("google_business_name"))
    name_similarity = SequenceMatcher(None, expected_name, actual_name).ratio() if expected_name and actual_name else 0.0
    score = 0.60 * name_similarity

    lead_host, place_host = _host(lead.website), _host(fields.get("google_website"))
    if lead_host and place_host and lead_host == place_host:
        score += 0.25
    elif lead_host and place_host:
        score -= 0.30

    lead_phone, place_phone = _phone_key(getattr(lead, "phone", None)), _phone_key(fields.get("google_phone"))
    if lead_phone and place_phone and (lead_phone == place_phone or (len(lead_phone) >= 10 and len(place_phone) >= 10 and lead_phone[-10:] == place_phone[-10:])):
        score += 0.25
    elif lead_phone and place_phone:
        score -= 0.30

    address = _name_key(fields.get("google_address"))
    locality_values = [lead.address, lead.city, lead.state, lead.country]
    locality_tokens = [_name_key(value) for value in locality_values if value and _name_key(value)]
    if address and locality_tokens:
        if any(token in address for token in locality_tokens):
            score += 0.15

    return min(1.0, max(0.0, score)), name_similarity


class GooglePlacesProvider:
    """Conservative business matcher backed by Places API (New) Text Search."""

    name = "google_places"

    def __init__(
        self,
        execution: ProviderExecutionService,
        *,
        enabled: bool | None = None,
        request_timeout_seconds: float | None = None,
        operation: str | None = None,
        client_factory=httpx.AsyncClient,
    ) -> None:
        self.execution = execution
        self.enabled = settings.google_places_enabled if enabled is None else enabled
        self.api_key_configured = bool(settings.google_places_api_key.strip())
        self.timeout = request_timeout_seconds if request_timeout_seconds is not None else settings.google_places_request_timeout_seconds
        self.operation = operation or settings.google_places_operation
        self.client_factory = client_factory
        if self.timeout <= 0:
            raise ValueError("Google Places request timeout must be positive")

    async def collect(self, lead: LeadInput) -> ProviderResult:
        started_at = now_utc()
        timer = time.perf_counter()
        if not self.enabled:
            return self._failure(lead, "PROVIDER_DISABLED", "Google Places provider is disabled", ProviderErrorType.PERMANENT, started_at, timer)
        if not self.api_key_configured:
            return self._failure(lead, "MISSING_API_KEY", "Google Places API key is not configured", ProviderErrorType.PERMANENT, started_at, timer)

        query = ", ".join(value.strip() for value in (lead.company_name, lead.address, lead.city, lead.state, lead.country) if value and value.strip())
        response: httpx.Response | None = None
        try:
            async with self.client_factory(timeout=self.timeout) as client:
                async def request(credential: str | None) -> httpx.Response:
                    if not credential:
                        raise RuntimeError("provider execution returned no Google Places key")
                    result = await client.post(
                        PLACES_SEARCH_TEXT_URL,
                        headers={
                            "X-Goog-Api-Key": credential,
                            "X-Goog-FieldMask": PLACES_FIELD_MASK,
                            "Content-Type": "application/json",
                        },
                        json={"textQuery": query, "pageSize": 5},
                    )
                    # Parse while inside the execution boundary so malformed JSON
                    # is classified and recorded as invalid_response.
                    if 200 <= result.status_code < 300:
                        result.json()
                    return result

                response = await self.execution.execute(
                    "google_places",
                    self.operation,
                    request,
                    requires_api_key=True,
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
                f"GOOGLE_{exc.outcome.value.upper()}",
                f"Google Places request failed ({exc.outcome.value})",
                category,
                started_at,
                timer,
                status=exc.http_status,
                attempt=max(1, exc.attempts),
                retry_after=exc.retry_after_seconds,
            )
        except (httpx.TimeoutException, TimeoutError):
            return self._failure(lead, "GOOGLE_TIMEOUT", "Google Places request timed out", ProviderErrorType.TIMEOUT, started_at, timer)
        except (httpx.TransportError, OSError, ConnectionError):
            return self._failure(lead, "GOOGLE_TRANSPORT", "Google Places transport failed", ProviderErrorType.TRANSIENT, started_at, timer)
        except (ValueError, TypeError):
            return self._failure(lead, "INVALID_RESPONSE", "Google Places returned malformed JSON", ProviderErrorType.INVALID_RESPONSE, started_at, timer, status=response.status_code if response else None)

        if not isinstance(payload, dict):
            return self._failure(lead, "INVALID_RESPONSE", "Google Places returned an invalid response structure", ProviderErrorType.INVALID_RESPONSE, started_at, timer, status=response.status_code)
        raw_places = payload.get("places", [])
        if not isinstance(raw_places, list):
            return self._failure(lead, "INVALID_RESPONSE", "Google Places returned an invalid places list", ProviderErrorType.INVALID_RESPONSE, started_at, timer, status=response.status_code)
        candidates = [(_place_fields(item), item) for item in raw_places if isinstance(item, dict) and _clean_text(item.get("id")) and _place_name(item)]
        if raw_places and not candidates:
            return self._failure(lead, "INVALID_RESPONSE", "Google Places returned no usable place records", ProviderErrorType.INVALID_RESPONSE, started_at, timer, status=response.status_code)
        if not candidates:
            return self._match_failure(lead, "no_match", "No Google Places business matched the lead", None, 0.0, response, started_at, timer)

        ranked = sorted(((*_match_score(lead, fields), fields, raw) for fields, raw in candidates), key=lambda item: item[0], reverse=True)
        best_score, best_name_similarity, best_fields, best_raw = ranked[0]
        second_score = ranked[1][0] if len(ranked) > 1 else 0.0
        ambiguous = len(ranked) > 1 and best_name_similarity >= 0.85 and abs(best_score - second_score) < AMBIGUITY_MARGIN
        if ambiguous or best_score < MATCH_THRESHOLD:
            status = "ambiguous_match" if ambiguous else "no_match"
            reason = "Multiple Google Places businesses are similarly plausible" if ambiguous else "Candidate confidence is below the safe match threshold"
            return self._match_failure(lead, status, reason, best_fields, best_score, response, started_at, timer, candidate_count=len(ranked))

        return self._matched_result(lead, best_fields, best_score, response, started_at, timer)

    def _metadata(
        self,
        started_at: datetime,
        timer: float,
        *,
        status: int | None = None,
        attempt: int = 1,
        attributes: dict[str, str | int | float | bool | None] | None = None,
    ) -> ProviderRequestMetadata:
        completed = now_utc()
        return ProviderRequestMetadata(
            operation=self.operation,
            attempt=max(1, attempt),
            duration_ms=max(0, int((time.perf_counter() - timer) * 1000)),
            started_at=started_at,
            completed_at=completed,
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
    ) -> ProviderResult:
        error = ProviderError(error_type=error_type, code=code, message=message, retry_after_seconds=retry_after)
        return ProviderResult(
            provider=GOOGLE_PLACES_IDENTITY,
            status=ProviderResultStatus.FAILED,
            metadata=self._metadata(started_at, timer, status=status, attempt=attempt),
            errors=(error,),
        )

    def _match_failure(
        self,
        lead: LeadInput,
        status: str,
        reason: str,
        candidate: dict[str, Any] | None,
        confidence: float,
        response: httpx.Response,
        started_at: datetime,
        timer: float,
        *,
        candidate_count: int = 0,
    ) -> ProviderResult:
        code = "AMBIGUOUS_MATCH" if status == "ambiguous_match" else "NO_MATCH"
        error = ProviderError(error_type=ProviderErrorType.PERMANENT, code=code, message=reason)
        attributes: dict[str, str | int | float | bool | None] = {
            "match_status": status,
            "match_reason": reason,
            "match_confidence": round(confidence, 4),
            "candidate_count": candidate_count,
            "candidate_place_id": candidate.get("google_place_id") if candidate else None,
            "candidate_business_name": candidate.get("google_business_name") if candidate else None,
        }
        return ProviderResult(
            provider=GOOGLE_PLACES_IDENTITY,
            status=ProviderResultStatus.FAILED,
            metadata=self._metadata(started_at, timer, status=response.status_code, attributes=attributes),
            errors=(error,),
        )

    def _matched_result(
        self,
        lead: LeadInput,
        fields: dict[str, Any],
        confidence: float,
        response: httpx.Response,
        started_at: datetime,
        timer: float,
    ) -> ProviderResult:
        fetched_at = now_utc()
        place_id = fields["google_place_id"]
        maps_url = fields.get("google_maps_url")
        source_url = maps_url or f"https://places.googleapis.com/v1/places/{place_id}"
        source = EvidenceSource(source_type=EvidenceSourceType.BUSINESS_DIRECTORY, provider=GOOGLE_PLACES_IDENTITY)
        evidence: list[EvidenceInput] = []
        observations: list[NormalizedObservation] = []
        for key, value in fields.items():
            if value is None:
                continue
            evidence.append(EvidenceInput(
                lead_id=lead.lead_id,
                enrichment_run_id=lead.enrichment_run_id,
                evidence_type=key,
                source=source,
                source_url=source_url,
                external_provider_reference=place_id,
                observed_value=value,
                observed_at=fetched_at,
                fetched_at=fetched_at,
                confidence=round(confidence, 4),
                metadata={"match_status": "strong_match", "api": "places_api_new"},
            ))
            observations.append(NormalizedObservation(
                key=key,
                value=value,
                kind=ObservationKind.FACT,
                observed_at=fetched_at,
                confidence=round(confidence, 4),
            ))
        evidence.append(EvidenceInput(
            lead_id=lead.lead_id,
            enrichment_run_id=lead.enrichment_run_id,
            evidence_type="google_match_confidence",
            source=source,
            source_url=source_url,
            external_provider_reference=place_id,
            observed_value=round(confidence, 4),
            observed_at=fetched_at,
            fetched_at=fetched_at,
            confidence=round(confidence, 4),
            metadata={"match_status": "strong_match", "match_algorithm": "name_identity_location_v1"},
        ))
        observations.append(NormalizedObservation(
            key="google_match_confidence",
            value=round(confidence, 4),
            kind=ObservationKind.DETERMINISTIC,
            observed_at=fetched_at,
            confidence=round(confidence, 4),
        ))
        metadata = self._metadata(
            started_at,
            timer,
            status=response.status_code,
            attributes={
                "match_status": "strong_match",
                "match_reason": "Business name matched and an independent identifier or location corroborated it",
                "match_confidence": round(confidence, 4),
                "place_id": place_id,
                "matched_business_name": fields.get("google_business_name"),
            },
        )
        return ProviderResult(
            provider=GOOGLE_PLACES_IDENTITY,
            status=ProviderResultStatus.SUCCEEDED,
            observations=tuple(observations),
            evidence=tuple(evidence),
            metadata=metadata,
        )
