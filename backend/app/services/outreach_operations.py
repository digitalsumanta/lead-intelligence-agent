"""Explicit, human-triggered sends and normalized delivery-event handling."""
import re
import uuid
from datetime import timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.outreach import OutreachContent
from app.models import (
    Lead, OutreachArtifact, OutreachExecution, OutreachProviderEvent,
    OutreachStatus, SalesActivity, SalesActivityType,
)
from app.models.entities import now_utc
from app.providers.outreach import (
    OutreachMessage, OutreachProviderFailure, resolve_outreach_provider,
)
from app.schemas.outreach_operations import (
    ManualSendConfirmation, OutreachProviderEventIn, OutreachRetryRequest,
    OutreachSendRequest,
)
from app.services.lead_intelligence_api import read_lead_intelligence
from app.services.outreach import source_state, validate_outreach

ACTIVE_EXECUTION_STATES = ("QUEUED", "SENDING", "SENT", "DELIVERED")
EMAIL_RE = re.compile(r"^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$")
PHONE_FORMAT_RE = re.compile(r"^\+[1-9][0-9\s().-]{6,24}$")


class OutreachOperationError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 409):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _safe_provider_failure(error: OutreachProviderFailure, channel: str) -> tuple[str, str, int]:
    messages = {
        "PROVIDER_DISABLED": f"{channel.title()} sending is disabled",
        "PROVIDER_NOT_CONFIGURED": f"No complete {channel.title()} provider configuration is available",
        "PROVIDER_CONFIGURATION_INVALID": "The configured outreach provider is unavailable",
        "PROVIDER_TIMEOUT": "The configured outreach provider timed out",
        "PROVIDER_UNAVAILABLE": "The configured outreach provider could not be reached",
        "PROVIDER_REJECTED": "The configured outreach provider rejected the send request",
    }
    statuses = {
        "PROVIDER_DISABLED": 409, "PROVIDER_NOT_CONFIGURED": 503,
        "PROVIDER_CONFIGURATION_INVALID": 503, "PROVIDER_TIMEOUT": 504,
        "PROVIDER_UNAVAILABLE": 502, "PROVIDER_REJECTED": 502,
    }
    code = error.code if error.code in messages else "PROVIDER_FAILURE"
    message = messages.get(code, "The outreach provider could not complete the send request")
    status_code = statuses.get(code, 502)
    return code, message, status_code


def _stored_failure_status(code: str | None) -> int:
    return {
        "PROVIDER_DISABLED": 409, "PROVIDER_NOT_CONFIGURED": 503,
        "PROVIDER_CONFIGURATION_INVALID": 503, "PROVIDER_TIMEOUT": 504,
        "PROVIDER_UNAVAILABLE": 502, "PROVIDER_REJECTED": 502,
    }.get(code or "", 502)


def _recipient(lead: Lead, channel: str) -> str:
    if channel == "EMAIL":
        value = (lead.email or "").strip()
        if not EMAIL_RE.fullmatch(value):
            raise OutreachOperationError("INVALID_EMAIL_DESTINATION", "The lead does not have a valid email destination", 422)
        return value
    if channel == "WHATSAPP":
        value = (lead.phone or "").strip()
        digits = re.sub(r"\D", "", value)
        if not PHONE_FORMAT_RE.fullmatch(value) or not 8 <= len(digits) <= 15:
            raise OutreachOperationError("INVALID_WHATSAPP_DESTINATION", "The lead phone must be a valid international E.164 number for WhatsApp", 422)
        return "+" + digits
    raise OutreachOperationError("INVALID_CHANNEL", "A delivery channel is required", 422)


def _compose(content: OutreachContent, channel: str) -> tuple[str | None, str]:
    if channel == "EMAIL":
        body = "\n\n".join(part for part in (
            content.email.opening.text, content.email.body.text, content.email.call_to_action.text,
        ) if part.strip())
        return content.email.subject, body
    if channel == "WHATSAPP":
        body = "\n\n".join(part for part in (
            content.whatsapp.opening.text, content.whatsapp.message.text,
            content.whatsapp.call_to_action.text,
        ) if part.strip())
        return None, body
    return None, ""


