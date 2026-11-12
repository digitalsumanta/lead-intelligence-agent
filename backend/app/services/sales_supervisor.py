"""
Deterministic Sales Action Prioritization Engine for Controlled Autonomous Sales Supervisor.

Discovers eligible sales opportunities across the pipeline, evaluates candidate
contact recommendations, maps urgency from buying signals and follow-up schedules,
integrates historical sales feedback positive rates, and computes deterministic
prioritization scores without executing outreach or mutating tasks.
"""

from __future__ import annotations

import logging
import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.config import settings
from app.domain.contact_recommendation import RecommendedSalesAction
from app.domain.sales_supervisor import (
    SupervisorActionReadinessStatus,
    SupervisorActionTier,
    SupervisorApprovalStatus,
    SupervisorExecutionDecision,
    SupervisorUrgencyBand,
    calculate_supervisor_action_priority,
    classify_supervisor_action,
    is_policy_approval_required,
    sort_prioritized_actions,
)
from app.models.entities import (
    AiOutreachExecution,
    BuyingSignal,
    ConfidenceLevel,
    Lead,
    LeadIntelligenceOpportunity,
    OutreachExecution,
    SalesActivity,
    SalesActivityType,
    SalesTask,
    SalesTaskStatus,
    SignalStrength,
    now_utc,
)
from app.schemas.sales_supervisor import (
    ActionReadinessDetails,
    PrioritizedSalesAction,
    SalesSupervisorPipelineResponse,
    SupervisorActionScoreBreakdownOut,
    SupervisorExecutionAuthorizationResult,
    SupervisorHumanApproval,
)
from app.services.contact_recommendation import get_contact_recommendation_for_opportunity
from app.domain.sales_feedback import FeedbackSentiment
from app.services.sales_feedback import get_opportunity_sales_feedback

logger = logging.getLogger("lead_agent.sales_supervisor")

HIGH_URGENCY_SIGNAL_TYPES = frozenset({
    "HIRING_EXPANSION",
    "FUNDING_ANNOUNCEMENT",
    "TECH_STACK_CHANGE",
    "LEADERSHIP_CHANGE",
    "EXPANSION_SIGNAL",
})


def validate_source_score(name: str, value: Any) -> float:
    """
    Validate that an input score is numeric, finite, non-boolean, and within [0.0, 100.0].
    Does not silently clamp malformed or out-of-range values.
    """
    if value is None:
        raise ValueError(f"{name} cannot be None")
    if isinstance(value, bool):
        raise TypeError(f"{name} cannot be a boolean")
    if not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be numeric, got {type(value).__name__}")
    fval = float(value)
    if not math.isfinite(fval):
        raise ValueError(f"{name} must be finite, got {fval}")
    if fval < 0.0 or fval > 100.0:
        raise ValueError(f"{name} out of range [0.0, 100.0], got {fval}")
    return fval


def build_action_identity(contact_id: uuid.UUID, recommended_action: RecommendedSalesAction) -> str:
    """
    Build standard deterministic action identity matching existing execution bridge conventions.
    """
    return f"contact_recommendation:{contact_id}:{recommended_action.value}"


def map_buying_signals_to_urgency(
    lead: Optional[Lead],
    signals: Sequence[BuyingSignal],
    opportunity: Optional[LeadIntelligenceOpportunity] = None,
    now: Optional[datetime] = None,
) -> tuple[float, SupervisorUrgencyBand, list[str]]:
    """
    Deterministically computes an action's urgency score on a 0-100 scale:
    - 100.0 (OVERDUE): Follow-up date has passed.
    - 80.0 (DUE_TODAY): Follow-up is scheduled within 24 hours.
    - 60.0 (HIGH_SIGNAL): Fresh high-priority buying signal detected (e.g. hiring, funding).
    - 40.0 (STANDARD): Active buying signals present.
    - 20.0 (STANDARD): Standard outreach cadence when no urgent temporal signal exists.
    - 0.0 (NONE): Explicitly informational only.

    Safety: Missing buying signal data is NEVER represented as maximum urgency.
    """
    if now is None:
        now = now_utc()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    # 1. Temporal follow-up schedule takes highest precedence
    if lead and lead.next_follow_up_at:
        follow_up = lead.next_follow_up_at
        if follow_up.tzinfo is None:
            follow_up = follow_up.replace(tzinfo=timezone.utc)
        else:
            follow_up = follow_up.astimezone(timezone.utc)

        if follow_up < now:
            return (
                100.0,
                SupervisorUrgencyBand.OVERDUE,
                [f"Follow-up was due at {follow_up.isoformat()} (OVERDUE)"],
            )
        elif (follow_up - now).total_seconds() <= 86400:
            return (
                80.0,
                SupervisorUrgencyBand.DUE_TODAY,
                [f"Follow-up is due today at {follow_up.isoformat()} (DUE TODAY)"],
            )

    # 2. Check active buying signals
    if signals:
        high_urgency_signals = [
            s for s in signals
            if (
                s.signal_type in HIGH_URGENCY_SIGNAL_TYPES
                or s.confidence == ConfidenceLevel.HIGH
                or s.strength == SignalStrength.HIGH
            )
        ]
        if high_urgency_signals:
            sig_types = sorted({s.signal_type for s in high_urgency_signals})
            return (
                60.0,
                SupervisorUrgencyBand.HIGH_SIGNAL,
                [f"High-priority buying signals detected: {', '.join(sig_types)}"],
            )
        else:
            return (
                40.0,
                SupervisorUrgencyBand.STANDARD,
                [f"Active buying signals present ({len(signals)} signals detected)"],
            )

    # 3. Standard fallback when no urgent signals are present (never max urgency)
    if opportunity and opportunity.priority == "HIGH":
        return (
            20.0,
            SupervisorUrgencyBand.STANDARD,
            ["Standard outreach cadence for high-priority sales opportunity"],
        )

    return (
        20.0,
        SupervisorUrgencyBand.STANDARD,
        ["Standard outreach cadence (no urgent buying signals detected)"],
    )


