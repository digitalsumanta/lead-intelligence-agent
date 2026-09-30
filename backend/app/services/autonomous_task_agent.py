"""Deterministic, policy-gated execution of internal SalesTask records."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, time, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.ai_sales_action_planner import ActionPlanOutput, ActionType
from app.domain.ai_sales_intelligence import AIValidationError
from app.domain.ai_sales_supervisor import SupervisorResult
from app.models import (
    AgentPolicyEvaluation, AiSalesActionPlan, AiSalesSupervisorRun, AutonomousTaskExecution,
    Lead, LeadStatus, SalesPriority, SalesTask, SalesTaskStatus, SalesTaskType,
)
from app.models.entities import now_utc
from app.schemas.agent_policy import PolicyDecision, PolicyEvaluationRequest, PolicyChannel
from app.schemas.autonomous_task_agent import (
    TaskAgentSourceType, TaskExecutionRequest, TaskExecutionResult, TaskExecutionStatus,
)
from app.schemas.sales_tasks import SalesTaskCreate
from app.services.ai_sales_intelligence import AISalesContextBuilder, validate_context_references
from app.services.ai_sales_action_planner import validate_action_plan
from app.services.ai_sales_supervisor import ACTION_TO_ATTENTION, build_pipeline_context
from app.services.agent_policy import evaluate_policy
from app.services.sales_tasks import create_task


ACTION_TASK = {
    ActionType.FOLLOW_UP: SalesTaskType.FOLLOW_UP,
    ActionType.CALL_PROSPECT: SalesTaskType.CALL,
    ActionType.REVIEW_SEO_OPPORTUNITY: SalesTaskType.OTHER,
    ActionType.REVIEW_OUTREACH: SalesTaskType.REVIEW_OUTREACH,
    ActionType.PREPARE_PROPOSAL: SalesTaskType.PROPOSAL,
    ActionType.INVESTIGATE_MISSING_INFORMATION: SalesTaskType.OTHER,
    ActionType.REVIEW_COMMERCIAL_CONTEXT: SalesTaskType.OTHER,
}
ACTION_TITLE = {
    ActionType.FOLLOW_UP: "Follow up with {company}",
    ActionType.CALL_PROSPECT: "Call prospect: {company}",
    ActionType.REVIEW_SEO_OPPORTUNITY: "Review SEO opportunity: {company}",
    ActionType.REVIEW_OUTREACH: "Review previous outreach: {company}",
    ActionType.PREPARE_PROPOSAL: "Prepare proposal for {company}",
    ActionType.INVESTIGATE_MISSING_INFORMATION: "Investigate missing information: {company}",
    ActionType.REVIEW_COMMERCIAL_CONTEXT: "Review commercial context: {company}",
}
TASK_AGENT_REQUESTER = "autonomous_task_agent"


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _plan_action_id(action) -> str:
    return _hash({
        "action_type": action.action_type.value,
        "evidence_ids": sorted(action.evidence_ids),
        "opportunity_ids": sorted(action.opportunity_ids),
        "service_ids": sorted(action.service_ids),
    })


def _idempotency_key(request: TaskExecutionRequest) -> str:
    return _hash({
        "lead_id": str(request.lead_id), "source_type": request.source_type.value,
        "source_id": str(request.source_id), "source_action_id": request.source_action_id,
        "action_type": request.action_type.value,
    })


def _next_business_review_slot(now: datetime) -> datetime:
    day = now.astimezone(timezone.utc).date()
    while True:
        day += timedelta(days=1)
        if day.weekday() < 5:
            return datetime.combine(day, time(hour=9), tzinfo=timezone.utc)


def _due_at(action_type: ActionType, lead: Lead, now: datetime) -> datetime:
    if action_type == ActionType.FOLLOW_UP and lead.next_follow_up_at is not None:
        value = lead.next_follow_up_at
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    if action_type == ActionType.REVIEW_SEO_OPPORTUNITY:
        return _next_business_review_slot(now)
    return now + timedelta(days=1)


def _requested_channel(action_type: ActionType, lead: Lead) -> PolicyChannel | None:
    if action_type == ActionType.CALL_PROSPECT:
        return PolicyChannel.PHONE
    if action_type != ActionType.FOLLOW_UP:
        return None
    if lead.email and not lead.email_opted_out:
        return PolicyChannel.EMAIL
    if lead.phone and not lead.whatsapp_opted_out:
        return PolicyChannel.WHATSAPP
    return PolicyChannel.PHONE


async def _load_source_action(session: AsyncSession, request: TaskExecutionRequest, context, fingerprint: str, catalog, *, validated_supervisor_fingerprint: str | None = None):
    if request.source_type == TaskAgentSourceType.AI_SALES_ACTION_PLAN:
        artifact = await session.scalar(select(AiSalesActionPlan).where(
            AiSalesActionPlan.id == request.source_id,
            AiSalesActionPlan.lead_id == request.lead_id,
        ))
        if artifact is None or artifact.status != "validated":
            raise ValueError("A validated action-plan artifact for this lead is required.")
        if artifact.source_fingerprint != fingerprint or request.context_fingerprint != fingerprint:
            raise StaleContextError
        output = ActionPlanOutput.model_validate(artifact.action_plan)
        validate_action_plan(output, context, catalog, request.lead_id)
        matches = [item for item in output.actions if item.action_type == request.action_type]
        action = next((item for item in matches if _plan_action_id(item) == request.source_action_id), None)
        if action is None:
            raise ValueError("The requested action is not present in the persisted action plan.")
        expected_evidence = set(action.evidence_ids) | set(action.opportunity_ids)
        if not set(request.evidence_ids).issubset(expected_evidence):
            raise AIValidationError("Requested evidence is not part of the persisted action.")
        validate_context_references(set(request.evidence_ids), context, catalog, request.lead_id)
        return artifact, action, action.priority, action.preparation_items

    run = await session.scalar(select(AiSalesSupervisorRun).where(
        AiSalesSupervisorRun.id == request.source_id,
        AiSalesSupervisorRun.scope == "PIPELINE",
        AiSalesSupervisorRun.status == "COMPLETED",
    ))
    if run is None or run.result_json is None:
        raise ValueError("A completed Sales Supervisor run is required.")
    if validated_supervisor_fingerprint is not None:
        if run.context_fingerprint != validated_supervisor_fingerprint:
            raise StaleContextError
    else:
        _, _, current_pipeline_fingerprint = await build_pipeline_context(session)
        if run.context_fingerprint != current_pipeline_fingerprint:
            raise StaleContextError
    result = SupervisorResult.model_validate(run.result_json)
    finding = next((item for item in result.findings if item.finding_id == request.source_action_id), None)
    if finding is None or finding.lead_id != request.lead_id or finding.action_type != request.action_type:
        raise ValueError("The requested action is not present for this lead in the persisted Supervisor run.")
    if request.context_fingerprint != fingerprint:
        raise StaleContextError
    if finding.attention_type not in ACTION_TO_ATTENTION.get(finding.action_type, set()):
        raise ValueError("The persisted Supervisor action is incompatible with its attention finding.")
    if finding.policy_decision == "DENY":
        raise ValueError("A policy-denied Supervisor finding cannot be executed.")
    if not set(request.evidence_ids).issubset(set(finding.evidence_ids)):
        raise AIValidationError("Requested evidence is not part of the persisted finding.")
    validate_context_references(set(request.evidence_ids) | set(finding.opportunity_ids), context, catalog, request.lead_id)
    return run, finding, SalesPriority.MEDIUM, ()


class StaleContextError(ValueError):
    pass


def _audit_result(record: AutonomousTaskExecution) -> TaskExecutionResult:
    status = TaskExecutionStatus(record.execution_status)
    return TaskExecutionResult(
        execution_id=record.id, lead_id=record.lead_id, action_type=ActionType(record.action_type),
        execution_status=status, policy_decision=PolicyDecision(record.policy_decision) if record.policy_decision else None,
        task_id=record.task_id, created=status == TaskExecutionStatus.CREATED,
        existing_task_id=record.task_id if status == TaskExecutionStatus.ALREADY_EXISTS else None,
        context_fingerprint=record.context_fingerprint, evidence_ids=tuple(record.evidence_refs or ()),
        source_artifact_id=record.source_id, source_action_id=record.source_action_id,
        policy_evaluation_id=record.policy_evaluation_id, reason=record.reason,
        executed_at=record.executed_at, created_at=record.created_at,
    )


async def _persist_attempt(
    session: AsyncSession, request: TaskExecutionRequest, *, fingerprint: str, key: str,
    status: TaskExecutionStatus, reason: str, policy_id: UUID | None = None,
    policy_decision: PolicyDecision | None = None, task_id: UUID | None = None,
    executed_at: datetime | None = None,
) -> TaskExecutionResult:
    row = AutonomousTaskExecution(
        lead_id=request.lead_id, action_type=request.action_type.value,
        source_type=request.source_type.value, source_id=request.source_id,
        source_action_id=request.source_action_id, task_id=task_id,
        policy_evaluation_id=policy_id,
        policy_decision=policy_decision.value if policy_decision else None,
        context_fingerprint=fingerprint, evidence_refs=sorted(set(request.evidence_ids)),
        execution_status=status.value, idempotency_key=key, reason=reason[:500],
        requested_by=TASK_AGENT_REQUESTER, created_at=now_utc(), executed_at=executed_at,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return _audit_result(row)


async def execute_task_action(
    session: AsyncSession, request: TaskExecutionRequest, *, now: datetime | None = None,
    validated_supervisor_fingerprint: str | None = None,
) -> TaskExecutionResult:
    now = now or now_utc()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    key = _idempotency_key(request)
    lead = await session.get(Lead, request.lead_id)
    if lead is None:
        raise LookupError("Lead not found")
    context, fingerprint, catalog = await AISalesContextBuilder().build(session, request.lead_id)
    prior_source = await session.scalar(select(SalesTask).where(
        SalesTask.lead_id == request.lead_id,
        SalesTask.agent_source_type == request.source_type.value,
        SalesTask.agent_source_id == request.source_id,
        SalesTask.agent_source_action_id == request.source_action_id,
    ).limit(1))
    if prior_source is not None:
        return await _persist_attempt(session, request, fingerprint=fingerprint, key=key, status=TaskExecutionStatus.ALREADY_EXISTS, reason="This source action already created an internal task.", task_id=prior_source.id)
    try:
        _, action, priority, prep_items = await _load_source_action(
            session, request, context, fingerprint, catalog,
            validated_supervisor_fingerprint=validated_supervisor_fingerprint,
        )
    except StaleContextError:
        return await _persist_attempt(session, request, fingerprint=fingerprint, key=key, status=TaskExecutionStatus.STALE_CONTEXT, reason="The source artifact or request does not match current deterministic context.")
    except (ValueError, AIValidationError):
        return await _persist_attempt(session, request, fingerprint=fingerprint, key=key, status=TaskExecutionStatus.INVALID_REQUEST, reason="The source artifact, action, or evidence references failed server validation.")

    lead_status = getattr(lead.status, "value", lead.status)
    sales_status = getattr(lead.sales_status, "value", lead.sales_status)
    if lead_status == LeadStatus.FAILED.value or sales_status in {"WON", "LOST"}:
        return await _persist_attempt(session, request, fingerprint=fingerprint, key=key, status=TaskExecutionStatus.DENIED, reason="The lead is not eligible for automated task creation.")

    task_type = ACTION_TASK[request.action_type]
    prior_source = await session.scalar(select(SalesTask).where(
        SalesTask.lead_id == request.lead_id,
        SalesTask.agent_source_type == request.source_type.value,
        SalesTask.agent_source_id == request.source_id,
        SalesTask.agent_source_action_id == request.source_action_id,
    ).limit(1))
    active_equivalent = await session.scalar(select(SalesTask).where(
        SalesTask.lead_id == request.lead_id,
        SalesTask.task_type == task_type,
        SalesTask.status.in_((SalesTaskStatus.OPEN, SalesTaskStatus.IN_PROGRESS)),
    ).limit(1))
    if prior_source is not None or active_equivalent is not None:
        existing = prior_source or active_equivalent
        return await _persist_attempt(session, request, fingerprint=fingerprint, key=key, status=TaskExecutionStatus.ALREADY_EXISTS, reason="An equivalent source action or active task already exists.", task_id=existing.id)

    policy_request = PolicyEvaluationRequest(
        action_type=request.action_type.value,
        requested_channel=_requested_channel(request.action_type, lead),
        evidence_ids=request.evidence_ids,
        opportunity_ids=tuple(getattr(action, "opportunity_ids", ())),
        service_ids=tuple(getattr(action, "service_ids", ())),
        source_fingerprint=fingerprint,
    )
    try:
        policy = await evaluate_policy(session, request.lead_id, policy_request)
    except Exception:
        await session.rollback()
        return await _persist_attempt(session, request, fingerprint=fingerprint, key=key, status=TaskExecutionStatus.FAILED, reason="Policy evaluation failed; no task was created.")
    if policy.decision == PolicyDecision.DENY:
        if "DUPLICATE_ACTIVE_TASK" in policy.reason:
            duplicate = await session.scalar(select(SalesTask).where(
                SalesTask.lead_id == request.lead_id,
                SalesTask.task_type == task_type,
                SalesTask.status.in_((SalesTaskStatus.OPEN, SalesTaskStatus.IN_PROGRESS)),
            ).limit(1))
            if duplicate is not None:
                return await _persist_attempt(session, request, fingerprint=fingerprint, key=key, status=TaskExecutionStatus.ALREADY_EXISTS, reason="An equivalent active task already exists.", policy_id=policy.evaluation_id, policy_decision=policy.decision, task_id=duplicate.id)
        return await _persist_attempt(session, request, fingerprint=fingerprint, key=key, status=TaskExecutionStatus.DENIED, reason=policy.reason, policy_id=policy.evaluation_id, policy_decision=policy.decision)
    if policy.decision == PolicyDecision.REQUIRE_HUMAN_APPROVAL:
        return await _persist_attempt(session, request, fingerprint=fingerprint, key=key, status=TaskExecutionStatus.REQUIRES_HUMAN_APPROVAL, reason=policy.reason, policy_id=policy.evaluation_id, policy_decision=policy.decision)

    try:
        locked_lead = await session.scalar(select(Lead).where(Lead.id == request.lead_id).with_for_update())
        if locked_lead is None:
            raise LookupError("Lead not found")
        _, locked_fingerprint, locked_catalog = await AISalesContextBuilder().build(session, request.lead_id)
        if locked_fingerprint != fingerprint:
            return await _persist_attempt(session, request, fingerprint=locked_fingerprint, key=key, status=TaskExecutionStatus.STALE_CONTEXT, reason="Lead context changed after policy evaluation.", policy_id=policy.evaluation_id, policy_decision=policy.decision)
        validate_context_references(set(request.evidence_ids), context, locked_catalog, request.lead_id)
        same_source = await session.scalar(select(SalesTask).where(
            SalesTask.lead_id == request.lead_id,
            SalesTask.agent_source_type == request.source_type.value,
            SalesTask.agent_source_id == request.source_id,
            SalesTask.agent_source_action_id == request.source_action_id,
        ).limit(1))
        active_equivalent = await session.scalar(select(SalesTask).where(
            SalesTask.lead_id == request.lead_id,
            SalesTask.task_type == task_type,
            SalesTask.status.in_((SalesTaskStatus.OPEN, SalesTaskStatus.IN_PROGRESS)),
        ).limit(1))
        existing = same_source or active_equivalent
        if existing is not None:
            return await _persist_attempt(session, request, fingerprint=fingerprint, key=key, status=TaskExecutionStatus.ALREADY_EXISTS, reason="An equivalent source action or active task already exists.", policy_id=policy.evaluation_id, policy_decision=policy.decision, task_id=existing.id)

        company = (locked_lead.company_name or "lead").strip()[:160]
        title = ACTION_TITLE[request.action_type].format(company=company)[:250]
        task_priority = getattr(locked_lead.priority, "value", locked_lead.priority)
        if request.source_type == TaskAgentSourceType.AI_SALES_SUPERVISOR:
            task_priority = locked_lead.priority
        elif priority is not None and getattr(priority, "value", priority) != task_priority:
            return await _persist_attempt(session, request, fingerprint=fingerprint, key=key, status=TaskExecutionStatus.INVALID_REQUEST, reason="The persisted action-plan priority is not current.", policy_id=policy.evaluation_id, policy_decision=policy.decision)
        due_at = _due_at(request.action_type, locked_lead, now)
        description = "Internal task created from a validated AI recommendation. No prospect communication is performed. Evidence references: " + ", ".join(sorted(set(request.evidence_ids)))
        task = await create_task(session, request.lead_id, SalesTaskCreate(
            task_type=task_type, title=title, description=description,
            due_at=due_at, priority=SalesPriority(task_priority),
        ), commit=False)
        task.created_by_agent = True
        task.agent_source_type = request.source_type.value
        task.agent_source_id = request.source_id
        task.agent_source_action_id = request.source_action_id
        task.agent_evidence_refs = sorted(set(request.evidence_ids))
        await session.flush()
        audit = AutonomousTaskExecution(
            lead_id=request.lead_id, action_type=request.action_type.value,
            source_type=request.source_type.value, source_id=request.source_id,
            source_action_id=request.source_action_id, task_id=task.id,
            policy_evaluation_id=policy.evaluation_id, policy_decision=policy.decision.value,
            context_fingerprint=fingerprint, evidence_refs=sorted(set(request.evidence_ids)),
            execution_status=TaskExecutionStatus.CREATED.value, idempotency_key=key,
            reason="Policy allowed a deterministic internal SalesTask creation.",
            requested_by=TASK_AGENT_REQUESTER, created_at=now, executed_at=now,
        )
        session.add(audit)
        await session.commit()
        await session.refresh(audit)
        return _audit_result(audit)
    except IntegrityError:
        await session.rollback()
        existing = await session.scalar(select(SalesTask).where(
            SalesTask.lead_id == request.lead_id,
            SalesTask.agent_source_type == request.source_type.value,
            SalesTask.agent_source_id == request.source_id,
            SalesTask.agent_source_action_id == request.source_action_id,
        ).limit(1))
        return await _persist_attempt(session, request, fingerprint=fingerprint, key=key, status=TaskExecutionStatus.ALREADY_EXISTS if existing else TaskExecutionStatus.FAILED, reason="A concurrent execution already created this task." if existing else "Task creation failed due to a data-integrity conflict.", policy_id=policy.evaluation_id, policy_decision=policy.decision, task_id=existing.id if existing else None)
    except Exception:
        await session.rollback()
        return await _persist_attempt(session, request, fingerprint=fingerprint, key=key, status=TaskExecutionStatus.FAILED, reason="The internal task could not be created.", policy_id=policy.evaluation_id, policy_decision=policy.decision)


async def task_execution_history(session: AsyncSession, *, lead_id: UUID | None = None, limit: int = 50):
    query = select(AutonomousTaskExecution)
    if lead_id is not None:
        query = query.where(AutonomousTaskExecution.lead_id == lead_id)
    rows = (await session.scalars(query.order_by(AutonomousTaskExecution.created_at.desc(), AutonomousTaskExecution.id.desc()).limit(limit))).all()
    return [_audit_result(row) for row in rows]


async def task_execution_by_id(session: AsyncSession, execution_id: UUID, *, lead_id: UUID | None = None):
    query = select(AutonomousTaskExecution).where(AutonomousTaskExecution.id == execution_id)
    if lead_id is not None:
        query = query.where(AutonomousTaskExecution.lead_id == lead_id)
    return await session.scalar(query)


async def execute_supervisor_run_tasks(session: AsyncSession) -> tuple[int, int, list[TaskExecutionResult]]:
    run = await session.scalar(select(AiSalesSupervisorRun).where(
        AiSalesSupervisorRun.scope == "PIPELINE", AiSalesSupervisorRun.status == "COMPLETED",
    ).order_by(AiSalesSupervisorRun.created_at.desc(), AiSalesSupervisorRun.id.desc()).limit(1))
    if run is None or run.result_json is None:
        return 0, 0, []
    _, _, current_pipeline_fingerprint = await build_pipeline_context(session)
    validated_run_fingerprint = run.context_fingerprint if run.context_fingerprint == current_pipeline_fingerprint else None
    result = SupervisorResult.model_validate(run.result_json)
    actionable = [finding for finding in result.findings if finding.action_type is not None]
    executions: list[TaskExecutionResult] = []
    for finding in actionable:
        request = TaskExecutionRequest(
            lead_id=finding.lead_id, action_type=finding.action_type,
            source_type=TaskAgentSourceType.AI_SALES_SUPERVISOR, source_id=run.id,
            source_action_id=finding.finding_id, context_fingerprint=run.context_fingerprint,
            evidence_ids=finding.evidence_ids, requested_by=TASK_AGENT_REQUESTER,
        )
        try:
            _, fingerprint, _ = await AISalesContextBuilder().build(session, finding.lead_id)
            body = request.model_copy(update={"context_fingerprint": fingerprint})
            executions.append(await execute_task_action(
                session, body, validated_supervisor_fingerprint=validated_run_fingerprint,
            ))
        except LookupError:
            continue
        except Exception:
            await session.rollback()
            executions.append(await _persist_attempt(
                session, request, fingerprint=run.context_fingerprint,
                key=_idempotency_key(request), status=TaskExecutionStatus.FAILED,
                reason="The Supervisor finding could not be validated for internal task execution.",
            ))
    return len(result.findings), len(actionable), executions