async def _approved_current_artifact(session: AsyncSession, artifact_id: uuid.UUID):
    artifact = await session.scalar(select(OutreachArtifact).where(OutreachArtifact.id == artifact_id).with_for_update())
    if artifact is None:
        raise OutreachOperationError("OUTREACH_NOT_FOUND", "Outreach artifact not found", 404)
    lead = await session.scalar(select(Lead).where(Lead.id == artifact.lead_id).with_for_update())
    if lead is None:
        raise OutreachOperationError("LEAD_NOT_FOUND", "Lead not found", 404)
    if artifact.status == OutreachStatus.STALE:
        raise OutreachOperationError("OUTREACH_STALE", "Outreach must be regenerated or revalidated before sending")
    if artifact.status != OutreachStatus.APPROVED:
        raise OutreachOperationError("OUTREACH_NOT_APPROVED", "Only approved outreach can be sent")
    intelligence = await read_lead_intelligence(session, lead.id)
    if intelligence is None:
        artifact.status = OutreachStatus.STALE
        artifact.updated_at = now_utc()
        await session.commit()
        raise OutreachOperationError("OUTREACH_STALE", "Outreach must be regenerated or revalidated before sending")
    fingerprint, _, _ = source_state(intelligence, lead)
    if fingerprint != artifact.source_fingerprint:
        artifact.status = OutreachStatus.STALE
        artifact.updated_at = now_utc()
        await session.commit()
        raise OutreachOperationError("OUTREACH_STALE", "Outreach must be regenerated or revalidated before sending")
    if not artifact.content:
        raise OutreachOperationError("OUTREACH_CONTENT_UNAVAILABLE", "Approved outreach content is unavailable")
    try:
        content = OutreachContent.model_validate(artifact.content)
        validate_outreach(content, intelligence, lead.sales_status)
    except (TypeError, ValueError):
        raise OutreachOperationError("OUTREACH_CONTENT_INVALID", "Approved outreach failed current validation") from None
    return artifact, lead, content


def _execution_snapshot(content: OutreachContent, channel: str, subject: str | None, text: str) -> dict:
    return {
        "content": content.model_dump(mode="json"),
        "selected_channel": channel,
        "subject": subject,
        "text": text,
    }


async def _by_idempotency_key(session: AsyncSession, artifact_id: uuid.UUID, key: str):
    return await session.scalar(select(OutreachExecution).where(
        OutreachExecution.outreach_artifact_id == artifact_id,
        OutreachExecution.idempotency_key == key,
    ))


def _activity_for_execution(execution: OutreachExecution, now) -> SalesActivity:
    if execution.channel == "MANUAL":
        activity_type = SalesActivityType(execution.manual_method) if execution.manual_method else SalesActivityType.OTHER
        channel_text = f"manual {execution.manual_method.lower()}" if execution.manual_method else "manual"
    else:
        activity_type = SalesActivityType(execution.channel)
        channel_text = execution.channel.lower()
    return SalesActivity(
        lead_id=execution.lead_id, activity_type=activity_type,
        outreach_execution_id=execution.id,
        note=f"Approved outreach confirmed sent via {channel_text}. Execution {execution.id}.",
        occurred_at=now,
    )