RECENT_CONTACT_ACTIVITY_TYPES = (
    SalesActivityType.CALL,
    SalesActivityType.EMAIL,
    SalesActivityType.WHATSAPP,
    SalesActivityType.MEETING,
    SalesActivityType.FOLLOW_UP,
)


def _normalize_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Ensure a datetime is timezone-aware UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def evaluate_action_readiness(
    opportunity: LeadIntelligenceOpportunity,
    recommended_action: RecommendedSalesAction,
    contact_id: uuid.UUID,
    contact_lead: Optional[Lead],
    company_lead: Optional[Lead] = None,
    active_tasks: Optional[Sequence[SalesTask]] = None,
    recent_contact_time: Optional[datetime] = None,
    eval_time: Optional[datetime] = None,
    cooldown_minutes: Optional[int] = None,
    rec_reasons: Optional[list[str]] = None,
    urgency_reasons: Optional[list[str]] = None,
    priority_score: float = 0.0,
) -> ActionReadinessDetails:
    """
    Evaluates candidate sales action against the 5 Phase 12J-C pre-flight readiness gates:
    - G1: Evidence Sufficiency
    - G2: Target Contact Validity
    - G3: Compliance & Opt-Out
    - G4: Contact Cooldown
    - G5: Collision Guard

    Precedence order when multiple gates fail:
    1. BLOCKED_OPT_OUT
    2. ACTIVE_TASK_EXISTS
    3. BLOCKED_COOLDOWN
    4. MISSING_TARGET_DATA
    5. INSUFFICIENT_EVIDENCE
    If all gates pass: READY.

    Read-Only Contract: Pure evaluation without database mutation or side-effects.
    """
    eval_aware = _normalize_utc(eval_time) or now_utc()
    eff_cooldown = cooldown_minutes if cooldown_minutes is not None else settings.agent_contact_cooldown_minutes
    tasks = list(active_tasks or [])
    action_id = build_action_identity(contact_id, recommended_action)

    # -------------------------------------------------------------------------
    # Gate 1: Evidence Sufficiency (G1)
    # -------------------------------------------------------------------------
    has_evidence = False
    evidence_obj = opportunity.evidence
    source_ref = (opportunity.source_reference or "").strip()
    if evidence_obj and isinstance(evidence_obj, (dict, list)) and len(evidence_obj) > 0:
        has_evidence = True
    elif source_ref:
        has_evidence = True

    g1_reason = "Candidate action lacks verifiable evidence or source reference." if not has_evidence else None

    # -------------------------------------------------------------------------
    # Gate 2: Target Contact Validity (G2)
    # -------------------------------------------------------------------------
    has_valid_contact = True
    g2_reason = None
    email = (contact_lead.email or "").strip() if contact_lead else ""
    phone = (contact_lead.phone or "").strip() if contact_lead else ""
    linkedin = (contact_lead.linkedin_url or "").strip() if contact_lead else ""

    if recommended_action == RecommendedSalesAction.PERSONALIZED_EMAIL:
        if not email or "@" not in email:
            has_valid_contact = False
            g2_reason = "Target contact missing valid email address for personalized email outreach."
    elif recommended_action == RecommendedSalesAction.DISCOVERY_CALL:
        if not phone or len(phone) < 5:
            has_valid_contact = False
            g2_reason = "Target contact missing valid phone number for discovery call."
    elif recommended_action == RecommendedSalesAction.DIRECT_OUTREACH:
        if not (email or phone or linkedin):
            has_valid_contact = False
            g2_reason = "Target contact missing direct outreach coordinates (email, phone, or LinkedIn)."
    elif recommended_action == RecommendedSalesAction.FOLLOW_UP:
        if not (email or phone):
            has_valid_contact = False
            g2_reason = "Target contact missing valid email or phone for follow-up communication."
    elif recommended_action == RecommendedSalesAction.RESEARCH_CONTACT:
        has_valid_contact = True
    else:
        has_valid_contact = True

    # -------------------------------------------------------------------------
    # Gate 3: Compliance & Opt-Out (G3)
    # -------------------------------------------------------------------------
    is_opted_out = False
    g3_reason = None
    opt_out_channel = None

    c_email_opt = bool(contact_lead and contact_lead.email_opted_out)
    p_email_opt = bool(company_lead and company_lead.email_opted_out)
    email_opted = c_email_opt or p_email_opt

    c_phone_opt = bool(contact_lead and contact_lead.phone_opted_out)
    p_phone_opt = bool(company_lead and company_lead.phone_opted_out)
    phone_opted = c_phone_opt or p_phone_opt

    c_wa_opt = bool(contact_lead and contact_lead.whatsapp_opted_out)
    p_wa_opt = bool(company_lead and company_lead.whatsapp_opted_out)
    whatsapp_opted = c_wa_opt or p_wa_opt

    has_email_coord = bool(email and "@" in email)
    has_phone_coord = bool(phone and len(phone) >= 5)
    has_linkedin_coord = bool(linkedin)

    if recommended_action == RecommendedSalesAction.PERSONALIZED_EMAIL:
        if email_opted:
            is_opted_out = True
            opt_out_channel = "EMAIL"
            g3_reason = "Target contact or lead organization has opted out of email communication."
    elif recommended_action == RecommendedSalesAction.DISCOVERY_CALL:
        if phone_opted:
            is_opted_out = True
            opt_out_channel = "PHONE"
            g3_reason = "Target contact or lead organization has opted out of phone communication."
    elif recommended_action == RecommendedSalesAction.FOLLOW_UP:
        if has_email_coord and has_phone_coord:
            if email_opted and phone_opted:
                is_opted_out = True
                opt_out_channel = "ALL"
                g3_reason = "Target contact has opted out of both email and phone follow-up communication."
        elif has_email_coord and not has_phone_coord:
            if email_opted:
                is_opted_out = True
                opt_out_channel = "EMAIL"
                g3_reason = "Target contact has opted out of email communication and has no alternative follow-up channel."
        elif has_phone_coord and not has_email_coord:
            if phone_opted:
                is_opted_out = True
                opt_out_channel = "PHONE"
                g3_reason = "Target contact has opted out of phone communication and has no alternative follow-up channel."
        elif not has_email_coord and not has_phone_coord:
            if email_opted or phone_opted:
                is_opted_out = True
                opt_out_channel = "EMAIL/PHONE"
                g3_reason = "Target contact has recorded opt-out and no valid alternate communication channel."
    elif recommended_action == RecommendedSalesAction.DIRECT_OUTREACH:
        if not has_linkedin_coord:
            if has_email_coord and has_phone_coord and email_opted and phone_opted:
                is_opted_out = True
                opt_out_channel = "ALL"
                g3_reason = "Target contact has opted out of all available direct outreach channels."
            elif has_email_coord and not has_phone_coord and email_opted:
                is_opted_out = True
                opt_out_channel = "EMAIL"
                g3_reason = "Target contact has opted out of email outreach and has no alternative channel."
            elif has_phone_coord and not has_email_coord and phone_opted:
                is_opted_out = True
                opt_out_channel = "PHONE"
                g3_reason = "Target contact has opted out of phone outreach and has no alternative channel."
            elif not has_email_coord and not has_phone_coord and (email_opted or phone_opted):
                is_opted_out = True
                opt_out_channel = "EMAIL/PHONE"
                g3_reason = "Target contact has recorded opt-out and no valid alternate communication channel."
    elif recommended_action == RecommendedSalesAction.RESEARCH_CONTACT:
        is_opted_out = False

    # -------------------------------------------------------------------------
    # Gate 4: Contact Cooldown (G4)
    # -------------------------------------------------------------------------
    in_cooldown = False
    g4_reason = None
    cooldown_until = None

    if recommended_action != RecommendedSalesAction.RESEARCH_CONTACT and eff_cooldown > 0:
        cooldown_delta = timedelta(minutes=eff_cooldown)
        if recent_contact_time is not None:
            c_time = _normalize_utc(recent_contact_time)
            if c_time and c_time > eval_aware - cooldown_delta:
                in_cooldown = True
                cooldown_until_dt = c_time + cooldown_delta
                cooldown_until = cooldown_until_dt.isoformat()
                g4_reason = f"Target contact in active cooldown until {cooldown_until} (last outreach was at {c_time.isoformat()})."

    # -------------------------------------------------------------------------
    # Gate 5: Collision Guard (G5)
    # -------------------------------------------------------------------------
    has_active_task = False
    g5_reason = None
    colliding_task_id = None

    for task in tasks:
        if task.status in (SalesTaskStatus.OPEN, SalesTaskStatus.IN_PROGRESS):
            if task.agent_source_id == opportunity.id or task.agent_source_action_id == action_id:
                has_active_task = True
                colliding_task_id = str(task.id)
                g5_reason = f"Active sales task already exists ({task.id}) in status {task.status.value}."
                break

    # -------------------------------------------------------------------------
    # Precedence Resolution and Detail Aggregation
    # -------------------------------------------------------------------------
    all_reasons: list[str] = []
    blocking_details: dict[str, Any] = {}
    primary_status = SupervisorActionReadinessStatus.READY

    # Precedence 1: BLOCKED_OPT_OUT
    if is_opted_out:
        primary_status = SupervisorActionReadinessStatus.BLOCKED_OPT_OUT
        if g3_reason:
            all_reasons.append(g3_reason)
        blocking_details["opt_out_channel"] = opt_out_channel

    # Precedence 2: ACTIVE_TASK_EXISTS
    if has_active_task:
        if primary_status == SupervisorActionReadinessStatus.READY:
            primary_status = SupervisorActionReadinessStatus.ACTIVE_TASK_EXISTS
        if g5_reason:
            all_reasons.append(g5_reason)
        blocking_details["active_task_id"] = colliding_task_id

    # Precedence 3: BLOCKED_COOLDOWN
    if in_cooldown:
        if primary_status == SupervisorActionReadinessStatus.READY:
            primary_status = SupervisorActionReadinessStatus.BLOCKED_COOLDOWN
        if g4_reason:
            all_reasons.append(g4_reason)
        blocking_details["cooldown_until"] = cooldown_until

    # Precedence 4: MISSING_TARGET_DATA
    if not has_valid_contact:
        if primary_status == SupervisorActionReadinessStatus.READY:
            primary_status = SupervisorActionReadinessStatus.MISSING_TARGET_DATA
        if g2_reason:
            all_reasons.append(g2_reason)
        blocking_details["missing_contact_data"] = True

    # Precedence 5: INSUFFICIENT_EVIDENCE
    if not has_evidence:
        if primary_status == SupervisorActionReadinessStatus.READY:
            primary_status = SupervisorActionReadinessStatus.INSUFFICIENT_EVIDENCE
        if g1_reason:
            all_reasons.append(g1_reason)
        blocking_details["insufficient_evidence"] = True

    if primary_status == SupervisorActionReadinessStatus.READY:
        all_reasons.append(f"Candidate action passed all readiness gates with priority score {priority_score}/100")
        if rec_reasons:
            all_reasons.extend(rec_reasons)
        if urgency_reasons:
            all_reasons.extend(urgency_reasons)

    return ActionReadinessDetails(
        status=primary_status,
        reasons=all_reasons,
        has_valid_contact=has_valid_contact,
        has_evidence=has_evidence,
        is_opted_out=is_opted_out,
        in_cooldown=in_cooldown,
        has_active_task=has_active_task,
        blocking_details=blocking_details if blocking_details else None,
    )


