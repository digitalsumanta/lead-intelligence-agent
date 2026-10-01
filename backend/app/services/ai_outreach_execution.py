"""Deterministic approval and one-attempt execution of Phase 10D email drafts."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.ai_outreach_preparation import (
    CONTEXT_VERSION, PREPARATION_VERSION, OutreachPreparationRequest,
    PreparationChannel, PreparationSourceType, PreparationStatus,
)
from app.domain.ai_sales_action_planner import ActionType
from app.models import (
    AgentPolicyEvaluation, AiOutreachApproval, AiOutreachExecution, AiOutreachPreparation,
    Lead, SalesActivity, SalesActivityType,
)
from app.models.entities import now_utc
from app.providers.outreach import OutreachProviderFailure
from app.providers.email import EmailProviderResult, EmailSendRequest, resolve_email_provider
from app.services.email_delivery_events import add_execution_delivery_event
from app.schemas.agent_policy import PolicyChannel, PolicyDecision, PolicyEvaluationRequest
from app.schemas.ai_outreach_execution import (
    APPROVAL_VERSION, EXECUTION_VERSION, OutreachApprovalOut, OutreachExecutionOut,
)
from app.services.agent_policy import evaluate_policy
from app.services.ai_outreach_preparation import (
    PreparationError, StalePreparationContext, _contact_problem, _load_source,
)
from app.services.ai_sales_intelligence import AISalesContextBuilder, validate_context_references


EMAIL_RE = re.compile(r"^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$")
COMMUNICATION_POLICY_ACTION = ActionType.FOLLOW_UP


class OutreachExecutionError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 409):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class ApprovalSnapshot:
    id: UUID
    preparation_id: UUID
    lead_id: UUID
    status: str
    channel: str
    recipient: str
    context_fingerprint: str
    preparation_version: str
    content_hash: str


def _hash(value) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _preparation_content_hash(preparation: AiOutreachPreparation) -> str:
    return _hash({
        "lead_id": str(preparation.lead_id), "preparation_id": str(preparation.id),
        "source_type": preparation.source_type, "source_id": str(preparation.source_id),
        "source_action_id": preparation.source_action_id, "channel": preparation.channel,
        "subject": preparation.subject, "body": preparation.body,
        "context_version": preparation.context_version,
        "context_fingerprint": preparation.context_fingerprint,
        "preparation_version": preparation.preparation_version,
    })


def _idempotency_key(approval: AiOutreachApproval) -> str:
    return _hash({
        "lead_id": str(approval.lead_id), "preparation_id": str(approval.preparation_id),
        "approval_id": str(approval.id), "context_fingerprint": approval.context_fingerprint,
        "execution_version": EXECUTION_VERSION,
    })


def _approval_output(row: AiOutreachApproval, *, stale: bool = False) -> OutreachApprovalOut:
    return OutreachApprovalOut(
        id=row.id, preparation_id=row.preparation_id, lead_id=row.lead_id,
        status="STALE" if stale else row.status, approved_by_user_id=row.approved_by_user_id,
        channel="EMAIL", recipient=row.recipient, context_fingerprint=row.context_fingerprint,
        preparation_version=row.preparation_version, content_hash=row.content_hash,
        approval_version=row.approval_version, policy_evaluation_id=row.policy_evaluation_id,
        policy_decision=row.policy_decision,
        approved_at=row.approved_at,
    )


def _execution_output(row: AiOutreachExecution) -> OutreachExecutionOut:
    return OutreachExecutionOut(
        id=row.id, approval_id=row.approval_id, preparation_id=row.preparation_id,
        lead_id=row.lead_id, channel="EMAIL", recipient=row.recipient, subject=row.subject,
        content_hash=row.content_hash, context_fingerprint=row.context_fingerprint,
        execution_version=row.execution_version, policy_evaluation_id=row.policy_evaluation_id,
        policy_decision=row.policy_decision,
        provider=row.provider, provider_message_id=row.provider_message_id, status=row.status,
        provider_status=row.provider_status,
        failure_code=row.failure_code, failure_reason=row.failure_reason,
        idempotency_key=row.idempotency_key, started_at=row.started_at,
        completed_at=row.completed_at, created_at=row.created_at,
    )


async def _load_preparation(session: AsyncSession, preparation_id: UUID, *, lock: bool = False):
    query = select(AiOutreachPreparation).where(AiOutreachPreparation.id == preparation_id)
    if lock:
        query = query.with_for_update()
    preparation = await session.scalar(query)
    if preparation is None:
        raise OutreachExecutionError("PREPARATION_NOT_FOUND", "Outreach preparation not found", 404)
    lead = await session.get(Lead, preparation.lead_id)
    if lead is None:
        raise OutreachExecutionError("LEAD_NOT_FOUND", "Lead not found", 404)
    return preparation, lead


async def _validate_current_preparation(session: AsyncSession, preparation, lead):
    preparation_id, lead_id = preparation.id, lead.id
    session.expire_all()
    preparation = await session.get(AiOutreachPreparation, preparation_id)
    lead = await session.get(Lead, lead_id)
    if preparation is None or lead is None:
        raise OutreachExecutionError("PREPARATION_NOT_FOUND", "Outreach preparation or lead not found", 404)
    if preparation.channel != "EMAIL":
        raise OutreachExecutionError("INVALID_CHANNEL", "Only EMAIL preparations can be approved or executed", 422)
    if preparation.status not in {
        PreparationStatus.READY_FOR_HUMAN_REVIEW.value,
        PreparationStatus.REQUIRES_HUMAN_APPROVAL.value,
    } or not preparation.review_required:
        raise OutreachExecutionError("INVALID_STATE", "This preparation is not eligible for human approval", 409)
    if preparation.context_version != CONTEXT_VERSION or preparation.preparation_version != PREPARATION_VERSION:
        raise OutreachExecutionError("STALE_CONTEXT", "This preparation uses an unsupported or stale version", 409)
    if not preparation.subject or not preparation.body:
        raise OutreachExecutionError("INVALID_PREPARATION", "The email subject or body is unavailable", 409)

    context, fingerprint, catalog = await AISalesContextBuilder().build(session, lead_id)
    if fingerprint != preparation.context_fingerprint:
        raise OutreachExecutionError("STALE_CONTEXT", "Lead context changed after this preparation was created", 409)
    request = OutreachPreparationRequest(
        lead_id=lead.id, source_type=PreparationSourceType(preparation.source_type),
        source_id=preparation.source_id, source_action_id=preparation.source_action_id,
        action_type=ActionType(preparation.action_type), channel=PreparationChannel.EMAIL,
    )
    try:
        _, selection, source_refs = await _load_source(session, request, context, fingerprint, catalog)
        allowed_evidence = set(getattr(selection, "evidence_ids", ()))
        allowed_opportunities = set(getattr(selection, "opportunity_ids", ()))
        allowed_services = set(getattr(selection, "service_ids", ()))
        evidence_refs = set(preparation.evidence_refs or ())
        opportunity_refs = set(preparation.opportunity_refs or ())
        service_refs = set(preparation.service_refs or ())
        if (not evidence_refs or not evidence_refs.issubset(allowed_evidence)
                or not opportunity_refs.issubset(allowed_opportunities)
                or not service_refs.issubset(allowed_services)
                or not (evidence_refs | opportunity_refs).issubset(source_refs)):
            raise PreparationError("Stored preparation references no longer match its source artifact")
        validate_context_references(evidence_refs | opportunity_refs, context, catalog, lead.id)
        if not service_refs.issubset(set(context.active_service_ids)):
            raise PreparationError("Stored service references are no longer active")
    except StalePreparationContext:
        raise OutreachExecutionError("STALE_CONTEXT", "The source recommendation is no longer current", 409) from None
    except (PreparationError, ValueError, TypeError):
        raise OutreachExecutionError("INVALID_PROVENANCE", "The source artifact or evidence references are invalid", 409) from None

    contact_error = _contact_problem(lead, request)
    if contact_error:
        raise OutreachExecutionError("COMPLIANCE_BLOCKED", contact_error, 409)
    recipient = (lead.email or "").strip()
    if not EMAIL_RE.fullmatch(recipient):
        raise OutreachExecutionError("INVALID_RECIPIENT", "The lead does not have a valid email destination", 422)
    return context, fingerprint, recipient, evidence_refs, opportunity_refs, service_refs


async def _evaluate_execution_policy(session, lead_id, fingerprint, evidence_refs, opportunity_refs, service_refs):
    # Every actual email send is an external follow-up for policy purposes, even
    # when the original preparation came from a proposal/SEO review action.
    return await evaluate_policy(session, lead_id, PolicyEvaluationRequest(
        action_type=COMMUNICATION_POLICY_ACTION, requested_channel=PolicyChannel.EMAIL,
        evidence_ids=tuple(sorted(evidence_refs)), opportunity_ids=tuple(sorted(opportunity_refs)),
        service_ids=tuple(sorted(service_refs)), source_fingerprint=fingerprint,
    ))


async def approve_preparation(
    session: AsyncSession, preparation_id: UUID, *, approved_by_user_id: str,
) -> OutreachApprovalOut:
    if not approved_by_user_id or len(approved_by_user_id) > 120:
        raise OutreachExecutionError("AUTHENTICATED_USER_REQUIRED", "An authenticated user identity is required", 401)
    preparation, lead = await _load_preparation(session, preparation_id)
    existing = await session.scalar(select(AiOutreachApproval).where(
        AiOutreachApproval.preparation_id == preparation.id,
        AiOutreachApproval.status == "APPROVED",
    ))
    if existing is not None:
        stale = await _approval_is_stale(session, existing)
        await session.refresh(existing)
        return _approval_output(existing, stale=stale)

    _, fingerprint, recipient, evidence_refs, opportunity_refs, service_refs = await _validate_current_preparation(session, preparation, lead)
    policy = await _evaluate_execution_policy(session, lead.id, fingerprint, evidence_refs, opportunity_refs, service_refs)
    if policy.decision == PolicyDecision.DENY:
        raise OutreachExecutionError("POLICY_DENIED", policy.reason, 409)
    # Policy evaluation commits its audit record, so reload and recheck the
    # immutable preparation before writing the approval.
    preparation, lead = await _load_preparation(session, preparation_id)
    _, current_fingerprint, current_recipient, _, _, _ = await _validate_current_preparation(session, preparation, lead)
    if current_fingerprint != fingerprint or current_recipient != recipient:
        raise OutreachExecutionError("STALE_CONTEXT", "Lead context or recipient changed during approval", 409)
    row = AiOutreachApproval(
        preparation_id=preparation.id, lead_id=lead.id, approved_by_user_id=approved_by_user_id,
        channel="EMAIL", recipient=recipient, context_fingerprint=fingerprint,
        preparation_version=preparation.preparation_version,
        content_hash=_preparation_content_hash(preparation), approval_version=APPROVAL_VERSION,
        policy_evaluation_id=policy.evaluation_id, policy_decision=policy.decision.value,
        status="APPROVED", approved_at=now_utc(), created_at=now_utc(),
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await session.scalar(select(AiOutreachApproval).where(
            AiOutreachApproval.preparation_id == preparation_id,
            AiOutreachApproval.status == "APPROVED",
        ))
        if existing is None:
            raise
        return _approval_output(existing)
    await session.refresh(row)
    return _approval_output(row)


async def approval_by_preparation(session: AsyncSession, preparation_id: UUID):
    row = await session.scalar(select(AiOutreachApproval).where(
        AiOutreachApproval.preparation_id == preparation_id,
        AiOutreachApproval.status == "APPROVED",
    ))
    if row is None:
        return None
    stale = await _approval_is_stale(session, row)
    await session.refresh(row)
    return _approval_output(row, stale=stale)


async def _approval_is_stale(session: AsyncSession, row: AiOutreachApproval) -> bool:
    preparation_id = row.preparation_id
    approved_context = row.context_fingerprint
    approved_recipient = row.recipient
    approved_version = row.preparation_version
    approved_content_hash = row.content_hash
    try:
        preparation, lead = await _load_preparation(session, preparation_id)
        _, fingerprint, recipient, _, _, _ = await _validate_current_preparation(session, preparation, lead)
        return (
            fingerprint != approved_context
            or recipient != approved_recipient
            or preparation.preparation_version != approved_version
            or _preparation_content_hash(preparation) != approved_content_hash
        )
    except OutreachExecutionError:
        return True


async def _save_terminal_execution(session, *, approval, preparation, policy_id, status, code, reason, provider_name="unavailable"):
    now = now_utc()
    policy_record = await session.get(AgentPolicyEvaluation, policy_id)
    row = AiOutreachExecution(
        approval_id=approval.id, preparation_id=preparation.id, lead_id=approval.lead_id,
        channel="EMAIL", recipient=approval.recipient, subject=preparation.subject or "",
        content_hash=_preparation_content_hash(preparation), context_fingerprint=approval.context_fingerprint,
        execution_version=EXECUTION_VERSION, policy_evaluation_id=policy_id,
        policy_decision=policy_record.decision if policy_record else PolicyDecision.DENY.value,
        provider=provider_name, status=status, failure_code=code, failure_reason=reason[:500],
        idempotency_key=_idempotency_key(approval), started_at=now, completed_at=now, created_at=now,
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await session.scalar(select(AiOutreachExecution).where(AiOutreachExecution.approval_id == approval.id))
        if existing is None:
            raise
        return _execution_output(existing)
    await session.refresh(row)
    return _execution_output(row)


async def execute_approved_email(session: AsyncSession, approval_id: UUID, *, provider=None) -> OutreachExecutionOut:
    existing = await session.scalar(select(AiOutreachExecution).where(AiOutreachExecution.approval_id == approval_id))
    if existing is not None:
        return _execution_output(existing)
    approval = await session.scalar(select(AiOutreachApproval).where(AiOutreachApproval.id == approval_id).with_for_update())
    if approval is None:
        raise OutreachExecutionError("INVALID_APPROVAL", "Outreach approval not found", 404)
    if approval.status != "APPROVED" or approval.channel != "EMAIL":
        raise OutreachExecutionError("INVALID_APPROVAL", "An approved EMAIL approval is required", 409)
    approval = ApprovalSnapshot(
        id=approval.id, preparation_id=approval.preparation_id, lead_id=approval.lead_id,
        status=approval.status, channel=approval.channel, recipient=approval.recipient,
        context_fingerprint=approval.context_fingerprint,
        preparation_version=approval.preparation_version, content_hash=approval.content_hash,
    )
    preparation, lead = await _load_preparation(session, approval.preparation_id, lock=True)
    if preparation.lead_id != approval.lead_id:
        raise OutreachExecutionError("INVALID_APPROVAL", "Approval and preparation ownership do not match", 409)

    current = None
    try:
        current = await _validate_current_preparation(session, preparation, lead)
    except OutreachExecutionError as error:
        policy = await _evaluate_execution_policy(
            session, lead.id, approval.context_fingerprint,
            set(preparation.evidence_refs or ()), set(preparation.opportunity_refs or ()),
            set(preparation.service_refs or ()),
        )
        return await _save_terminal_execution(
            session, approval=approval, preparation=preparation, policy_id=policy.evaluation_id,
            status="STALE" if error.code == "STALE_CONTEXT" else "BLOCKED",
            code=error.code, reason=str(error),
        )
    _, fingerprint, recipient, evidence_refs, opportunity_refs, service_refs = current
    if fingerprint != approval.context_fingerprint or recipient != approval.recipient:
        policy = await _evaluate_execution_policy(session, lead.id, fingerprint, evidence_refs, opportunity_refs, service_refs)
        return await _save_terminal_execution(
            session, approval=approval, preparation=preparation, policy_id=policy.evaluation_id,
            status="STALE", code="STALE_CONTEXT", reason="Context or recipient changed after approval.",
        )
    if _preparation_content_hash(preparation) != approval.content_hash or preparation.preparation_version != approval.preparation_version:
        policy = await _evaluate_execution_policy(session, lead.id, fingerprint, evidence_refs, opportunity_refs, service_refs)
        return await _save_terminal_execution(
            session, approval=approval, preparation=preparation, policy_id=policy.evaluation_id,
            status="BLOCKED", code="APPROVED_CONTENT_CHANGED", reason="The approved preparation content changed.",
        )
    policy = await _evaluate_execution_policy(session, lead.id, fingerprint, evidence_refs, opportunity_refs, service_refs)
    if policy.decision == PolicyDecision.DENY:
        return await _save_terminal_execution(
            session, approval=approval, preparation=preparation, policy_id=policy.evaluation_id,
            status="BLOCKED", code="POLICY_DENIED", reason=policy.reason,
        )
    # evaluate_policy commits. Re-read and revalidate context and recipient at
    # the send boundary before reserving the single execution slot.
    preparation, lead = await _load_preparation(session, approval.preparation_id, lock=True)
    _, final_fingerprint, final_recipient, final_evidence, final_opportunities, final_services = await _validate_current_preparation(session, preparation, lead)
    if (final_fingerprint != approval.context_fingerprint or final_recipient != approval.recipient
            or _preparation_content_hash(preparation) != approval.content_hash):
        return await _save_terminal_execution(
            session, approval=approval, preparation=preparation, policy_id=policy.evaluation_id,
            status="STALE", code="STALE_CONTEXT", reason="Context, recipient, or approved content changed before execution.",
        )
    # The execution-time policy must still refer to the exact fingerprint and
    # evidence validated immediately above.
    if policy.source_fingerprint != final_fingerprint:
        return await _save_terminal_execution(
            session, approval=approval, preparation=preparation, policy_id=policy.evaluation_id,
            status="STALE", code="STALE_CONTEXT", reason="Context changed during execution authorization.",
        )
    duplicate = await session.scalar(select(AiOutreachExecution).where(AiOutreachExecution.approval_id == approval.id))
    if duplicate is not None:
        return _execution_output(duplicate)
    try:
        adapter = provider or resolve_email_provider()
        provider_name = str(getattr(adapter, "name", "unknown"))[:80]
    except (OutreachProviderFailure, ValueError) as error:
        return await _save_terminal_execution(
            session, approval=approval, preparation=preparation, policy_id=policy.evaluation_id,
            status="FAILED", code=getattr(error, "code", "PROVIDER_NOT_CONFIGURED"), reason=str(error),
        )
    now = now_utc()
    execution = AiOutreachExecution(
        approval_id=approval.id, preparation_id=preparation.id, lead_id=lead.id,
        channel="EMAIL", recipient=approval.recipient, subject=preparation.subject or "",
        content_hash=approval.content_hash, context_fingerprint=approval.context_fingerprint,
        execution_version=EXECUTION_VERSION, policy_evaluation_id=policy.evaluation_id,
        policy_decision=policy.decision.value,
        provider=provider_name, status="EXECUTING", idempotency_key=_idempotency_key(approval),
        started_at=now, created_at=now,
    )
    session.add(execution)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await session.scalar(select(AiOutreachExecution).where(AiOutreachExecution.approval_id == approval.id))
        if existing is None:
            raise
        return _execution_output(existing)
    await session.refresh(execution)

    message = EmailSendRequest(
        recipient=approval.recipient, subject=preparation.subject or "",
        body=preparation.body or "", idempotency_key=execution.idempotency_key,
        metadata={"execution_id": str(execution.id), "approval_id": str(approval.id)},
    )
    try:
        result = await adapter.send(message)
    except OutreachProviderFailure as error:
        execution.status = "UNCERTAIN" if error.code in {"PROVIDER_TIMEOUT", "PROVIDER_UNAVAILABLE"} else "FAILED"
        execution.failure_code = error.code[:80]
        execution.failure_reason = str(error)[:500]
    except Exception:
        execution.status = "UNCERTAIN"
        execution.failure_code = "PROVIDER_OUTCOME_UNKNOWN"
        execution.failure_reason = "The provider outcome is unknown; automatic retry is disabled."
    else:
        outcome = result.outcome if isinstance(result, EmailProviderResult) else None
        if outcome not in {"SENT", "FAILED", "UNCERTAIN"}:
            result = EmailProviderResult(
                outcome="UNCERTAIN", provider=provider_name,
                error_code="PROVIDER_INVALID_RESULT",
                error_message="The email provider returned an invalid result",
            )
        outcome = result.outcome
        execution.status = outcome
        execution.provider_message_id = str(result.provider_message_id)[:512] if result.provider_message_id else None
        provider_status = getattr(result, "provider_status", None)
        execution.provider_status = str(provider_status)[:80] if provider_status else None
        error_code = getattr(result, "error_code", None)
        execution.failure_code = str(error_code)[:80] if error_code else None
        failure_reason = getattr(result, "error_message", None)
        execution.failure_reason = str(failure_reason)[:500] if failure_reason else None
        if outcome == "SENT":
            execution.failure_code = None
            execution.failure_reason = None
        elif execution.failure_code is None:
            execution.failure_code = "PROVIDER_OUTCOME_UNKNOWN" if outcome == "UNCERTAIN" else "PROVIDER_FAILURE"
            execution.failure_reason = "The email provider did not return a safe error description."
    execution.completed_at = now_utc()
    if execution.status == "SENT":
        session.add(SalesActivity(
            lead_id=execution.lead_id, activity_type=SalesActivityType.EMAIL,
            note=f"Human-approved Phase 10E email sent. Execution {execution.id}.",
            occurred_at=execution.completed_at,
        ))
        add_execution_delivery_event(session, execution, "SENT")
    elif execution.status == "FAILED":
        add_execution_delivery_event(session, execution, "FAILED")
    await session.commit()
    await session.refresh(execution)
    return _execution_output(execution)


async def execution_by_id(session: AsyncSession, execution_id: UUID):
    row = await session.get(AiOutreachExecution, execution_id)
    return _execution_output(row) if row else None


async def execution_history_for_preparation(session: AsyncSession, preparation_id: UUID):
    if await session.get(AiOutreachPreparation, preparation_id) is None:
        raise OutreachExecutionError("PREPARATION_NOT_FOUND", "Outreach preparation not found", 404)
    rows = (await session.scalars(select(AiOutreachExecution).where(
        AiOutreachExecution.preparation_id == preparation_id,
    ).order_by(AiOutreachExecution.created_at.desc(), AiOutreachExecution.id.desc()))).all()
    return [_execution_output(row) for row in rows]


async def execution_history_for_lead(session: AsyncSession, lead_id: UUID, limit: int = 50):
    if await session.get(Lead, lead_id) is None:
        raise OutreachExecutionError("LEAD_NOT_FOUND", "Lead not found", 404)
    rows = (await session.scalars(select(AiOutreachExecution).where(
        AiOutreachExecution.lead_id == lead_id,
    ).order_by(AiOutreachExecution.created_at.desc(), AiOutreachExecution.id.desc()).limit(limit))).all()
    return [_execution_output(row) for row in rows]