async def _send_attempt(session: AsyncSession, execution: OutreachExecution, *, provider=None) -> OutreachExecution:
    await session.commit()
    try:
        adapter = provider or resolve_outreach_provider(execution.channel)
        message = OutreachMessage(
            channel=execution.channel, recipient=execution.recipient or "",
            subject=execution.subject, content=execution.content_snapshot["text"], provider=execution.provider,
            idempotency_key=execution.idempotency_key,
        )
        result = await adapter.send(message)
    except OutreachProviderFailure as error:
        safe_code, safe_message, status_code = _safe_provider_failure(error, execution.channel)
        execution.status = "FAILED"
        execution.error_code = safe_code
        execution.error_message = safe_message
        execution.failed_at = now_utc()
        execution.updated_at = now_utc()
        await session.commit()
        raise OutreachOperationError(safe_code, safe_message, status_code) from None
    except Exception:
        execution.status = "FAILED"
        execution.error_code = "PROVIDER_FAILURE"
        execution.error_message = "The outreach provider could not complete the send request"
        execution.failed_at = now_utc()
        execution.updated_at = now_utc()
        await session.commit()
        raise OutreachOperationError(execution.error_code, execution.error_message, 502) from None
    sent_at = now_utc()
    execution.status = "SENT"
    execution.sent_at = sent_at
    execution.provider_message_id = result.provider_message_id if result else None
    execution.updated_at = sent_at
    session.add(_activity_for_execution(execution, sent_at))
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise OutreachOperationError("DUPLICATE_OUTREACH_EXECUTION", "This approved outreach has already been executed") from None
    await session.refresh(execution)
    return execution


async def send_approved_outreach(session: AsyncSession, artifact_id: uuid.UUID, body: OutreachSendRequest, *, provider=None) -> OutreachExecution:
    duplicate = await _by_idempotency_key(session, artifact_id, body.idempotency_key)
    if duplicate is not None:
        if duplicate.channel != body.channel:
            raise OutreachOperationError("IDEMPOTENCY_KEY_REUSED", "This idempotency key was already used for another channel")
        if duplicate.status == "FAILED":
            raise OutreachOperationError(duplicate.error_code or "PROVIDER_FAILURE", duplicate.error_message or "The previous send attempt failed", _stored_failure_status(duplicate.error_code))
        return duplicate
    artifact, lead, content = await _approved_current_artifact(session, artifact_id)
    # Recheck after locking the artifact so concurrent requests with the same
    # idempotency key return the first attempt instead of reporting a conflict.
    duplicate = await _by_idempotency_key(session, artifact_id, body.idempotency_key)
    if duplicate is not None:
        if duplicate.channel != body.channel:
            raise OutreachOperationError("IDEMPOTENCY_KEY_REUSED", "This idempotency key was already used for another channel")
        if duplicate.status == "FAILED":
            raise OutreachOperationError(duplicate.error_code or "PROVIDER_FAILURE", duplicate.error_message or "The previous send attempt failed", _stored_failure_status(duplicate.error_code))
        return duplicate
    existing = await session.scalar(select(OutreachExecution).where(
        OutreachExecution.outreach_artifact_id == artifact.id,
        OutreachExecution.status.in_(ACTIVE_EXECUTION_STATES),
    ))
    if existing:
        raise OutreachOperationError("OUTREACH_ALREADY_EXECUTED", "This approved artifact already has an active or completed execution")
    failed = await session.scalar(select(OutreachExecution).where(
        OutreachExecution.outreach_artifact_id == artifact.id,
        OutreachExecution.status == "FAILED",
    ).order_by(OutreachExecution.attempt_number.desc()).limit(1))
    if failed:
        raise OutreachOperationError("RETRY_REQUIRED", "Use the explicit retry action for the failed execution")

    if body.channel == "MANUAL":
        recipient = _recipient(lead, body.manual_method) if body.manual_method else None
        subject, text = _compose(content, body.manual_method) if body.manual_method else (None, "")
        execution = OutreachExecution(
            lead_id=lead.id, outreach_artifact_id=artifact.id, channel="MANUAL",
            manual_method=body.manual_method, provider="manual", status="QUEUED",
            attempt_number=1, idempotency_key=body.idempotency_key, recipient=recipient,
            subject=subject, content_snapshot=_execution_snapshot(content, "MANUAL", subject, text),
        )
        session.add(execution)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            raise OutreachOperationError("OUTREACH_ALREADY_EXECUTED", "This approved artifact already has an active execution") from None
        await session.refresh(execution)
        return execution

    recipient = _recipient(lead, body.channel)
    subject, text = _compose(content, body.channel)
    provider_name = "http_json"
    # Configuration resolution is deliberately server-side. It checks enablement
    # and complete credentials but never returns configuration to the API client.
    if provider is None:
        try:
            adapter = resolve_outreach_provider(body.channel)
            provider_name = adapter.name
        except OutreachProviderFailure as error:
            safe_code, safe_message, status_code = _safe_provider_failure(error, body.channel)
            # Persist a failed attempt so the human can inspect it and retry later.
            execution = OutreachExecution(
                lead_id=lead.id, outreach_artifact_id=artifact.id, channel=body.channel,
                provider="unconfigured", status="FAILED", attempt_number=1,
                idempotency_key=body.idempotency_key, recipient=recipient, subject=subject,
                content_snapshot=_execution_snapshot(content, body.channel, subject, text),
                error_code=safe_code, error_message=safe_message, failed_at=now_utc(),
            )
            session.add(execution)
            await session.commit()
            await session.refresh(execution)
            raise OutreachOperationError(safe_code, safe_message, status_code) from None
    else:
        adapter = provider
        provider_name = getattr(provider, "name", "test_provider")
    execution = OutreachExecution(
        lead_id=lead.id, outreach_artifact_id=artifact.id, channel=body.channel,
        provider=provider_name, status="SENDING", attempt_number=(failed.attempt_number + 1 if failed else 1),
        idempotency_key=body.idempotency_key, recipient=recipient, subject=subject,
        content_snapshot=_execution_snapshot(content, body.channel, subject, text),
    )
    session.add(execution)
    try:
        await session.flush()
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise OutreachOperationError("OUTREACH_ALREADY_EXECUTED", "This approved artifact already has an active execution") from None
    return await _send_attempt(session, execution, provider=adapter)