async def evaluate_sales_supervisor_pipeline(
    session: AsyncSession,
    lead_id: Optional[uuid.UUID] = None,
    limit: int = 50,
    now: Optional[datetime] = None,
    cooldown_minutes: Optional[int] = None,
) -> SalesSupervisorPipelineResponse:
    """
    Evaluates actionable sales opportunities across the pipeline, computes
    deterministic prioritization rankings, and executes Phase 12J-C pre-flight
    readiness evaluations for each candidate action.

    Read-Only Contract:
    - Does NOT create, update, or delete SalesTask records.
    - Does NOT dispatch external communication (email, SMS, calls).
    - Returns deterministic, audit-traceable candidate actions ranked by priority score.
    """
    eval_time = _normalize_utc(now) or now_utc()
    eff_cooldown = cooldown_minutes if cooldown_minutes is not None else settings.agent_contact_cooldown_minutes

    # 1. Query eligible opportunities
    if lead_id is not None:
        opp_stmt = (
            select(LeadIntelligenceOpportunity)
            .where(LeadIntelligenceOpportunity.lead_id == lead_id)
            .order_by(LeadIntelligenceOpportunity.score.desc().nullslast())
        )
    else:
        opp_stmt = (
            select(LeadIntelligenceOpportunity)
            .order_by(LeadIntelligenceOpportunity.score.desc().nullslast())
            .limit(limit)
        )

    opp_res = await session.execute(opp_stmt)
    opportunities = list(opp_res.scalars().all())

    if not opportunities:
        return SalesSupervisorPipelineResponse(
            evaluated_at=eval_time,
            total_candidates=0,
            ready_count=0,
            blocked_count=0,
            actions=[],
            summary={
                "total_opportunities_evaluated": 0,
                "candidates_generated": 0,
                "skipped_no_recommendation": 0,
                "ready_count": 0,
                "blocked_count": 0,
            },
        )

    # 2. Batch-fetch associated leads and active buying signals to avoid N+1 queries
    unique_lead_ids = list({opp.lead_id for opp in opportunities})
    leads_res = await session.execute(select(Lead).where(Lead.id.in_(unique_lead_ids)))
    leads_map: dict[uuid.UUID, Lead] = {l.id: l for l in leads_res.scalars().all()}

    signals_res = await session.execute(
        select(BuyingSignal).where(
            BuyingSignal.company_id.in_(unique_lead_ids),
            BuyingSignal.status == "ACTIVE",
        )
    )
    signals_by_lead: dict[uuid.UUID, list[BuyingSignal]] = {}
    for sig in signals_res.scalars().all():
        signals_by_lead.setdefault(sig.company_id, []).append(sig)

    # 3. Retrieve recommendations and collect all relevant contact lead IDs
    evaluated_raw_candidates: list[dict[str, Any]] = []
    skipped_no_rec = 0
    all_target_lead_ids: set[uuid.UUID] = set(unique_lead_ids)

    for opp in opportunities:
        opp_score_val = validate_source_score("opportunity_score", opp.score)
        rec = await get_contact_recommendation_for_opportunity(session, opp.id)
        if not rec or not rec.get("recommended_contact"):
            skipped_no_rec += 1
            continue

        raw_action = rec.get("recommended_action")
        if isinstance(raw_action, str):
            try:
                rec_action = RecommendedSalesAction(raw_action)
            except ValueError:
                rec_action = RecommendedSalesAction.NO_ACTION
        else:
            rec_action = raw_action or RecommendedSalesAction.NO_ACTION

        if rec_action in (None, RecommendedSalesAction.NO_ACTION):
            skipped_no_rec += 1
            continue

        rec_score_raw = rec.get("recommendation_score")
        rec_score_val = validate_source_score("recommendation_fit", rec_score_raw)

        contact_item = rec["recommended_contact"]
        contact_id = uuid.UUID(str(contact_item["contact_id"]))
        all_target_lead_ids.add(contact_id)

        evaluated_raw_candidates.append({
            "opp": opp,
            "opp_score_val": opp_score_val,
            "rec": rec,
            "rec_action": rec_action,
            "rec_score_val": rec_score_val,
            "contact_id": contact_id,
        })

    # 4. Batch-fetch any contact leads not already loaded
    missing_lead_ids = [lid for lid in all_target_lead_ids if lid not in leads_map]
    if missing_lead_ids:
        missing_res = await session.execute(select(Lead).where(Lead.id.in_(missing_lead_ids)))
        for l in missing_res.scalars().all():
            leads_map[l.id] = l

    # 5. Batch-fetch active tasks for collision guard
    active_tasks_res = await session.execute(
        select(SalesTask).where(
            SalesTask.status.in_([SalesTaskStatus.OPEN, SalesTaskStatus.IN_PROGRESS]),
            or_(
                SalesTask.lead_id.in_(list(all_target_lead_ids)),
                SalesTask.agent_source_id.in_([opp.id for opp in opportunities]),
            ),
        )
    )
    all_active_tasks = list(active_tasks_res.scalars().all())

    # 6. Batch-fetch recent contact events for cooldown
    cutoff_time = eval_time - timedelta(minutes=max(0, eff_cooldown))
    recent_contact_map: dict[uuid.UUID, datetime] = {}

    # SalesActivity
    activity_res = await session.execute(
        select(SalesActivity.lead_id, SalesActivity.occurred_at).where(
            SalesActivity.lead_id.in_(list(all_target_lead_ids)),
            SalesActivity.activity_type.in_(RECENT_CONTACT_ACTIVITY_TYPES),
            SalesActivity.occurred_at >= cutoff_time,
        )
    )
    for lid, occ_at in activity_res.all():
        if occ_at:
            occ_utc = _normalize_utc(occ_at)
            cur = recent_contact_map.get(lid)
            recent_contact_map[lid] = max(cur, occ_utc) if cur else occ_utc

    # OutreachExecution
    outreach_res = await session.execute(
        select(OutreachExecution.lead_id, OutreachExecution.sent_at).where(
            OutreachExecution.lead_id.in_(list(all_target_lead_ids)),
            OutreachExecution.sent_at.is_not(None),
            OutreachExecution.sent_at >= cutoff_time,
        )
    )
    for lid, sent_at in outreach_res.all():
        if sent_at:
            sent_utc = _normalize_utc(sent_at)
            cur = recent_contact_map.get(lid)
            recent_contact_map[lid] = max(cur, sent_utc) if cur else sent_utc

    # AiOutreachExecution
    ai_outreach_res = await session.execute(
        select(AiOutreachExecution.lead_id, AiOutreachExecution.completed_at).where(
            AiOutreachExecution.lead_id.in_(list(all_target_lead_ids)),
            AiOutreachExecution.status == "SENT",
            AiOutreachExecution.completed_at.is_not(None),
            AiOutreachExecution.completed_at >= cutoff_time,
        )
    )
    for lid, comp_at in ai_outreach_res.all():
        if comp_at:
            comp_utc = _normalize_utc(comp_at)
            cur = recent_contact_map.get(lid)
            recent_contact_map[lid] = max(cur, comp_utc) if cur else comp_utc

    # 7. Evaluate each candidate action
    candidate_actions: list[PrioritizedSalesAction] = []

    for item in evaluated_raw_candidates:
        opp = item["opp"]
        opp_score_val = item["opp_score_val"]
        rec = item["rec"]
        rec_action = item["rec_action"]
        rec_score_val = item["rec_score_val"]
        contact_id = item["contact_id"]

        lead_obj = leads_map.get(opp.lead_id)
        contact_lead_obj = leads_map.get(contact_id)
        lead_signals = signals_by_lead.get(opp.lead_id, [])

        urgency_score, urgency_band, urgency_reasons = map_buying_signals_to_urgency(
            lead=lead_obj,
            signals=lead_signals,
            opportunity=opp,
            now=eval_time,
        )

        # Resolve closed-loop historical feedback for this candidate action
        fb_res = await get_opportunity_sales_feedback(session, opp.id)
        fb_rate: Optional[float] = None
        fb_executions: int = 0
        feedback_evidence: list[str] = []

        if fb_res:
            rec_action_key = rec_action.value if hasattr(rec_action, "value") else str(rec_action)
            act_perf = fb_res.action_performance.get(rec_action_key)

            # 1. Action-specific verified feedback (most granular causal feedback)
            if act_perf and act_perf.verified_executions > 0 and act_perf.verified_positive_rate is not None:
                fb_executions = act_perf.verified_executions
                fb_rate = validate_source_score("historical_feedback.action_positive_rate", act_perf.verified_positive_rate)
            # 2. Opportunity-wide verified feedback (if action-specific is not available)
            elif fb_res.verified_executions > 0 and fb_res.verified_positive_rate is not None:
                fb_executions = fb_res.verified_executions
                fb_rate = validate_source_score("historical_feedback.opportunity_positive_rate", fb_res.verified_positive_rate)
            # 3. Fallback when all outcomes are unresolved/missing or 0 verified executions:
            else:
                fb_executions = 0
                fb_rate = None

            # Add verified execution provenance to evidence keys
            for fb_item in fb_res.items:
                if fb_item.sentiment in (FeedbackSentiment.POSITIVE, FeedbackSentiment.NEGATIVE, FeedbackSentiment.NEUTRAL):
                    if fb_item.activity_id:
                        feedback_evidence.append(f"feedback_activity:{fb_item.activity_id}")
                    elif fb_item.task_id:
                        feedback_evidence.append(f"feedback_task:{fb_item.task_id}")

        breakdown = calculate_supervisor_action_priority(
            opportunity_score=opp_score_val,
            recommendation_fit=rec_score_val,
            historical_feedback=fb_rate,
            urgency=urgency_score,
            total_feedback_executions=fb_executions,
        )

        action_id = build_action_identity(contact_id, rec_action)

        # Relevant active tasks for this action/lead
        candidate_active_tasks = [
            t for t in all_active_tasks
            if t.lead_id in (opp.lead_id, contact_id) or t.agent_source_id == opp.id
        ]

        # Recent contact time for cooldown
        latest_c: Optional[datetime] = None
        for lid in (opp.lead_id, contact_id):
            t = recent_contact_map.get(lid)
            if t:
                latest_c = max(latest_c, t) if latest_c else t

        # Execute Phase 12J-C Pre-Flight Readiness Evaluation
        readiness = evaluate_action_readiness(
            opportunity=opp,
            recommended_action=rec_action,
            contact_id=contact_id,
            contact_lead=contact_lead_obj,
            company_lead=lead_obj,
            active_tasks=candidate_active_tasks,
            recent_contact_time=latest_c,
            eval_time=eval_time,
            cooldown_minutes=eff_cooldown,
            rec_reasons=rec.get("reasons", []),
            urgency_reasons=urgency_reasons,
            priority_score=breakdown.final_score,
        )

        evidence_keys = [
            f"opportunity:{opp.id}",
            f"contact:{contact_id}",
            f"action:{action_id}",
        ]
        if lead_signals:
            evidence_keys.extend([f"signal:{s.id}" for s in lead_signals[:5]])
        if feedback_evidence:
            evidence_keys.extend(feedback_evidence[:5])

        prioritized_action = PrioritizedSalesAction(
            action_id=action_id,
            lead_id=opp.lead_id,
            opportunity_id=opp.id,
            contact_id=contact_id,
            recommended_action=rec_action,
            opportunity_score=opp_score_val,
            recommendation_fit=rec_score_val,
            historical_feedback_rate=fb_rate if (fb_executions > 0 and fb_rate is not None) else None,
            urgency_score=urgency_score,
            priority_score=breakdown.final_score,
            score_breakdown=SupervisorActionScoreBreakdownOut.from_breakdown(breakdown),
            readiness_status=readiness.status,
            readiness_details=readiness,
            evidence_keys=evidence_keys,
            policy_approval_required=is_policy_approval_required(rec_action),
            created_at=eval_time,
        )
        candidate_actions.append(prioritized_action)

    # 8. Deterministic stable ranking: highest priority first, tie-broken predictably
    ranked_actions = sort_prioritized_actions(
        candidate_actions,
        score_key=lambda a: a.priority_score,
        opp_key=lambda a: a.opportunity_score,
        rec_key=lambda a: a.recommendation_fit,
        urg_key=lambda a: a.urgency_score,
        id_key=lambda a: f"{a.action_id}:{a.opportunity_id}",
    )

    ready_count = sum(1 for a in ranked_actions if a.readiness_status == SupervisorActionReadinessStatus.READY)
    blocked_count = sum(1 for a in ranked_actions if a.readiness_status != SupervisorActionReadinessStatus.READY)

    return SalesSupervisorPipelineResponse(
        evaluated_at=eval_time,
        total_candidates=len(ranked_actions),
        ready_count=ready_count,
        blocked_count=blocked_count,
        actions=ranked_actions,
        summary={
            "total_opportunities_evaluated": len(opportunities),
            "candidates_generated": len(ranked_actions),
            "skipped_no_recommendation": skipped_no_rec,
            "ready_count": ready_count,
            "blocked_count": blocked_count,
            "highest_priority_score": ranked_actions[0].priority_score if ranked_actions else None,
        },
    )


