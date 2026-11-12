"""
REST API Router for Provider Delivery Webhook Ingress.

Phase 12J - D2 - Gate D-C4:
Provides secure ingress endpoints for Resend and SendGrid delivery callbacks.
Enforces raw-body cryptographic signature verification, replay protection,
payload size limits, PII masking, append-only persistence, and Fast-ACK.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Mapping

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.entities import SupervisorApprovalDispatch
from app.repositories.supervisor_delivery_event_repository import (
    SupervisorDeliveryEventRepository,
)
from app.services.webhooks.normalizer import (
    NormalizedDeliveryEvent,
    normalize_resend_event,
    normalize_sendgrid_event,
)
from app.services.webhooks.resend_verifier import ResendWebhookVerifier
from app.services.webhooks.sendgrid_verifier import SendGridWebhookVerifier
from app.services.webhooks.signature_verifier import (
    WebhookConfigurationError,
    WebhookSignatureInvalidError,
    WebhookSignatureMissingError,
    WebhookTimestampExpiredError,
)

logger = logging.getLogger("lead_agent.api.delivery_webhooks")

router = APIRouter(prefix="/api/sales-supervisor/webhooks/delivery", tags=["Sales Supervisor Delivery Webhooks"])

MAX_WEBHOOK_PAYLOAD_BYTES = 1_048_576  # 1 MB


async def _correlate_dispatch(
    session: AsyncSession,
    event: NormalizedDeliveryEvent,
) -> tuple[uuid.UUID | None, uuid.UUID | None, uuid.UUID | None]:
    """
    Attempts to match an incoming event to an existing outbox dispatch record
    using correlation_dispatch_id (from metadata/custom_args) or provider_message_id.
    Returns (dispatch_id, dispatch_reference_id, lead_reference_id).
    """
    dispatch = None

    # 1. Match by explicit correlation_dispatch_id
    if event.correlation_dispatch_id:
        try:
            d_uuid = uuid.UUID(event.correlation_dispatch_id)
            stmt = select(SupervisorApprovalDispatch).where(SupervisorApprovalDispatch.id == d_uuid)
            res = await session.execute(stmt)
            dispatch = res.scalar_one_or_none()
        except (ValueError, TypeError):
            pass

    # 2. Match by provider_message_id if not found yet
    if not dispatch and event.provider_message_id:
        stmt = (
            select(SupervisorApprovalDispatch)
            .where(SupervisorApprovalDispatch.provider_message_id == event.provider_message_id)
            .order_by(SupervisorApprovalDispatch.created_at.desc())
        )
        res = await session.execute(stmt)
        dispatch = res.scalar_one_or_none()

    if dispatch:
        return dispatch.id, dispatch.id, dispatch.lead_reference_id

    return None, None, None


@router.post(
    "/{provider}",
    summary="Ingest asynchronous delivery webhooks from communication providers",
    status_code=status.HTTP_200_OK,
)
async def ingest_delivery_webhook(
    provider: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Ingest endpoint for provider delivery webhooks.
    Enforces:
    - Bounded payload size (<= 1MB)
    - Strict Content-Type check (application/json)
    - Cryptographic signature & replay window validation
    - Durable persistence before acknowledgement (Fast-ACK)
    - Zero synchronous reconciliation or outbound triggers
    """
    provider_key = (provider or "").strip().lower()
    if provider_key not in {"resend", "sendgrid"}:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "UNSUPPORTED_PROVIDER", "message": f"Provider '{provider}' is not supported"},
        )

    # 1. Content-Type check
    content_type = request.headers.get("content-type", "").lower()
    if not content_type.startswith("application/json"):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail={"code": "UNSUPPORTED_CONTENT_TYPE", "message": "Content-Type must be application/json"},
        )

    # 2. Payload size check
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_WEBHOOK_PAYLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={"code": "PAYLOAD_TOO_LARGE", "message": "Webhook payload exceeds 1MB limit"},
        )

    raw_body = await request.body()
    if len(raw_body) > MAX_WEBHOOK_PAYLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={"code": "PAYLOAD_TOO_LARGE", "message": "Webhook payload exceeds 1MB limit"},
        )

    # 3. Cryptographic Signature Verification
    try:
        if provider_key == "resend":
            secret = getattr(settings, "outreach_resend_webhook_secret", "")
            verifier = ResendWebhookVerifier(secret=secret)
            verifier.verify(raw_body=raw_body, headers=request.headers)
        elif provider_key == "sendgrid":
            public_key = getattr(settings, "outreach_sendgrid_webhook_public_key", "")
            verifier = SendGridWebhookVerifier(public_key=public_key)
            verifier.verify(raw_body=raw_body, headers=request.headers)
    except WebhookSignatureMissingError as exc:
        logger.warning("Webhook signature missing for %s: %s", provider_key, exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except WebhookSignatureInvalidError as exc:
        logger.warning("Webhook signature invalid for %s: %s", provider_key, exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except WebhookTimestampExpiredError as exc:
        logger.warning("Webhook timestamp expired for %s: %s", provider_key, exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except WebhookConfigurationError as exc:
        logger.error("Webhook verifier configuration error for %s: %s", provider_key, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": exc.code, "message": "Webhook verification service temporarily unavailable"},
        ) from exc

    # 4. JSON Deserialization
    try:
        parsed_json = json.loads(raw_body.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "MALFORMED_JSON", "message": f"Malformed JSON in webhook body: {exc}"},
        ) from exc

    # 5. Normalization & Persistence
    normalized_events: list[NormalizedDeliveryEvent] = []
    if provider_key == "resend":
        if isinstance(parsed_json, dict):
            normalized_events.append(normalize_resend_event(parsed_json, dict(request.headers)))
    elif provider_key == "sendgrid":
        if isinstance(parsed_json, list):
            for item in parsed_json:
                if isinstance(item, dict):
                    normalized_events.append(normalize_sendgrid_event(item))
        elif isinstance(parsed_json, dict):
            normalized_events.append(normalize_sendgrid_event(parsed_json))

    persisted_count = 0
    duplicate_count = 0

    try:
        for event in normalized_events:
            # Skip ignored events (e.g. opens / clicks per DC3-02)
            if event.is_ignored_event:
                continue

            # Correlate matching outbox dispatch
            dispatch_id, dispatch_ref, lead_ref = await _correlate_dispatch(db, event)

            # Persist normalized event
            _, is_duplicate = await SupervisorDeliveryEventRepository.record_delivery_event(
                db,
                provider_name=event.provider_name,
                provider_message_id=event.provider_message_id,
                provider_event_id=event.provider_event_id,
                event_type=event.event_type,
                raw_event_type=event.raw_event_type,
                event_timestamp=event.event_timestamp,
                dispatch_id=dispatch_id,
                dispatch_reference_id=dispatch_ref,
                lead_reference_id=lead_ref,
                recipient_masked=event.recipient_masked,
                bounce_type=event.bounce_type,
                bounce_code=event.bounce_code,
                bounce_reason=event.bounce_reason,
                metadata_json=event.metadata_json,
            )
            if is_duplicate:
                duplicate_count += 1
            else:
                persisted_count += 1

        await db.commit()
    except Exception as exc:
        await db.rollback()
        logger.exception("Failed to persist delivery events: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "PERSISTENCE_FAILED", "message": "Failed to persist delivery events"},
        ) from exc

    return {
        "status": "accepted",
        "persisted": persisted_count,
        "duplicates": duplicate_count,
        "ignored": len([e for e in normalized_events if e.is_ignored_event]),
    }