async def retry_outreach_execution(session: AsyncSession, execution_id: uuid.UUID, body: OutreachRetryRequest, *, provider=None) -> OutreachExecution:
    previous = await session.scalar(select(OutreachExecution).where(OutreachExecution.id == execution_id).with_for_update())
    if previous is None:
        raise OutreachOperationError("EXECUTION_NOT_FOUND", "Outreach execution not found", 404)
    duplicate = await _by_idempotency_key(session, previous.outreach_artifact_id, body.idempotency_key)
    if duplicate is not None:
        if duplicate.retry_of_execution_id != previous.id:
            raise OutreachOperationError("IDEMPOTENCY_KEY_REUSED", "This idempotency key was already used for another execution")
        if duplicate.status == "FAILED":
            raise OutreachOperationError(duplicate.error_code or "PROVIDER_FAILURE", duplicate.error_message or "The retry attempt failed", _stored_failure_status(duplicate.error_code))
        return duplicate
    if previous.status != "FAILED" or previous.channel == "MANUAL":
        raise OutreachOperationError("EXECUTION_NOT_RETRYABLE", "Only failed external sends can be retried")
    artifact, lead, content = await _approved_current_artifact(session, previous.outreach_artifact_id)
    recipient = _recipient(lead, previous.channel)
    subject, text = _compose(content, previous.channel)
    try:
        adapter = provider or resolve_outreach_provider(previous.channel)
    except OutreachProviderFailure as error:
        safe_code, safe_message, status_code = _safe_provider_failure(error, previous.channel)
        raise OutreachOperationError(safe_code, safe_message, status_code) from None
    execution = OutreachExecution(
        lead_id=lead.id, outreach_artifact_id=artifact.id, retry_of_execution_id=previous.id,
        channel=previous.channel, provider=getattr(adapter, "name", previous.provider), status="SENDING",
        attempt_number=previous.attempt_number + 1, idempotency_key=body.idempotency_key,
        recipient=recipient, subject=subject,
        content_snapshot=_execution_snapshot(content, previous.channel, subject, text),
    )
    session.add(execution)
    try:
        await session.flush()
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise OutreachOperationError("OUTREACH_ALREADY_EXECUTED", "This approved artifact already has an active execution") from None
    return await _send_attempt(session, execution, provider=adapter)