def _derive_expected_channel(action: Any) -> Optional[str]:
    """
    Derives the primary external communication channel expected for an outreach action.
    Returns uppercase channel name ('EMAIL', 'PHONE', 'DIRECT') or None if multi-channel/internal.
    """
    action_val = action.value if hasattr(action, "value") else str(action)
    action_val = action_val.strip().upper()
    if action_val == "PERSONALIZED_EMAIL":
        return "EMAIL"
    elif action_val == "DISCOVERY_CALL":
        return "PHONE"
    elif action_val == "DIRECT_OUTREACH":
        return "DIRECT"
    elif action_val == "FOLLOW_UP":
        return None  # Multi-channel (can be EMAIL or PHONE)
    return None


def authorize_supervisor_action_execution(
    action: PrioritizedSalesAction,
    approval: Optional[SupervisorHumanApproval] = None,
    eval_time: Optional[datetime] = None,
) -> SupervisorExecutionAuthorizationResult:
    """
    Determines whether a candidate sales action is authorized for execution.

    Safety Rules & Invariants:
    1. Unknown Action Types Fail Closed: If an action cannot be verified and classified,
       authorization is DENIED with denial_code='UNKNOWN_ACTION_TYPE'.
    2. Readiness Gate Prerequisite: Action readiness MUST be READY.
       No prioritization score (even 100.0) or human approval can ever override a
       failed readiness gate (opt-out, cooldown, collision, missing target, insufficient evidence).
    3. External Outreach Actions: Strictly require explicit, unexpired human approval with
       valid approver identity matching the exact candidate action (action_id, lead_id,
       opportunity_id, contact_id, and channel if specified). Missing approval returns
       REQUIRES_APPROVAL; mismatched or expired approval returns DENIED.
    4. Internal Operations: Can proceed with system authorization (AUTHORIZED) without a human
       signature provided readiness is READY and internal semantics are verified. If explicit
       human approval is provided with REJECTED or REVOKED status, the rejection is respected.
    5. Pure Decision Logic: Zero external communications dispatched; zero task records mutated.
    """
    eval_aware = _normalize_utc(eval_time) or now_utc()

    # 1. Action classification (fail-closed if unknown)
    try:
        action_tier = classify_supervisor_action(action.recommended_action)
    except (ValueError, TypeError) as exc:
        return SupervisorExecutionAuthorizationResult(
            authorized=False,
            decision=SupervisorExecutionDecision.DENIED,
            action_id=action.action_id,
            reasons=[f"Execution authorization denied: {str(exc)}"],
            readiness_status=action.readiness_status,
            action_tier=SupervisorActionTier.EXTERNAL_OUTREACH,
            approval_required=True,
            approval_details=approval,
            denial_code="UNKNOWN_ACTION_TYPE",
            evaluated_at=eval_aware,
        )

    approval_required = action_tier == SupervisorActionTier.EXTERNAL_OUTREACH

    # 2. Readiness prerequisite (BLOCKED gates CANNOT be bypassed by approval or priority score)
    if action.readiness_status != SupervisorActionReadinessStatus.READY:
        denial_reason = (
            f"Execution authorization denied: Candidate action readiness is {action.readiness_status.value}. "
            f"Readiness gates cannot be overridden by approval or priority score ({action.priority_score}/100)."
        )
        return SupervisorExecutionAuthorizationResult(
            authorized=False,
            decision=SupervisorExecutionDecision.DENIED,
            action_id=action.action_id,
            reasons=[denial_reason, *action.readiness_details.reasons],
            readiness_status=action.readiness_status,
            action_tier=action_tier,
            approval_required=approval_required,
            approval_details=approval,
            denial_code=f"READINESS_{action.readiness_status.value}",
            evaluated_at=eval_aware,
        )

    # 3. External outreach requires valid human approval
    if action_tier == SupervisorActionTier.EXTERNAL_OUTREACH:
        if approval is None:
            return SupervisorExecutionAuthorizationResult(
                authorized=False,
                decision=SupervisorExecutionDecision.REQUIRES_APPROVAL,
                action_id=action.action_id,
                reasons=[
                    f"Execution authorization withheld: External outreach action '{action.recommended_action.value}' "
                    "requires explicit human approval."
                ],
                readiness_status=action.readiness_status,
                action_tier=action_tier,
                approval_required=True,
                approval_details=None,
                denial_code="APPROVAL_REQUIRED",
                evaluated_at=eval_aware,
            )

        # Check approver identity
        if not approval.approver_id or not approval.approver_id.strip():
            return SupervisorExecutionAuthorizationResult(
                authorized=False,
                decision=SupervisorExecutionDecision.DENIED,
                action_id=action.action_id,
                reasons=["Execution authorization denied: Approver identity is missing or empty."],
                readiness_status=action.readiness_status,
                action_tier=action_tier,
                approval_required=True,
                approval_details=approval,
                denial_code="INVALID_APPROVER_IDENTITY",
                evaluated_at=eval_aware,
            )

        # Check approval status
        if approval.status != SupervisorApprovalStatus.APPROVED:
            return SupervisorExecutionAuthorizationResult(
                authorized=False,
                decision=SupervisorExecutionDecision.DENIED,
                action_id=action.action_id,
                reasons=[f"Execution authorization denied: Human approval status is '{approval.status.value}'."],
                readiness_status=action.readiness_status,
                action_tier=action_tier,
                approval_required=True,
                approval_details=approval,
                denial_code=f"APPROVAL_{approval.status.value}",
                evaluated_at=eval_aware,
            )

        # Check expiration
        if approval.expires_at is not None:
            exp_aware = _normalize_utc(approval.expires_at)
            if exp_aware and eval_aware > exp_aware:
                return SupervisorExecutionAuthorizationResult(
                    authorized=False,
                    decision=SupervisorExecutionDecision.DENIED,
                    action_id=action.action_id,
                    reasons=[f"Execution authorization denied: Human approval expired at {exp_aware.isoformat()}."],
                    readiness_status=action.readiness_status,
                    action_tier=action_tier,
                    approval_required=True,
                    approval_details=approval,
                    denial_code="APPROVAL_EXPIRED",
                    evaluated_at=eval_aware,
                )

        # Check action identity match
        if approval.action_id != action.action_id:
            return SupervisorExecutionAuthorizationResult(
                authorized=False,
                decision=SupervisorExecutionDecision.DENIED,
                action_id=action.action_id,
                reasons=[
                    f"Execution authorization denied: Approval action_id '{approval.action_id}' "
                    f"does not match candidate action_id '{action.action_id}'."
                ],
                readiness_status=action.readiness_status,
                action_tier=action_tier,
                approval_required=True,
                approval_details=approval,
                denial_code="ACTION_MISMATCH",
                evaluated_at=eval_aware,
            )

        # Check lead identity match
        if approval.lead_id != action.lead_id:
            return SupervisorExecutionAuthorizationResult(
                authorized=False,
                decision=SupervisorExecutionDecision.DENIED,
                action_id=action.action_id,
                reasons=[
                    f"Execution authorization denied: Approval lead_id '{approval.lead_id}' "
                    f"does not match candidate lead_id '{action.lead_id}'."
                ],
                readiness_status=action.readiness_status,
                action_tier=action_tier,
                approval_required=True,
                approval_details=approval,
                denial_code="LEAD_MISMATCH",
                evaluated_at=eval_aware,
            )

        # Check opportunity identity match
        if approval.opportunity_id != action.opportunity_id:
            return SupervisorExecutionAuthorizationResult(
                authorized=False,
                decision=SupervisorExecutionDecision.DENIED,
                action_id=action.action_id,
                reasons=[
                    f"Execution authorization denied: Approval opportunity_id '{approval.opportunity_id}' "
                    f"does not match candidate opportunity_id '{action.opportunity_id}'."
                ],
                readiness_status=action.readiness_status,
                action_tier=action_tier,
                approval_required=True,
                approval_details=approval,
                denial_code="OPPORTUNITY_MISMATCH",
                evaluated_at=eval_aware,
            )

        # Check contact identity match (if contact is specified)
        if action.contact_id is not None and approval.contact_id != action.contact_id:
            return SupervisorExecutionAuthorizationResult(
                authorized=False,
                decision=SupervisorExecutionDecision.DENIED,
                action_id=action.action_id,
                reasons=[
                    f"Execution authorization denied: Approval contact_id '{approval.contact_id}' "
                    f"does not match candidate contact_id '{action.contact_id}'."
                ],
                readiness_status=action.readiness_status,
                action_tier=action_tier,
                approval_required=True,
                approval_details=approval,
                denial_code="CONTACT_MISMATCH",
                evaluated_at=eval_aware,
            )

        # Check channel match if channel specified in approval
        if approval.channel is not None and approval.channel.strip():
            raw_appr_channel = approval.channel.strip().upper()
            expected_channel = _derive_expected_channel(action.recommended_action)
            if expected_channel and raw_appr_channel != expected_channel:
                return SupervisorExecutionAuthorizationResult(
                    authorized=False,
                    decision=SupervisorExecutionDecision.DENIED,
                    action_id=action.action_id,
                    reasons=[
                        f"Execution authorization denied: Approval channel '{approval.channel}' "
                        f"does not match action expected channel '{expected_channel}'."
                    ],
                    readiness_status=action.readiness_status,
                    action_tier=action_tier,
                    approval_required=True,
                    approval_details=approval,
                    denial_code="CHANNEL_MISMATCH",
                    evaluated_at=eval_aware,
                )

        return SupervisorExecutionAuthorizationResult(
            authorized=True,
            decision=SupervisorExecutionDecision.AUTHORIZED,
            action_id=action.action_id,
            reasons=[
                f"External outreach action '{action.recommended_action.value}' authorized with "
                f"valid human approval from '{approval.approver_id}'."
            ],
            readiness_status=action.readiness_status,
            action_tier=action_tier,
            approval_required=True,
            approval_details=approval,
            denial_code=None,
            evaluated_at=eval_aware,
        )

    # 4. Internal operations
    if action_tier == SupervisorActionTier.INTERNAL_OPERATION:
        # If human approval is provided and explicitly REJECTED/REVOKED, respect it
        if approval is not None and approval.status in (SupervisorApprovalStatus.REJECTED, SupervisorApprovalStatus.REVOKED):
            return SupervisorExecutionAuthorizationResult(
                authorized=False,
                decision=SupervisorExecutionDecision.DENIED,
                action_id=action.action_id,
                reasons=[
                    f"Execution authorization denied: Human supervisor explicitly marked action as '{approval.status.value}'."
                ],
                readiness_status=action.readiness_status,
                action_tier=action_tier,
                approval_required=False,
                approval_details=approval,
                denial_code=f"APPROVAL_{approval.status.value}",
                evaluated_at=eval_aware,
            )

        return SupervisorExecutionAuthorizationResult(
            authorized=True,
            decision=SupervisorExecutionDecision.AUTHORIZED,
            action_id=action.action_id,
            reasons=[
                f"Internal operation '{action.recommended_action.value}' authorized for system workflow execution "
                "(readiness verified, no external communication required)."
            ],
            readiness_status=action.readiness_status,
            action_tier=action_tier,
            approval_required=False,
            approval_details=approval,
            denial_code=None,
            evaluated_at=eval_aware,
        )


