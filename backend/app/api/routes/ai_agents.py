"""AI sales intelligence, copilot, action plans, proposals, agent policy, supervisor runs, agent tasks and AI outreach."""
import uuid

from app.database import (
    get_db,
)
from app.domain.ai_outreach_preparation import (
    OutreachPreparationOut,
    OutreachPreparationRequest,
)
from app.models import (
    Lead,
)
from app.schemas.agent_policy import (
    PolicyEvaluationHistory,
    PolicyEvaluationOut,
    PolicyEvaluationRequest,
)
from app.schemas.ai_outreach_execution import (
    EmptyActionRequest,
    OutreachApprovalOut,
)
from app.schemas.ai_outreach_execution import (
    OutreachExecutionHistory as AiOutreachExecutionHistory,
)
from app.schemas.ai_outreach_execution import (
    OutreachExecutionOut as AiOutreachExecutionOut,
)
from app.schemas.ai_proposal_intelligence import (
    ProposalIntelligenceHistoryOut,
    ProposalIntelligenceOut,
)
from app.schemas.ai_sales_action_planner import ActionPlanHistoryOut, ActionPlanOut
from app.schemas.ai_sales_copilot import (
    CopilotAsk,
    CopilotHistoryOut,
    CopilotInteractionOut,
)
from app.schemas.ai_sales_intelligence import (
    AISalesIntelligenceHistoryOut,
    AISalesIntelligenceOut,
)
from app.schemas.ai_sales_supervisor import (
    SupervisorRunHistory,
    SupervisorRunOut,
    SupervisorRunRequest,
)
from app.schemas.autonomous_task_agent import (
    SupervisorTaskExecutionSummary,
    TaskExecutionHistory,
    TaskExecutionRequest,
    TaskExecutionResult,
)
from app.services.agent_policy import evaluate_policy, policy_history
from app.services.ai_outreach_execution import (
    OutreachExecutionError,
    approval_by_preparation,
    approve_preparation,
    execute_approved_email,
    execution_by_id,
    execution_history_for_lead,
    execution_history_for_preparation,
)
from app.services.ai_outreach_preparation import (
    current_preparation,
    preparation_by_id,
    preparation_history,
    prepare_outreach,
)
from app.services.ai_proposal_intelligence import (
    current_proposal,
    generate_proposal,
    proposal_by_id,
    proposal_history,
)
from app.services.ai_sales_action_planner import (
    action_plan_by_id,
    action_plan_history,
    current_action_plan,
    generate_action_plan,
)
from app.services.ai_sales_copilot import ai_sales_copilot_history, ask_ai_sales_copilot
from app.services.ai_sales_intelligence import (
    ai_sales_intelligence_by_id,
    ai_sales_intelligence_history,
    current_ai_sales_intelligence,
    generate_ai_sales_intelligence,
)
from app.services.ai_sales_supervisor import (
    build_pipeline_context,
    current_supervisor_run,
    run_supervisor,
    run_to_output,
    supervisor_history,
    supervisor_run_by_id,
)
from app.services.autonomous_task_agent import (
    execute_supervisor_run_tasks,
    execute_task_action,
    task_execution_by_id,
    task_execution_history,
)
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
)
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()


def _ai_sales_intelligence_payload(artifact, stale: bool):
    current = artifact.status == "validated" and not stale
    return AISalesIntelligenceOut(
        id=artifact.id, lead_id=artifact.lead_id,
        context_version=artifact.context_version,
        contract_version=artifact.contract_version,
        prompt_version=artifact.prompt_version,
        provider=artifact.provider, model=artifact.model, status=artifact.status,
        source_fingerprint=artifact.source_fingerprint, is_stale=stale,
        is_current=current, generated_at=artifact.generated_at,
        created_at=artifact.created_at, generated=artifact.generated,
        evidence_catalog=tuple(artifact.evidence_catalog or ()),
        evidence_refs=tuple(artifact.evidence_refs or ()),
        usage_metadata=artifact.usage_metadata or {},
        error_code=artifact.error_code, error_message=artifact.error_message,
    )