async def confirm_manual_send(session: AsyncSession, execution_id: uuid.UUID, body: ManualSendConfirmation) -> OutreachExecution:
    execution = await session.scalar(select(OutreachExecution).where(OutreachExecution.id == execution_id).with_for_update())
    if execution is None:
        raise OutreachOperationError("EXECUTION_NOT_FOUND", "Outreach execution not found", 404)
    if execution.channel != "MANUAL":
        raise OutreachOperationError("NOT_MANUAL_EXECUTION", "This execution was not prepared for manual sending")
    if execution.status == "SENT":
        return execution
    if execution.status != "QUEUED":
        raise OutreachOperationError("MANUAL_EXECUTION_NOT_CONFIRMABLE", "Only queued manual executions can be confirmed")
    artifact, _, _ = await _approved_current_artifact(session, execution.outreach_artifact_id)
    if artifact.lead_id != execution.lead_id:
        raise OutreachOperationError("CROSS_LEAD_EXECUTION", "Execution does not match the artifact lead")
    sent_at = now_utc()
    execution.status = "SENT"
    execution.sent_at = sent_at
    execution.updated_at = sent_at
    session.add(_activity_for_execution(execution, sent_at))
    await session.commit()
    await session.refresh(execution)
    return execution


async def cancel_manual_execution(session: AsyncSession, execution_id: uuid.UUID) -> OutreachExecution:
    execution = await session.scalar(select(OutreachExecution).where(OutreachExecution.id == execution_id).with_for_update())
    if execution is None:
        raise OutreachOperationError("EXECUTION_NOT_FOUND", "Outreach execution not found", 404)
    if execution.channel != "MANUAL":
        raise OutreachOperationError("NOT_MANUAL_EXECUTION", "Only an unsent manual execution can be cancelled")
    if execution.status == "CANCELLED":
        return execution
    if execution.status != "QUEUED":
        raise OutreachOperationError("EXECUTION_NOT_CANCELLABLE", "Only queued manual executions can be cancelled")
    execution.status = "CANCELLED"
    execution.updated_at = now_utc()
    await session.commit()
    await session.refresh(execution)
    return execution


async def record_provider_event(session: AsyncSession, execution_id: uuid.UUID, body: OutreachProviderEventIn) -> OutreachProviderEvent:
    execution = await session.scalar(select(OutreachExecution).where(OutreachExecution.id == execution_id).with_for_update())
    if execution is None:
        raise OutreachOperationError("EXECUTION_NOT_FOUND", "Outreach execution not found", 404)
    if execution.provider != body.provider or not execution.provider_message_id:
        raise OutreachOperationError("PROVIDER_EVENT_MISMATCH", "Provider event does not match a known provider message")
    existing = await session.scalar(select(OutreachProviderEvent).where(
        OutreachProviderEvent.provider == body.provider,
        OutreachProviderEvent.provider_event_id == body.provider_event_id,
    ))
    if existing:
        if existing.execution_id != execution.id or existing.event_type != body.event_type:
            raise OutreachOperationError("PROVIDER_EVENT_CONFLICT", "Provider event identifier was already used for another event")
        return existing
    if execution.status not in {"SENT", "DELIVERED"}:
        raise OutreachOperationError("PROVIDER_EVENT_NOT_APPLICABLE", "Provider events can update only sent executions")
    occurred = body.occurred_at.astimezone(timezone.utc)
    event = OutreachProviderEvent(
        execution_id=execution.id, provider=body.provider,
        provider_event_id=body.provider_event_id, event_type=body.event_type,
        occurred_at=occurred,
    )
    session.add(event)
    if body.event_type == "DELIVERED":
        execution.status = "DELIVERED"
        execution.delivered_at = occurred
        execution.updated_at = now_utc()
    elif body.event_type in {"BOUNCED", "FAILED"}:
        execution.status = "FAILED"
        execution.error_code = "PROVIDER_EVENT_FAILED"
        execution.error_message = "The provider reported delivery failure"
        execution.failed_at = occurred
        execution.updated_at = now_utc()
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise OutreachOperationError("PROVIDER_EVENT_CONFLICT", "Provider event was already recorded") from None
    await session.refresh(event)
    return event