def evaluate_pipeline_execution_authorizations(
    actions: Sequence[PrioritizedSalesAction],
    approvals: Optional[dict[str, SupervisorHumanApproval]] = None,
    eval_time: Optional[datetime] = None,
) -> list[SupervisorExecutionAuthorizationResult]:
    """
    Evaluates execution authorizations for a batch of prioritized actions across the pipeline.
    Matches approvals by action_id.
    """
    approvals_map = approvals or {}
    results = []
    for action in actions:
        appr = approvals_map.get(action.action_id)
        results.append(authorize_supervisor_action_execution(action, appr, eval_time=eval_time))
    return results


async def authorize_sales_supervisor_action_durable(
    session: AsyncSession,
    action: PrioritizedSalesAction,
    channel: Optional[str] = None,
    contact_email: Optional[str] = None,
    contact_phone: Optional[str] = None,
    now: Optional[datetime] = None,
) -> SupervisorExecutionAuthorizationResult:
    """
    Evaluates execution authorization for a prioritized action using the durable
    human approval repository and lifecycle service.
    """
    from app.services.supervisor_approval_service import SupervisorApprovalService

    service = SupervisorApprovalService()
    return await service.authorize_action_execution(
        session,
        action=action,
        channel=channel,
        contact_email=contact_email,
        contact_phone=contact_phone,
        now=now,
    )