def _ai_sales_copilot_payload(artifact, fingerprint: str) -> CopilotInteractionOut:
    return CopilotInteractionOut(
        id=artifact.id, lead_id=artifact.lead_id, question=artifact.question,
        intent=artifact.intent, context_version=artifact.context_version,
        contract_version=artifact.contract_version, prompt_version=artifact.prompt_version,
        provider=artifact.provider, model=artifact.model, status=artifact.status,
        source_fingerprint=artifact.source_fingerprint,
        is_stale=artifact.source_fingerprint != fingerprint,
        response=artifact.response,
        evidence_catalog=tuple(artifact.evidence_catalog or ()),
        usage_metadata=artifact.usage_metadata or {}, error_code=artifact.error_code,
        error_message=artifact.error_message, created_at=artifact.created_at,
    )


def _ai_sales_action_plan_payload(artifact, fingerprint: str) -> ActionPlanOut:
    stale = artifact.source_fingerprint != fingerprint
    return ActionPlanOut(
        id=artifact.id, lead_id=artifact.lead_id,
        contract_version=artifact.contract_version, prompt_version=artifact.prompt_version,
        context_version=artifact.context_version, provider=artifact.provider, model=artifact.model,
        status=artifact.status, source_fingerprint=artifact.source_fingerprint,
        is_stale=stale, is_current=not stale and artifact.status == "validated",
        action_plan=artifact.action_plan,
        evidence_catalog=tuple(artifact.evidence_catalog or ()),
        evidence_refs=tuple(artifact.evidence_refs or ()),
        usage_metadata=artifact.usage_metadata or {}, error_code=artifact.error_code,
        error_message=artifact.error_message, created_at=artifact.created_at,
        updated_at=artifact.updated_at,
    )


def _ai_proposal_intelligence_payload(artifact, fingerprint: str) -> ProposalIntelligenceOut:
    stale = artifact.source_fingerprint != fingerprint
    return ProposalIntelligenceOut(
        id=artifact.id, lead_id=artifact.lead_id,
        contract_version=artifact.contract_version, prompt_version=artifact.prompt_version,
        context_version=artifact.context_version, provider=artifact.provider, model=artifact.model,
        status=artifact.status, source_fingerprint=artifact.source_fingerprint,
        is_stale=stale, is_current=not stale and artifact.status == "validated",
        proposal_brief=artifact.proposal_brief,
        evidence_catalog=tuple(artifact.evidence_catalog or ()),
        evidence_refs=tuple(artifact.evidence_refs or ()),
        usage_metadata=artifact.usage_metadata or {}, error_code=artifact.error_code,
        error_message=artifact.error_message, created_at=artifact.created_at,
        updated_at=artifact.updated_at,
    )


def _raise_outreach_execution_error(error: OutreachExecutionError):
    raise HTTPException(error.status_code, detail={"code": error.code, "message": str(error)}) from None


