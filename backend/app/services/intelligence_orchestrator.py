import uuid
import logging
from typing import Optional, Dict, Any, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.models.entities import Lead, LeadIntelligenceOpportunity
from app.services.opportunity_contact_matching import get_matched_contacts_for_opportunity
from app.services.contact_recommendation import get_contact_recommendation_for_opportunity
from app.services.contact_recommendation_execution import synchronize_recommended_contact_task
from app.services.sales_feedback import get_opportunity_sales_feedback
from app.schemas.intelligence_orchestrator import (
    OrchestrationOpportunityResult,
    OrchestrationSummary,
    LeadIntelligenceOrchestrationResponse,
)

logger = logging.getLogger("lead_agent")


async def run_lead_intelligence_orchestration(
    session: AsyncSession,
    lead_id: uuid.UUID
) -> Optional[LeadIntelligenceOrchestrationResponse]:
    """
    Coordinates the existing deterministic intelligence and sales services without duplicating logic:
    1. Loads Lead
    2. Identifies existing Opportunities
    3. Matches Contacts via existing service
    4. Generates Recommendation via existing service
    5. Synchronizes SalesTasks via existing bridge
    6. Collects existing Feedback via existing service (for all opportunities)
    7. Produces deterministic decision trace and structured evidence
    """
    # 1. Load lead
    lead = await session.get(Lead, lead_id)
    if not lead:
        return None

    company_name = lead.company_name or str(lead_id)

    # 2. Identify existing opportunities
    opp_stmt = select(LeadIntelligenceOpportunity).where(
        LeadIntelligenceOpportunity.lead_id == lead_id
    ).order_by(LeadIntelligenceOpportunity.score.desc())
    opp_res = await session.execute(opp_stmt)
    opportunities = list(opp_res.scalars().all())

    if not opportunities:
        return LeadIntelligenceOrchestrationResponse(
            lead_id=lead.id,
            company_name=company_name,
            status="NO_OPPORTUNITY",
            opportunities=[],
            summary=OrchestrationSummary(
                total_opportunities=0,
                tasks_created=0,
                tasks_reused=0,
                no_contact_count=0,
                no_recommendation_count=0,
                failed_count=0,
            ),
            decision_trace=[
                f"lead:{lead.id}",
                "status:NO_OPPORTUNITY",
            ],
            evidence=[{
                "lead_id": str(lead.id),
                "company_name": company_name,
                "message": "No sales opportunities found for lead",
            }],
        )

    # 3. Process each opportunity
    opp_results: List[OrchestrationOpportunityResult] = []
    global_evidence: List[Dict[str, Any]] = []
    global_trace: List[str] = [f"lead:{lead.id}"]

    tasks_created = 0
    tasks_reused = 0
    no_contact_count = 0
    no_recommendation_count = 0
    failed_count = 0

    for opp in opportunities:
        opp_trace: List[str] = [
            f"opportunity:{opp.id}",
            f"opportunity_type:{opp.opportunity_type}",
            f"score:{opp.score}",
        ]
        matched_count = 0
        recommended_contact = None
        rec = None
        task_dict = None
        task_info = None

        try:
            # Step A: Match contacts
            matched_data = await get_matched_contacts_for_opportunity(session, opp.id)
            contacts = matched_data.get("contacts", []) if matched_data else []
            matched_count = len(contacts)

            if not contacts:
                opp_status = "NO_CONTACT"
                opp_trace.append("status:NO_CONTACT")

                # Step D: Sales Feedback (collected for every opportunity)
                feedback_res = await get_opportunity_sales_feedback(session, opp.id)
                feedback_dict: Optional[Dict[str, Any]] = None
                if feedback_res:
                    feedback_dict = {
                        "total_executions": feedback_res.total_executions,
                        "positive_count": feedback_res.positive_count,
                        "negative_count": feedback_res.negative_count,
                        "neutral_count": feedback_res.neutral_count,
                        "positive_rate": feedback_res.positive_rate,
                        "attribution_summary": feedback_res.attribution_summary,
                    }
                    opp_trace.append(f"feedback_executions:{feedback_res.total_executions}")
                    opp_trace.append(f"feedback_positive_rate:{feedback_res.positive_rate}%")

                no_contact_count += 1
                opp_result = OrchestrationOpportunityResult(
                    opportunity_id=opp.id,
                    opportunity_title=opp.title,
                    opportunity_type=opp.opportunity_type,
                    score=opp.score,
                    priority=opp.priority,
                    status=opp_status,
                    matched_contacts_count=0,
                    feedback=feedback_dict,
                    decision_trace=opp_trace,
                )
                opp_results.append(opp_result)
                global_trace.extend(opp_trace)
                global_evidence.append({
                    "opportunity_id": str(opp.id),
                    "title": opp.title,
                    "status": opp_status,
                    "feedback_executions": feedback_res.total_executions if feedback_res else 0,
                })
                continue

            # Step B: Contact Recommendation
            rec = await get_contact_recommendation_for_opportunity(session, opp.id)
            recommended_contact = rec.get("recommended_contact") if rec else None
            raw_action = rec.get("recommended_action") if rec else None
            rec_action = raw_action.value if hasattr(raw_action, "value") else (str(raw_action) if raw_action else None)

            if not rec or not recommended_contact or rec_action in (None, "NO_ACTION", "RecommendedSalesAction.NO_ACTION"):
                opp_status = "NO_RECOMMENDATION"
                opp_trace.append("status:NO_RECOMMENDATION")

                # Step D: Sales Feedback (collected for every opportunity)
                feedback_res = await get_opportunity_sales_feedback(session, opp.id)
                feedback_dict = None
                if feedback_res:
                    feedback_dict = {
                        "total_executions": feedback_res.total_executions,
                        "positive_count": feedback_res.positive_count,
                        "negative_count": feedback_res.negative_count,
                        "neutral_count": feedback_res.neutral_count,
                        "positive_rate": feedback_res.positive_rate,
                        "attribution_summary": feedback_res.attribution_summary,
                    }
                    opp_trace.append(f"feedback_executions:{feedback_res.total_executions}")
                    opp_trace.append(f"feedback_positive_rate:{feedback_res.positive_rate}%")

                no_recommendation_count += 1
                opp_result = OrchestrationOpportunityResult(
                    opportunity_id=opp.id,
                    opportunity_title=opp.title,
                    opportunity_type=opp.opportunity_type,
                    score=opp.score,
                    priority=opp.priority,
                    status=opp_status,
                    matched_contacts_count=matched_count,
                    recommendation=rec,
                    feedback=feedback_dict,
                    decision_trace=opp_trace,
                )
                opp_results.append(opp_result)
                global_trace.extend(opp_trace)
                global_evidence.append({
                    "opportunity_id": str(opp.id),
                    "title": opp.title,
                    "status": opp_status,
                    "matched_contacts_count": matched_count,
                    "feedback_executions": feedback_res.total_executions if feedback_res else 0,
                })
                continue

            contact_id = recommended_contact.get("contact_id")
            opp_trace.append(f"contact:{contact_id}")
            opp_trace.append(f"action:{rec_action}")
            opp_trace.append(f"recommendation_score:{rec.get('recommendation_score')}")

            # Step C: Sales Task Bridge
            task_info = await synchronize_recommended_contact_task(session, opp.id)
            if task_info and task_info.get("task_id"):
                task_dict = {
                    "task_id": str(task_info["task_id"]),
                    "created": task_info.get("created", False),
                    "reused": task_info.get("reused", False),
                    "task_type": task_info.get("task_type"),
                    "priority": task_info.get("priority"),
                }
                opp_trace.append(f"task:{task_info['task_id']}")
                if task_info.get("created"):
                    tasks_created += 1
                    opp_trace.append("task_state:CREATED")
                elif task_info.get("reused"):
                    tasks_reused += 1
                    opp_trace.append("task_state:REUSED")

            # Step D: Sales Feedback
            feedback_res = await get_opportunity_sales_feedback(session, opp.id)
            feedback_dict = None
            if feedback_res:
                feedback_dict = {
                    "total_executions": feedback_res.total_executions,
                    "positive_count": feedback_res.positive_count,
                    "negative_count": feedback_res.negative_count,
                    "neutral_count": feedback_res.neutral_count,
                    "positive_rate": feedback_res.positive_rate,
                    "attribution_summary": feedback_res.attribution_summary,
                }
                opp_trace.append(f"feedback_executions:{feedback_res.total_executions}")
                opp_trace.append(f"feedback_positive_rate:{feedback_res.positive_rate}%")

            if task_info and task_info.get("created"):
                opp_status = "TASK_CREATED"
            elif task_info and task_info.get("reused"):
                opp_status = "TASK_REUSED"
            else:
                opp_status = "COMPLETED"

            opp_result = OrchestrationOpportunityResult(
                opportunity_id=opp.id,
                opportunity_title=opp.title,
                opportunity_type=opp.opportunity_type,
                score=opp.score,
                priority=opp.priority,
                status=opp_status,
                matched_contacts_count=matched_count,
                recommended_contact=recommended_contact,
                recommendation=rec,
                sales_task=task_dict,
                feedback=feedback_dict,
                decision_trace=opp_trace,
            )
            opp_results.append(opp_result)
            global_trace.extend(opp_trace)
            global_evidence.append({
                "opportunity_id": str(opp.id),
                "title": opp.title,
                "status": opp_status,
                "recommended_contact_id": str(contact_id) if contact_id else None,
                "recommended_action": rec_action,
                "task_id": str(task_info["task_id"]) if (task_info and task_info.get("task_id")) else None,
                "feedback_executions": feedback_res.total_executions if feedback_res else 0,
            })

        except Exception as exc:
            logger.exception(f"Error orchestrating opportunity {opp.id}")
            failed_count += 1
            opp_trace.append(f"error:{str(exc)}")
            opp_result = OrchestrationOpportunityResult(
                opportunity_id=opp.id,
                opportunity_title=opp.title,
                opportunity_type=opp.opportunity_type,
                score=opp.score,
                priority=opp.priority,
                status="FAILED",
                matched_contacts_count=matched_count,
                recommended_contact=recommended_contact,
                recommendation=rec,
                sales_task=task_dict,
                decision_trace=opp_trace,
                error=str(exc),
            )
            opp_results.append(opp_result)
            global_trace.extend(opp_trace)
            global_evidence.append({
                "opportunity_id": str(opp.id),
                "title": opp.title,
                "status": "FAILED",
                "task_id": str(task_info["task_id"]) if (task_info and task_info.get("task_id")) else None,
                "error": str(exc),
            })

    # 4. Overall status aggregation
    total_opps = len(opportunities)
    if total_opps == 0:
        overall_status = "NO_OPPORTUNITY"
    elif failed_count == total_opps:
        overall_status = "FAILED"
    elif tasks_created == total_opps and failed_count == 0:
        overall_status = "TASK_CREATED"
    elif tasks_reused == total_opps and failed_count == 0:
        overall_status = "TASK_REUSED"
    elif no_contact_count == total_opps and failed_count == 0:
        overall_status = "NO_CONTACT"
    elif no_recommendation_count == total_opps and failed_count == 0:
        overall_status = "NO_RECOMMENDATION"
    elif all(r.status == "COMPLETED" for r in opp_results) and failed_count == 0:
        overall_status = "COMPLETED"
    else:
        overall_status = "PARTIAL"

    global_trace.append(f"overall_status:{overall_status}")

    return LeadIntelligenceOrchestrationResponse(
        lead_id=lead.id,
        company_name=company_name,
        status=overall_status,
        opportunities=opp_results,
        summary=OrchestrationSummary(
            total_opportunities=total_opps,
            tasks_created=tasks_created,
            tasks_reused=tasks_reused,
            no_contact_count=no_contact_count,
            no_recommendation_count=no_recommendation_count,
            failed_count=failed_count,
        ),
        decision_trace=global_trace,
        evidence=global_evidence,
    )
