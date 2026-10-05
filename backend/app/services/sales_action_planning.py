import uuid
from typing import List
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.entities import SalesTask, SalesTaskType, SalesTaskStatus, SalesPriority, LeadIntelligenceOpportunity
from app.services.sales_tasks import create_task
from app.schemas.sales_tasks import SalesTaskCreate

async def synchronize_opportunity_tasks(session: AsyncSession, lead_id: uuid.UUID) -> List[SalesTask]:
    """
    Creates or updates SalesTasks deterministically based on existing Sales Opportunities.
    Provides idempotency and preserves evidence.
    """
    opportunities_stmt = select(LeadIntelligenceOpportunity).where(
        LeadIntelligenceOpportunity.lead_id == lead_id
    )
    result = await session.execute(opportunities_stmt)
    opportunities = result.scalars().all()

    existing_tasks_stmt = select(SalesTask).where(
        SalesTask.lead_id == lead_id,
        SalesTask.agent_source_type == 'SALES_OPPORTUNITY'
    )
    existing_tasks_result = await session.execute(existing_tasks_stmt)
    existing_tasks = list(existing_tasks_result.scalars().all())
    
    created_or_updated = []
    
    for opp in opportunities:
        if not opp.opportunity_type:
            continue

        # Use source_reference as the robust identity (fallback to opportunity_type if missing)
        # This prevents collisions if multiple opportunities share the same opportunity_type
        action_id = opp.source_reference or opp.opportunity_type

        # Find an ACTIVE task for this exact action_id
        active_task = None
        for t in existing_tasks:
            if t.agent_source_action_id == action_id and t.status in (SalesTaskStatus.OPEN, SalesTaskStatus.IN_PROGRESS):
                active_task = t
                break
        
        if active_task:
            # Task exists and is active.
            # We re-link it to the new opportunity UUID so the frontend can jump to the latest opportunity.
            if active_task.agent_source_id != opp.id:
                active_task.agent_source_id = opp.id
                
                # Ensure the evidence ref points to the new opportunity
                refs = list(active_task.agent_evidence_refs) if active_task.agent_evidence_refs else []
                new_ref = f"opportunity:{opp.id}"
                if new_ref not in refs:
                    refs.append(new_ref)
                active_task.agent_evidence_refs = refs
                
            created_or_updated.append(active_task)
            continue
            
        # Check if this EXACT opportunity ID is already linked to a historical task.
        # This happens if we already synced this specific opp, it was completed, and we sync again
        # without refreshing the opportunity.
        already_synced = False
        for t in existing_tasks:
            if t.agent_source_id == opp.id:
                already_synced = True
                break
                
        if already_synced:
            # We don't want to spawn a new task if the exact same opportunity was already completed.
            continue
            
        # Create new task
        task_type = SalesTaskType.OTHER
        if opp.recommended_action == "PREPARE_OUTREACH":
            task_type = SalesTaskType.REVIEW_OUTREACH

        priority = SalesPriority.MEDIUM
        if opp.priority == "HIGH":
            priority = SalesPriority.HIGH
        elif opp.priority == "URGENT":
            priority = SalesPriority.URGENT

        action_str = (opp.recommended_action or "Review").replace('_', ' ').title()
        title = f"{action_str}: {opp.title}"[:250]
        
        new_task = SalesTask(
            lead_id=lead_id,
            task_type=task_type,
            title=title,
            description=opp.description,
            status=SalesTaskStatus.OPEN,
            priority=priority,
            due_at=datetime.now(timezone.utc),
            agent_source_type='SALES_OPPORTUNITY',
            agent_source_id=opp.id,
            agent_source_action_id=action_id,
            agent_evidence_refs=[f"opportunity:{opp.id}"]
        )
        session.add(new_task)
        existing_tasks.append(new_task)
        created_or_updated.append(new_task)

    await session.commit()
    return created_or_updated