@router.post("/api/leads/{lead_id}/ai-sales-intelligence", response_model=AISalesIntelligenceOut, status_code=201)
async def generate_lead_ai_sales_intelligence(lead_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    try:
        artifact = await generate_ai_sales_intelligence(db, lead_id)
    except LookupError as error:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": str(error)}) from None
    return _ai_sales_intelligence_payload(artifact, False)


@router.get("/api/leads/{lead_id}/ai-sales-intelligence", response_model=AISalesIntelligenceOut | None)
async def get_lead_ai_sales_intelligence(lead_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    try:
        artifact, stale = await current_ai_sales_intelligence(db, lead_id)
    except LookupError as error:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": str(error)}) from None
    return _ai_sales_intelligence_payload(artifact, stale) if artifact is not None else None


@router.get("/api/leads/{lead_id}/ai-sales-intelligence/history", response_model=AISalesIntelligenceHistoryOut)
async def get_lead_ai_sales_intelligence_history(
    lead_id: uuid.UUID, limit: int = Query(50, ge=1, le=100), db: AsyncSession = Depends(get_db),
):
    try:
        rows = await ai_sales_intelligence_history(db, lead_id, limit)
    except LookupError as error:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": str(error)}) from None
    return {"items": [_ai_sales_intelligence_payload(artifact, stale) for artifact, stale in rows]}


@router.get("/api/leads/{lead_id}/ai-sales-intelligence/history/{artifact_id}", response_model=AISalesIntelligenceOut)
async def get_lead_ai_sales_intelligence_artifact(
    lead_id: uuid.UUID, artifact_id: uuid.UUID, db: AsyncSession = Depends(get_db),
):
    try:
        artifact, stale = await ai_sales_intelligence_by_id(db, lead_id, artifact_id)
    except LookupError as error:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": str(error)}) from None
    if artifact is None:
        raise HTTPException(404, detail={"code": "AI_SALES_INTELLIGENCE_NOT_FOUND", "message": "AI sales intelligence artifact not found for this lead"})
    return _ai_sales_intelligence_payload(artifact, stale)


@router.post("/api/leads/{lead_id}/ai-copilot", response_model=CopilotInteractionOut, status_code=201)
async def ask_lead_ai_copilot(
    lead_id: uuid.UUID, request: CopilotAsk, db: AsyncSession = Depends(get_db),
):
    try:
        artifact = await ask_ai_sales_copilot(db, lead_id, request.question)
        return _ai_sales_copilot_payload(artifact, artifact.source_fingerprint)
    except LookupError as error:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": str(error)}) from None


@router.get("/api/leads/{lead_id}/ai-copilot/history", response_model=CopilotHistoryOut)
async def get_lead_ai_copilot_history(
    lead_id: uuid.UUID, limit: int = Query(50, ge=1, le=100), db: AsyncSession = Depends(get_db),
):
    try:
        artifacts, fingerprint = await ai_sales_copilot_history(db, lead_id, limit)
    except LookupError as error:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": str(error)}) from None
    return {"items": [_ai_sales_copilot_payload(item, fingerprint) for item in artifacts]}


@router.post("/api/leads/{lead_id}/ai-sales-action-plan", response_model=ActionPlanOut, status_code=201)
async def create_lead_ai_sales_action_plan(lead_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    try:
        artifact = await generate_action_plan(db, lead_id)
        return _ai_sales_action_plan_payload(artifact, artifact.source_fingerprint)
    except LookupError as error:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": str(error)}) from None


@router.get("/api/leads/{lead_id}/ai-sales-action-plan", response_model=ActionPlanOut | None)
async def get_lead_ai_sales_action_plan(lead_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    try:
        artifact, fingerprint = await current_action_plan(db, lead_id)
    except LookupError as error:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": str(error)}) from None
    return _ai_sales_action_plan_payload(artifact, fingerprint) if artifact else None


@router.get("/api/leads/{lead_id}/ai-sales-action-plan/history", response_model=ActionPlanHistoryOut)
async def get_lead_ai_sales_action_plan_history(
    lead_id: uuid.UUID, limit: int = Query(50, ge=1, le=100), db: AsyncSession = Depends(get_db),
):
    try:
        rows, fingerprint = await action_plan_history(db, lead_id, limit)
    except LookupError as error:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": str(error)}) from None
    return {"items": [_ai_sales_action_plan_payload(item, fingerprint) for item in rows]}


@router.get("/api/leads/{lead_id}/ai-sales-action-plan/history/{plan_id}", response_model=ActionPlanOut)
async def get_lead_ai_sales_action_plan_artifact(
    lead_id: uuid.UUID, plan_id: uuid.UUID, db: AsyncSession = Depends(get_db),
):
    try:
        artifact, fingerprint = await action_plan_by_id(db, lead_id, plan_id)
    except LookupError as error:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": str(error)}) from None
    if artifact is None:
        raise HTTPException(404, detail={"code": "AI_SALES_ACTION_PLAN_NOT_FOUND", "message": "Action plan not found for this lead"})
    return _ai_sales_action_plan_payload(artifact, fingerprint)


@router.post("/api/leads/{lead_id}/ai-proposal", response_model=ProposalIntelligenceOut, status_code=201)
async def create_lead_ai_proposal(lead_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    try:
        artifact = await generate_proposal(db, lead_id)
        _, current_fingerprint = await proposal_history(db, lead_id, limit=1)
        return _ai_proposal_intelligence_payload(artifact, current_fingerprint)
    except LookupError as error:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": str(error)}) from None


@router.get("/api/leads/{lead_id}/ai-proposal", response_model=ProposalIntelligenceOut | None)
async def get_lead_ai_proposal(lead_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    try:
        artifact, fingerprint = await current_proposal(db, lead_id)
    except LookupError as error:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": str(error)}) from None
    return _ai_proposal_intelligence_payload(artifact, fingerprint) if artifact else None


@router.get("/api/leads/{lead_id}/ai-proposal/history", response_model=ProposalIntelligenceHistoryOut)
async def get_lead_ai_proposal_history(
    lead_id: uuid.UUID, limit: int = Query(50, ge=1, le=100), db: AsyncSession = Depends(get_db),
):
    try:
        rows, fingerprint = await proposal_history(db, lead_id, limit)
    except LookupError as error:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": str(error)}) from None
    return {"items": [_ai_proposal_intelligence_payload(item, fingerprint) for item in rows]}


@router.get("/api/leads/{lead_id}/ai-proposal/history/{artifact_id}", response_model=ProposalIntelligenceOut)
async def get_lead_ai_proposal_artifact(
    lead_id: uuid.UUID, artifact_id: uuid.UUID, db: AsyncSession = Depends(get_db),
):
    try:
        artifact, fingerprint = await proposal_by_id(db, lead_id, artifact_id)
    except LookupError as error:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": str(error)}) from None
    if artifact is None:
        raise HTTPException(404, detail={"code": "AI_PROPOSAL_NOT_FOUND", "message": "Proposal intelligence artifact not found for this lead"})
    return _ai_proposal_intelligence_payload(artifact, fingerprint)


@router.post("/api/leads/{lead_id}/agent-policy/evaluate", response_model=PolicyEvaluationOut, status_code=201)
async def evaluate_lead_agent_policy(
    lead_id: uuid.UUID, body: PolicyEvaluationRequest, db: AsyncSession = Depends(get_db),
):
    try:
        return await evaluate_policy(db, lead_id, body)
    except LookupError as error:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": str(error)}) from None


@router.get("/api/leads/{lead_id}/agent-policy/history", response_model=PolicyEvaluationHistory)
async def get_lead_agent_policy_history(
    lead_id: uuid.UUID, limit: int = Query(50, ge=1, le=100), db: AsyncSession = Depends(get_db),
):
    try:
        return {"items": await policy_history(db, lead_id, limit)}
    except LookupError as error:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": str(error)}) from None


@router.post("/api/sales-supervisor/run", response_model=SupervisorRunOut, status_code=201)
async def create_sales_supervisor_run(
    body: SupervisorRunRequest, db: AsyncSession = Depends(get_db),
):
    run = await run_supervisor(db)
    _, _, current_fingerprint = await build_pipeline_context(db)
    return run_to_output(run, current_fingerprint)


@router.get("/api/sales-supervisor/current", response_model=SupervisorRunOut | None)
async def get_current_sales_supervisor_run(db: AsyncSession = Depends(get_db)):
    run, fingerprint = await current_supervisor_run(db)
    return run_to_output(run, fingerprint) if run else None


@router.get("/api/sales-supervisor/history", response_model=SupervisorRunHistory)
async def get_sales_supervisor_history(
    limit: int = Query(20, ge=1, le=50), db: AsyncSession = Depends(get_db),
):
    rows, fingerprint = await supervisor_history(db, limit)
    return {"items": [run_to_output(row, fingerprint) for row in rows]}


@router.get("/api/sales-supervisor/history/{run_id}", response_model=SupervisorRunOut)
async def get_sales_supervisor_run(run_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    run, fingerprint = await supervisor_run_by_id(db, run_id)
    if run is None:
        raise HTTPException(404, detail={"code": "SUPERVISOR_RUN_NOT_FOUND", "message": "Sales Supervisor run not found"})
    return run_to_output(run, fingerprint)


@router.post("/api/agent/tasks/execute", response_model=TaskExecutionResult, status_code=201)
async def execute_agent_task(body: TaskExecutionRequest, db: AsyncSession = Depends(get_db)):
    try:
        return await execute_task_action(db, body)
    except LookupError as error:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": str(error)}) from None


@router.get("/api/agent/tasks/executions", response_model=TaskExecutionHistory)
async def list_agent_task_executions(
    limit: int = Query(50, ge=1, le=100), db: AsyncSession = Depends(get_db),
):
    return {"items": await task_execution_history(db, limit=limit)}


@router.get("/api/agent/tasks/executions/{execution_id}", response_model=TaskExecutionResult)
async def get_agent_task_execution(execution_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    row = await task_execution_by_id(db, execution_id)
    if row is None:
        raise HTTPException(404, detail={"code": "TASK_EXECUTION_NOT_FOUND", "message": "Task Agent execution not found"})
    from app.services.autonomous_task_agent import _audit_result
    return _audit_result(row)


@router.post("/api/leads/{lead_id}/agent/tasks/execute", response_model=TaskExecutionResult, status_code=201)
async def execute_lead_agent_task(lead_id: uuid.UUID, body: TaskExecutionRequest, db: AsyncSession = Depends(get_db)):
    if body.lead_id != lead_id:
        raise HTTPException(422, detail={"code": "LEAD_SCOPE_MISMATCH", "message": "Request lead_id must match the path lead_id"})
    return await execute_agent_task(body, db)


@router.get("/api/leads/{lead_id}/agent/tasks/executions", response_model=TaskExecutionHistory)
async def list_lead_agent_task_executions(
    lead_id: uuid.UUID, limit: int = Query(50, ge=1, le=100), db: AsyncSession = Depends(get_db),
):
    if await db.get(Lead, lead_id) is None:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": "Lead not found"})
    return {"items": await task_execution_history(db, lead_id=lead_id, limit=limit)}


@router.post("/api/sales-supervisor/tasks/execute", response_model=SupervisorTaskExecutionSummary, status_code=201)
async def execute_sales_supervisor_tasks(db: AsyncSession = Depends(get_db)):
    total, eligible, results = await execute_supervisor_run_tasks(db)
    counts = {status: 0 for status in ("CREATED", "ALREADY_EXISTS", "DENIED", "REQUIRES_HUMAN_APPROVAL", "STALE_CONTEXT", "FAILED")}
    for result in results:
        if result.execution_status.value in counts:
            counts[result.execution_status.value] += 1
    not_processed = max(eligible - len(results), 0)
    counts["FAILED"] += not_processed
    return {
        "total_findings": total, "eligible": eligible,
        "created": counts["CREATED"], "already_exists": counts["ALREADY_EXISTS"],
        "denied": counts["DENIED"], "requires_human_approval": counts["REQUIRES_HUMAN_APPROVAL"],
        "stale_context": counts["STALE_CONTEXT"], "failed": counts["FAILED"],
        "execution_ids": tuple(result.execution_id for result in results),
    }


@router.post("/api/agent/outreach/prepare", response_model=OutreachPreparationOut, status_code=201)
async def prepare_agent_outreach(body: OutreachPreparationRequest, db: AsyncSession = Depends(get_db)):
    try:
        return await prepare_outreach(db, body)
    except LookupError as error:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": str(error)}) from None


@router.get("/api/agent/outreach/preparations/{lead_id}/current", response_model=OutreachPreparationOut | None)
async def get_current_agent_outreach_preparation(lead_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    try:
        return await current_preparation(db, lead_id)
    except LookupError as error:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": str(error)}) from None


@router.get("/api/agent/outreach/preparations/{lead_id}/history", response_model=list[OutreachPreparationOut])
async def get_agent_outreach_preparation_history(
    lead_id: uuid.UUID, limit: int = Query(50, ge=1, le=100), db: AsyncSession = Depends(get_db),
):
    try:
        return await preparation_history(db, lead_id, limit)
    except LookupError as error:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": str(error)}) from None


@router.get("/api/agent/outreach/preparations/{lead_id}/history/{preparation_id}", response_model=OutreachPreparationOut)
async def get_agent_outreach_preparation_detail(lead_id: uuid.UUID, preparation_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    if await db.get(Lead, lead_id) is None:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": "Lead not found"})
    row = await preparation_by_id(db, lead_id, preparation_id)
    if row is None:
        raise HTTPException(404, detail={"code": "OUTREACH_PREPARATION_NOT_FOUND", "message": "Outreach preparation not found"})
    return row


@router.post("/api/agent/outreach/preparations/{preparation_id}/approve", response_model=OutreachApprovalOut)
async def approve_agent_outreach_preparation(
    preparation_id: uuid.UUID, body: EmptyActionRequest, request: Request,
    db: AsyncSession = Depends(get_db),
):
    try:
        return await approve_preparation(
            db, preparation_id, approved_by_user_id=request.state.authenticated_user_id,
        )
    except OutreachExecutionError as error:
        _raise_outreach_execution_error(error)


@router.get("/api/agent/outreach/preparations/{preparation_id}/approval", response_model=OutreachApprovalOut | None)
async def get_agent_outreach_approval(preparation_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    try:
        return await approval_by_preparation(db, preparation_id)
    except OutreachExecutionError as error:
        _raise_outreach_execution_error(error)


@router.post("/api/agent/outreach/approvals/{approval_id}/execute", response_model=AiOutreachExecutionOut)
async def execute_agent_outreach_approval(
    approval_id: uuid.UUID, body: EmptyActionRequest, db: AsyncSession = Depends(get_db),
):
    try:
        return await execute_approved_email(db, approval_id)
    except OutreachExecutionError as error:
        _raise_outreach_execution_error(error)


@router.get("/api/agent/outreach/executions/{execution_id}", response_model=AiOutreachExecutionOut)
async def get_agent_outreach_execution(execution_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    row = await execution_by_id(db, execution_id)
    if row is None:
        raise HTTPException(404, detail={"code": "EXECUTION_NOT_FOUND", "message": "Outreach execution not found"})
    return row


@router.get("/api/agent/outreach/preparations/{preparation_id}/executions", response_model=AiOutreachExecutionHistory)
async def get_agent_outreach_preparation_executions(preparation_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    try:
        return {"items": await execution_history_for_preparation(db, preparation_id)}
    except OutreachExecutionError as error:
        _raise_outreach_execution_error(error)


@router.get("/api/agent/outreach/{lead_id}/executions", response_model=AiOutreachExecutionHistory)
async def get_agent_outreach_lead_executions(
    lead_id: uuid.UUID, limit: int = Query(50, ge=1, le=100), db: AsyncSession = Depends(get_db),
):
    try:
        return {"items": await execution_history_for_lead(db, lead_id, limit)}
    except OutreachExecutionError as error:
        _raise_outreach_execution_error(error)
