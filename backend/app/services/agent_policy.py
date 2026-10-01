"""Provider-independent deterministic policy evaluation and audit persistence."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.domain.ai_sales_intelligence import AIValidationError
from app.models import (
    AgentPolicyEvaluation, AiOutreachExecution, Lead, LeadStatus, OutreachExecution, SalesActivity, SalesActivityType,
    SalesTask, SalesTaskStatus, SalesTaskType,
)
from app.models.entities import now_utc
from app.schemas.agent_policy import (
    AutonomyLevel, PolicyAction, PolicyDecision, PolicyEvaluationOut,
    PolicyEvaluationRequest, PolicyRuleResult, PolicyChannel, POLICY_VERSION,
)
from app.services.ai_sales_intelligence import AISalesContextBuilder, validate_context_references


COMMUNICATION_ACTIONS = {PolicyAction.FOLLOW_UP, PolicyAction.CALL_PROSPECT}
COMMUNICATION_CHANNELS = {"EMAIL", "WHATSAPP", "PHONE"}
TASK_BY_ACTION = {PolicyAction.FOLLOW_UP: SalesTaskType.FOLLOW_UP, PolicyAction.CALL_PROSPECT: SalesTaskType.CALL}
AUTONOMY_BY_ACTION = {
    PolicyAction.FOLLOW_UP: AutonomyLevel.HUMAN_ONLY,
    PolicyAction.CALL_PROSPECT: AutonomyLevel.HUMAN_ONLY,
    PolicyAction.REVIEW_SEO_OPPORTUNITY: AutonomyLevel.ASSISTED,
    PolicyAction.REVIEW_OUTREACH: AutonomyLevel.ASSISTED,
    PolicyAction.PREPARE_PROPOSAL: AutonomyLevel.ASSISTED,
    PolicyAction.INVESTIGATE_MISSING_INFORMATION: AutonomyLevel.ASSISTED,
    PolicyAction.REVIEW_COMMERCIAL_CONTEXT: AutonomyLevel.ASSISTED,
}
RECENT_CONTACT_TYPES = {
    SalesActivityType.CALL, SalesActivityType.EMAIL, SalesActivityType.WHATSAPP,
    SalesActivityType.FOLLOW_UP,
}


def _request_fingerprint(body: PolicyEvaluationRequest, current_fingerprint: str) -> str:
    payload = {"policy_version": POLICY_VERSION, "request": body.model_dump(mode="json"), "current_fingerprint": current_fingerprint}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _rule(rule: str, passed: bool, result: str, reason: str) -> PolicyRuleResult:
    return PolicyRuleResult(rule=rule, passed=passed, result=result, reason=reason)


async def _latest_contact(session: AsyncSession, lead_id: UUID) -> datetime | None:
    activity_time = await session.scalar(select(SalesActivity.occurred_at).where(
        SalesActivity.lead_id == lead_id,
        SalesActivity.activity_type.in_(tuple(RECENT_CONTACT_TYPES)),
    ).order_by(SalesActivity.occurred_at.desc()).limit(1))
    sent_time = await session.scalar(select(OutreachExecution.sent_at).where(
        OutreachExecution.lead_id == lead_id,
        OutreachExecution.sent_at.is_not(None),
    ).order_by(OutreachExecution.sent_at.desc()).limit(1))
    phase_10e_sent_time = await session.scalar(select(AiOutreachExecution.completed_at).where(
        AiOutreachExecution.lead_id == lead_id,
        AiOutreachExecution.status == "SENT",
        AiOutreachExecution.completed_at.is_not(None),
    ).order_by(AiOutreachExecution.completed_at.desc()).limit(1))
    values = [value for value in (activity_time, sent_time, phase_10e_sent_time) if value is not None]
    normalized = [value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value for value in values]
    return max(normalized) if normalized else None


async def evaluate_policy(
    session: AsyncSession,
    lead_id: UUID,
    body: PolicyEvaluationRequest,
    *,
    evaluated_at: datetime | None = None,
) -> PolicyEvaluationOut:
    lead = await session.get(Lead, lead_id)
    if lead is None:
        raise LookupError("Lead not found")

    evaluated_at = evaluated_at or now_utc()
    if evaluated_at.tzinfo is None:
        evaluated_at = evaluated_at.replace(tzinfo=timezone.utc)
    context, current_fingerprint, catalog = await AISalesContextBuilder().build(session, lead_id)
    request_fingerprint = _request_fingerprint(body, current_fingerprint)
    lead_status = getattr(lead.status, "value", lead.status)
    valid_lead = lead_status != LeadStatus.FAILED.value
    rules: list[PolicyRuleResult] = [
        _rule("lead_exists", True, "PASS", "Lead exists."),
        _rule("lead_valid", valid_lead, "PASS" if valid_lead else "DENY", "Lead is eligible for policy review." if valid_lead else "A failed lead record cannot be used for policy authorization."),
    ]
    denied: str | None = None if valid_lead else "INVALID_LEAD_STATE"
    approval_reason: str | None = None

    if body.source_fingerprint != current_fingerprint:
        rules.append(_rule("context_fingerprint", False, "DENY", "The supplied context is stale; refresh the lead context and evaluate again."))
        denied = "STALE_CONTEXT"
    else:
        rules.append(_rule("context_fingerprint", True, "PASS", "The supplied fingerprint matches the current deterministic context."))

    refs = set(body.evidence_ids) | set(body.opportunity_ids)
    try:
        validate_context_references(refs, context, catalog, lead_id)
        allowed_opportunities = {
            item.id: item for item in context.deterministic_opportunities
            if item.source_type in {"lead_intelligence_opportunity", "deterministic_service_recommendation", "seo_opportunity"}
        }
        if not set(body.opportunity_ids).issubset(allowed_opportunities):
            raise AIValidationError("unsupported opportunity")
        rules.append(_rule("context_references", True, "PASS", "Evidence and opportunities belong to this lead and current context."))
    except AIValidationError:
        rules.append(_rule("context_references", False, "DENY", "One or more evidence or opportunity references are invalid for the current lead context."))
        denied = denied or "INVALID_CONTEXT_REFERENCE"

    active_services = set(context.active_service_ids)
    if not set(body.service_ids).issubset(active_services):
        rules.append(_rule("active_services", False, "DENY", "One or more selected services are inactive or unavailable."))
        denied = denied or "INVALID_SERVICE_REFERENCE"
    else:
        supported_services: set[str] = set()
        for ref in refs:
            source = catalog.get(ref)
            if source is None:
                continue
            data = source.get("data", {})
            if source.get("source_type") == "deterministic_service_recommendation":
                supported_services.add(data.get("service_id"))
            elif source.get("source_type") == "seo_opportunity":
                supported_services.update(data.get("recommended_service_ids", []))
        if not set(body.service_ids).issubset(supported_services):
            rules.append(_rule("supported_services", False, "DENY", "One or more selected services lack current deterministic support."))
            denied = denied or "UNSUPPORTED_SERVICE_REFERENCE"
        else:
            rules.append(_rule("active_services", True, "PASS", "Selected services are active."))
            rules.append(_rule("supported_services", True, "PASS", "Selected services have current deterministic support."))

    source_types = {catalog[ref]["source_type"] for ref in refs if ref in catalog}
    action_source_ok = True
    if body.action_type == PolicyAction.REVIEW_SEO_OPPORTUNITY:
        action_source_ok = bool(set(body.opportunity_ids)) and any(catalog[ref]["source_type"] == "seo_opportunity" for ref in body.opportunity_ids if ref in catalog)
    elif body.action_type == PolicyAction.REVIEW_OUTREACH:
        action_source_ok = bool(source_types & {"outreach_artifact", "outreach_execution", "outreach_provider_event", "sales_activity"})
    elif body.action_type == PolicyAction.INVESTIGATE_MISSING_INFORMATION:
        action_source_ok = any(catalog[ref]["source_type"] == "missing_data" for ref in body.evidence_ids if ref in catalog)
    elif body.action_type == PolicyAction.REVIEW_COMMERCIAL_CONTEXT:
        action_source_ok = bool(source_types & {"commercial_context", "revenue_collection"})
    elif body.action_type == PolicyAction.PREPARE_PROPOSAL:
        action_source_ok = bool(refs)
    rules.append(_rule("action_context", action_source_ok, "PASS" if action_source_ok else "DENY", "The action has the required current context." if action_source_ok else "The action lacks its required current deterministic context."))
    if not action_source_ok:
        denied = denied or "REQUIRED_CONTEXT_MISSING"

    if body.action_type in COMMUNICATION_ACTIONS and body.requested_channel:
        opted_out = (body.requested_channel.value == "EMAIL" and lead.email_opted_out) or (
            body.requested_channel.value == "WHATSAPP" and lead.whatsapp_opted_out
        ) or (
            body.requested_channel.value == "PHONE" and lead.phone_opted_out
        )
        rules.append(_rule("channel_opt_out", not opted_out, "DENY" if opted_out else "PASS", "The lead opted out of this channel." if opted_out else "No opt-out is recorded for this channel."))
        if opted_out:
            denied = denied or "CHANNEL_OPT_OUT"

        sales_state = getattr(lead.sales_status, "value", lead.sales_status)
        allowed_sales_state = sales_state not in {"WON", "LOST"}
        rules.append(_rule("sales_state", allowed_sales_state, "PASS" if allowed_sales_state else "DENY", "The sales state permits review of a communication action." if allowed_sales_state else "Won or lost leads cannot receive a new communication action."))
        if not allowed_sales_state:
            denied = denied or "SALES_STATE_BLOCKS_COMMUNICATION"

        task_type = TASK_BY_ACTION[body.action_type]
        duplicate_task = await session.scalar(select(SalesTask.id).where(
            SalesTask.lead_id == lead_id, SalesTask.task_type == task_type,
            SalesTask.status.in_((SalesTaskStatus.OPEN, SalesTaskStatus.IN_PROGRESS)),
        ).limit(1))
        rules.append(_rule("duplicate_active_task", duplicate_task is None, "PASS" if duplicate_task is None else "DENY", "No equivalent active task exists." if duplicate_task is None else "An equivalent open or in-progress task already exists."))
        if duplicate_task is not None:
            denied = denied or "DUPLICATE_ACTIVE_TASK"

        latest_contact = await _latest_contact(session, lead_id)
        cooldown = timedelta(minutes=max(0, settings.agent_contact_cooldown_minutes))
        in_cooldown = latest_contact is not None and latest_contact > evaluated_at - cooldown
        rules.append(_rule("recent_contact_cooldown", not in_cooldown, "PASS" if not in_cooldown else "DENY", "No qualifying recent contact is inside the cooldown." if not in_cooldown else "A qualifying contact occurred inside the configured cooldown."))
        if in_cooldown:
            denied = denied or "RECENT_CONTACT_COOLDOWN"

        rules.append(_rule("human_approval_boundary", False, "REQUIRE_HUMAN_APPROVAL", "Consequential external communication always requires explicit human approval."))
        approval_reason = "EXTERNAL_COMMUNICATION_REQUIRES_HUMAN_APPROVAL"
    else:
        rules.append(_rule("human_approval_boundary", True, "PASS", "This action does not itself perform an external communication."))

    if denied:
        decision, reason = PolicyDecision.DENY, denied
    elif approval_reason:
        decision, reason = PolicyDecision.REQUIRE_HUMAN_APPROVAL, approval_reason
    else:
        decision, reason = PolicyDecision.ALLOW, "DETERMINISTIC_REVIEW_ACTION_ALLOWED"

    record = AgentPolicyEvaluation(
        lead_id=lead_id, action_type=body.action_type.value,
        requested_channel=body.requested_channel.value if body.requested_channel else None,
        decision=decision.value, reason=reason, evidence_refs=sorted(set(body.evidence_ids)),
        opportunity_refs=sorted(set(body.opportunity_ids)), service_refs=sorted(set(body.service_ids)),
        policy_version=POLICY_VERSION, autonomy_level=AUTONOMY_BY_ACTION[body.action_type].value,
        source_fingerprint=body.source_fingerprint, request_fingerprint=request_fingerprint,
        requested_by="authenticated_api", rule_results=[item.model_dump(mode="json") for item in rules],
        created_at=evaluated_at,
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return PolicyEvaluationOut(
        evaluation_id=record.id, lead_id=record.lead_id, action_type=PolicyAction(record.action_type),
        requested_channel=body.requested_channel, decision=PolicyDecision(record.decision), reason=record.reason,
        evidence_ids=tuple(record.evidence_refs), opportunity_ids=tuple(record.opportunity_refs),
        service_ids=tuple(record.service_refs), policy_version=POLICY_VERSION,
        autonomy_level=AutonomyLevel(record.autonomy_level), source_fingerprint=record.source_fingerprint,
        request_fingerprint=record.request_fingerprint, requested_by=record.requested_by,
        rule_results=tuple(PolicyRuleResult.model_validate(item) for item in record.rule_results),
        evaluated_at=record.created_at,
    )


async def policy_history(session: AsyncSession, lead_id: UUID, limit: int = 50) -> list[AgentPolicyEvaluationOut]:
    if await session.get(Lead, lead_id) is None:
        raise LookupError("Lead not found")
    rows = (await session.scalars(select(AgentPolicyEvaluation).where(
        AgentPolicyEvaluation.lead_id == lead_id,
    ).order_by(AgentPolicyEvaluation.created_at.desc(), AgentPolicyEvaluation.id.desc()).limit(limit))).all()
    return [PolicyEvaluationOut(
        evaluation_id=row.id, lead_id=row.lead_id, action_type=PolicyAction(row.action_type),
        requested_channel=PolicyChannel(row.requested_channel) if row.requested_channel else None,
        decision=PolicyDecision(row.decision), reason=row.reason,
        evidence_ids=tuple(row.evidence_refs), opportunity_ids=tuple(row.opportunity_refs),
        service_ids=tuple(row.service_refs), policy_version=POLICY_VERSION,
        autonomy_level=AutonomyLevel(row.autonomy_level), source_fingerprint=row.source_fingerprint,
        request_fingerprint=row.request_fingerprint, requested_by=row.requested_by,
        rule_results=tuple(PolicyRuleResult.model_validate(item) for item in row.rule_results),
        evaluated_at=row.created_at,
    ) for row in rows]
