import uuid
import time
import re
import asyncio
import logging
from typing import Optional, Dict, Any, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.exc import IntegrityError, OperationalError, DatabaseError

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


# Known transient operational patterns indicating retryable database/connection state
KNOWN_TRANSIENT_DB_PATTERNS = (
    "deadlock",
    "could not obtain lock",
    "lock timeout",
    "lock_not_available",
    "database is locked",
    "canceling statement due to lock timeout",
    "concurrent update",
    "server closed connection",
    "server closed the connection",
    "connection to server was lost",
    "connection lost",
    "connection reset",
    "could not connect to server",
    "terminating connection due to administrator command",
    "the database system is starting up",
    "too many connections",
    "connection refused",
    "network is unreachable",
    "broken pipe",
)

# Known transient connection patterns indicating temporary socket or network disconnects
KNOWN_TRANSIENT_CONNECTION_PATTERNS = (
    "connection reset",
    "connection refused",
    "connection aborted",
    "connection closed",
    "connection lost",
    "broken pipe",
    "network is unreachable",
    "remote end closed",
    "temporary failure",
    "timed out",
)


def sanitize_error_message(msg: str) -> str:
    """
    Sanitizes an exception message to prevent leaking sensitive information
    such as database connection strings, passwords, or authentication tokens.

    Supported secret formats include:
    - Database URIs containing username:password credentials (e.g. postgresql://user:pass@host/db)
    - Authorization headers and Bearer / Basic tokens (e.g. 'Authorization: Bearer token123', 'Bearer token123')
    - Key-value credentials separated by ':' or '=' (e.g. password=..., pwd: ..., token=..., api_key=..., api-key: ..., x-api-key: ...)
    - Quoted credential values (e.g. password="secret value", api_key='secret')

    Note: This sanitizer redacts standard, recognized credential patterns. It does
    not guarantee detection of arbitrary or unformatted proprietary secrets.
    """
    if not msg:
        return "Unknown error"

    # 1. Redact database / network URIs with credentials: scheme://user:pass@host[:port][/path]
    sanitized = re.sub(
        r"(?i)\b([a-z0-9\+\-]+)://([^:\s@]+):([^@\s]+)@",
        r"\1://\2:***@",
        msg
    )

    # 2. Redact Authorization headers and Bearer / Basic tokens
    sanitized = re.sub(
        r"(?i)\b(Authorization\s*:\s*)?(Bearer|Basic)\s+[A-Za-z0-9_\-\.\:\=\+\/]+",
        r"\1\2 ***",
        sanitized
    )

    # 3. Redact quoted key-value credentials: key="secret" or key='secret'
    sanitized = re.sub(
        r"(?i)\b(password|passwd|pwd|secret|client[_-]?secret|api[_-]?key|x[_-]api[_-]key|token|auth[_-]?token|access[_-]?token|refresh[_-]?token)(\s*[:=]\s*)[\"']([^\"']+)[\"']",
        r'\1\2"***"',
        sanitized
    )

    # 4. Redact unquoted key-value credentials: key=secret or key: secret
    sanitized = re.sub(
        r"(?i)\b(password|passwd|pwd|secret|client[_-]?secret|api[_-]?key|x[_-]api[_-]key|token|auth[_-]?token|access[_-]?token|refresh[_-]?token)(\s*[:=]\s*)([^\s,;&\"'\)\}\]]+)",
        r"\1\2***",
        sanitized
    )

    # 5. Redact Authorization header if not already caught (e.g. Authorization: raw_token)
    sanitized = re.sub(
        r"(?i)\b(Authorization\s*:\s*)(?!Bearer|Basic)([^\s,;&\"'\)\}\]]+)",
        r"\1***",
        sanitized
    )

    if len(sanitized) > 500:
        sanitized = sanitized[:497] + "..."
    return sanitized


def classify_orchestration_error(exc: Exception, stage: Optional[str]) -> tuple[str, bool, str]:
    """
    Classifies an orchestration failure into:
    - error_code: str (stable documented error code)
    - retryable: bool (distinguishes known transient conditions from permanent/validation/unknown failures)
    - safe_error_message: str (sanitized message safe for logs and API consumers)

    Classification Rules:
    1. TimeoutError / asyncio.TimeoutError:
       - Transient RPC or network timeouts -> TIMEOUT_ERROR, retryable=True.
    2. ConnectionError / ConnectionResetError / ConnectionRefusedError:
       - Transient network disconnections -> CONNECTION_ERROR, retryable=True.
    3. SQLAlchemy OperationalError:
       - Verified against KNOWN_TRANSIENT_DB_PATTERNS (deadlocks, lock timeouts, server drops).
       - If transient condition detected -> DATABASE_OPERATIONAL_ERROR, retryable=True.
       - Otherwise (e.g. syntax error, undefined table, permission denied, unverified operational error) ->
         DATABASE_OPERATIONAL_ERROR, retryable=False.
    4. SQLAlchemy IntegrityError:
       - Constraint / unique / foreign key violation -> DATABASE_INTEGRITY_ERROR, retryable=False.
    5. SQLAlchemy DatabaseError:
       - General database error -> DATABASE_ERROR, retryable=False.
    6. ValueError / TypeError / KeyError / RequestValidationError:
       - Input or schema validation failure -> DATA_VALIDATION_ERROR, retryable=False.
    7. Stage-based fallback for unclassified / unknown exceptions:
       - Unknown errors are conservative and NOT marked retryable.
       - Stage mapped to CONTACT_MATCH_ERROR, RECOMMENDATION_ERROR, TASK_SYNC_ERROR,
         FEEDBACK_COLLECTION_ERROR, or ORCHESTRATION_ERROR, with retryable=False.
    """
    safe_msg = sanitize_error_message(str(exc))

    # 1. Timeout errors
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "TIMEOUT_ERROR", True, safe_msg

    # 2. Connection errors (narrowed to verified transient connection subclasses or patterns)
    if isinstance(exc, (ConnectionResetError, ConnectionRefusedError, ConnectionAbortedError, BrokenPipeError)):
        return "CONNECTION_ERROR", True, safe_msg

    if isinstance(exc, ConnectionError):
        exc_str_lower = str(exc).lower()
        is_transient = any(pattern in exc_str_lower for pattern in KNOWN_TRANSIENT_CONNECTION_PATTERNS)
        return "CONNECTION_ERROR", is_transient, safe_msg

    # 3. Database OperationalError
    if isinstance(exc, OperationalError):
        exc_str_lower = str(exc).lower()
        is_transient = any(pattern in exc_str_lower for pattern in KNOWN_TRANSIENT_DB_PATTERNS)
        return "DATABASE_OPERATIONAL_ERROR", is_transient, safe_msg

    # 4. Database IntegrityError
    if isinstance(exc, IntegrityError):
        return "DATABASE_INTEGRITY_ERROR", False, safe_msg

    # 5. General DatabaseError
    if isinstance(exc, DatabaseError):
        return "DATABASE_ERROR", False, safe_msg

    # 6. Data / validation errors
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return "DATA_VALIDATION_ERROR", False, safe_msg

    # 7. Stage-based fallback
    if stage == "CONTACT_MATCHING":
        return "CONTACT_MATCH_ERROR", False, safe_msg
    elif stage == "RECOMMENDATION_GENERATION":
        return "RECOMMENDATION_ERROR", False, safe_msg
    elif stage == "TASK_SYNCHRONIZATION":
        return "TASK_SYNC_ERROR", False, safe_msg
    elif stage == "FEEDBACK_COLLECTION":
        return "FEEDBACK_COLLECTION_ERROR", False, safe_msg

    return "ORCHESTRATION_ERROR", False, safe_msg


async def run_lead_intelligence_orchestration(
    session: AsyncSession,
    lead_id: uuid.UUID
) -> Optional[LeadIntelligenceOrchestrationResponse]:
    """
    Coordinates the existing deterministic intelligence and sales services without duplicating logic:
    1. Loads Lead
    2. Identifies existing Opportunities
    3. Matches Contacts via existing service (CONTACT_MATCHING)
    4. Generates Recommendation via existing service (RECOMMENDATION_GENERATION)
    5. Synchronizes SalesTasks via existing bridge (TASK_SYNCHRONIZATION)
    6. Collects existing Feedback via existing service (FEEDBACK_COLLECTION, for all opportunities)
    7. Produces deterministic decision trace, failure diagnostics, execution timings, and structured evidence.
    """
    orch_start = time.perf_counter()

    # 1. Load lead
    lead = await session.get(Lead, lead_id)
    if not lead:
        return None

    lead_id_val = lead.id
    company_name = lead.company_name or str(lead_id)

    # 2. Identify existing opportunities
    opp_stmt = select(LeadIntelligenceOpportunity).where(
        LeadIntelligenceOpportunity.lead_id == lead_id
    ).order_by(LeadIntelligenceOpportunity.score.desc())
    opp_res = await session.execute(opp_stmt)
    opportunities = list(opp_res.scalars().all())

    if not opportunities:
        total_duration_ms = round((time.perf_counter() - orch_start) * 1000, 2)
        return LeadIntelligenceOrchestrationResponse(
            lead_id=lead_id_val,
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
                f"lead:{lead_id_val}",
                "status:NO_OPPORTUNITY",
                f"duration_ms:{total_duration_ms}",
            ],
            evidence=[{
                "lead_id": str(lead_id_val),
                "company_name": company_name,
                "message": "No sales opportunities found for lead",
            }],
            duration_ms=total_duration_ms,
        )

    # Pre-extract opportunity scalar data to ensure transaction rollback resilience
    opp_data_list = [
        {
            "id": opp.id,
            "title": opp.title,
            "opportunity_type": opp.opportunity_type,
            "score": opp.score,
            "priority": opp.priority,
        }
        for opp in opportunities
    ]

    # 3. Process each opportunity
    opp_results: List[OrchestrationOpportunityResult] = []
    global_evidence: List[Dict[str, Any]] = []
    global_trace: List[str] = [f"lead:{lead_id_val}"]

    tasks_created = 0
    tasks_reused = 0
    no_contact_count = 0
    no_recommendation_count = 0
    failed_count = 0

    for opp in opp_data_list:
        opp_start = time.perf_counter()
        opp_id = opp["id"]
        opp_title = opp["title"]
        opp_type = opp["opportunity_type"]
        opp_score = opp["score"]
        opp_priority = opp["priority"]

        opp_trace: List[str] = [
            f"opportunity:{opp_id}",
            f"opportunity_type:{opp_type}",
            f"score:{opp_score}",
        ]
        matched_count = 0
        recommended_contact = None
        rec = None
        task_dict = None
        task_info = None
        stage_durations_ms: Dict[str, float] = {}
        current_stage: Optional[str] = None
        stage_start = time.perf_counter()

        try:
            # Stage A: CONTACT_MATCHING
            current_stage = "CONTACT_MATCHING"
            stage_start = time.perf_counter()
            matched_data = await get_matched_contacts_for_opportunity(session, opp_id)
            stage_durations_ms["CONTACT_MATCHING"] = round((time.perf_counter() - stage_start) * 1000, 2)
            contacts = matched_data.get("contacts", []) if matched_data else []
            matched_count = len(contacts)

            if not contacts:
                opp_status = "NO_CONTACT"
                opp_trace.append("status:NO_CONTACT")

                # Stage D: FEEDBACK_COLLECTION (collected for every opportunity)
                current_stage = "FEEDBACK_COLLECTION"
                stage_start = time.perf_counter()
                feedback_res = await get_opportunity_sales_feedback(session, opp_id)
                stage_durations_ms["FEEDBACK_COLLECTION"] = round((time.perf_counter() - stage_start) * 1000, 2)
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

                opp_duration_ms = round((time.perf_counter() - opp_start) * 1000, 2)
                opp_trace.append(f"duration_ms:{opp_duration_ms}")
                no_contact_count += 1
                opp_result = OrchestrationOpportunityResult(
                    opportunity_id=opp_id,
                    opportunity_title=opp_title,
                    opportunity_type=opp_type,
                    score=opp_score,
                    priority=opp_priority,
                    status=opp_status,
                    matched_contacts_count=0,
                    feedback=feedback_dict,
                    decision_trace=opp_trace,
                    failed_phase=None,
                    error_code=None,
                    retryable=None,
                    duration_ms=opp_duration_ms,
                    stage_durations_ms=stage_durations_ms,
                )
                opp_results.append(opp_result)
                global_trace.extend(opp_trace)
                global_evidence.append({
                    "opportunity_id": str(opp_id),
                    "title": opp_title,
                    "status": opp_status,
                    "feedback_executions": feedback_res.total_executions if feedback_res else 0,
                    "duration_ms": opp_duration_ms,
                })
                continue

            # Stage B: RECOMMENDATION_GENERATION
            current_stage = "RECOMMENDATION_GENERATION"
            stage_start = time.perf_counter()
            rec = await get_contact_recommendation_for_opportunity(session, opp_id)
            stage_durations_ms["RECOMMENDATION_GENERATION"] = round((time.perf_counter() - stage_start) * 1000, 2)
            recommended_contact = rec.get("recommended_contact") if rec else None
            raw_action = rec.get("recommended_action") if rec else None
            rec_action = raw_action.value if hasattr(raw_action, "value") else (str(raw_action) if raw_action else None)

            if not rec or not recommended_contact or rec_action in (None, "NO_ACTION", "RecommendedSalesAction.NO_ACTION"):
                opp_status = "NO_RECOMMENDATION"
                opp_trace.append("status:NO_RECOMMENDATION")

                # Stage D: FEEDBACK_COLLECTION (collected for every opportunity)
                current_stage = "FEEDBACK_COLLECTION"
                stage_start = time.perf_counter()
                feedback_res = await get_opportunity_sales_feedback(session, opp_id)
                stage_durations_ms["FEEDBACK_COLLECTION"] = round((time.perf_counter() - stage_start) * 1000, 2)
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

                opp_duration_ms = round((time.perf_counter() - opp_start) * 1000, 2)
                opp_trace.append(f"duration_ms:{opp_duration_ms}")
                no_recommendation_count += 1
                opp_result = OrchestrationOpportunityResult(
                    opportunity_id=opp_id,
                    opportunity_title=opp_title,
                    opportunity_type=opp_type,
                    score=opp_score,
                    priority=opp_priority,
                    status=opp_status,
                    matched_contacts_count=matched_count,
                    recommendation=rec,
                    feedback=feedback_dict,
                    decision_trace=opp_trace,
                    failed_phase=None,
                    error_code=None,
                    retryable=None,
                    duration_ms=opp_duration_ms,
                    stage_durations_ms=stage_durations_ms,
                )
                opp_results.append(opp_result)
                global_trace.extend(opp_trace)
                global_evidence.append({
                    "opportunity_id": str(opp_id),
                    "title": opp_title,
                    "status": opp_status,
                    "matched_contacts_count": matched_count,
                    "feedback_executions": feedback_res.total_executions if feedback_res else 0,
                    "duration_ms": opp_duration_ms,
                })
                continue

            contact_id = recommended_contact.get("contact_id")
            opp_trace.append(f"contact:{contact_id}")
            opp_trace.append(f"action:{rec_action}")
            opp_trace.append(f"recommendation_score:{rec.get('recommendation_score')}")

            # Stage C: TASK_SYNCHRONIZATION
            current_stage = "TASK_SYNCHRONIZATION"
            stage_start = time.perf_counter()
            task_info = await synchronize_recommended_contact_task(session, opp_id)
            stage_durations_ms["TASK_SYNCHRONIZATION"] = round((time.perf_counter() - stage_start) * 1000, 2)

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

            # Stage D: FEEDBACK_COLLECTION
            current_stage = "FEEDBACK_COLLECTION"
            stage_start = time.perf_counter()
            feedback_res = await get_opportunity_sales_feedback(session, opp_id)
            stage_durations_ms["FEEDBACK_COLLECTION"] = round((time.perf_counter() - stage_start) * 1000, 2)
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

            opp_duration_ms = round((time.perf_counter() - opp_start) * 1000, 2)
            opp_trace.append(f"duration_ms:{opp_duration_ms}")

            opp_result = OrchestrationOpportunityResult(
                opportunity_id=opp_id,
                opportunity_title=opp_title,
                opportunity_type=opp_type,
                score=opp_score,
                priority=opp_priority,
                status=opp_status,
                matched_contacts_count=matched_count,
                recommended_contact=recommended_contact,
                recommendation=rec,
                sales_task=task_dict,
                feedback=feedback_dict,
                decision_trace=opp_trace,
                failed_phase=None,
                error_code=None,
                retryable=None,
                duration_ms=opp_duration_ms,
                stage_durations_ms=stage_durations_ms,
            )
            opp_results.append(opp_result)
            global_trace.extend(opp_trace)
            global_evidence.append({
                "opportunity_id": str(opp_id),
                "title": opp_title,
                "status": opp_status,
                "recommended_contact_id": str(contact_id) if contact_id else None,
                "recommended_action": rec_action,
                "task_id": str(task_info["task_id"]) if (task_info and task_info.get("task_id")) else None,
                "feedback_executions": feedback_res.total_executions if feedback_res else 0,
                "duration_ms": opp_duration_ms,
            })

        except Exception as exc:
            failed_count += 1
            if current_stage and current_stage not in stage_durations_ms:
                stage_durations_ms[current_stage] = round((time.perf_counter() - stage_start) * 1000, 2)

            opp_duration_ms = round((time.perf_counter() - opp_start) * 1000, 2)
            failed_phase = current_stage or "ORCHESTRATION"
            error_code, retryable, safe_error = classify_orchestration_error(exc, current_stage)

            logger.error(
                "Error orchestrating opportunity %s in stage %s [code=%s, retryable=%s]: %s",
                opp_id,
                failed_phase,
                error_code,
                retryable,
                safe_error,
            )

            opp_trace.append(f"failed_phase:{failed_phase}")
            opp_trace.append(f"error_code:{error_code}")
            opp_trace.append(f"retryable:{retryable}")
            opp_trace.append(f"error:{safe_error}")
            opp_trace.append(f"duration_ms:{opp_duration_ms}")

            opp_result = OrchestrationOpportunityResult(
                opportunity_id=opp_id,
                opportunity_title=opp_title,
                opportunity_type=opp_type,
                score=opp_score,
                priority=opp_priority,
                status="FAILED",
                matched_contacts_count=matched_count,
                recommended_contact=recommended_contact,
                recommendation=rec,
                sales_task=task_dict,
                decision_trace=opp_trace,
                error=safe_error,
                failed_phase=failed_phase,
                error_code=error_code,
                retryable=retryable,
                duration_ms=opp_duration_ms,
                stage_durations_ms=stage_durations_ms,
            )
            opp_results.append(opp_result)
            global_trace.extend(opp_trace)
            global_evidence.append({
                "opportunity_id": str(opp_id),
                "title": opp_title,
                "status": "FAILED",
                "task_id": str(task_info["task_id"]) if (task_info and task_info.get("task_id")) else None,
                "failed_phase": failed_phase,
                "error_code": error_code,
                "retryable": retryable,
                "error": safe_error,
                "duration_ms": opp_duration_ms,
            })

    # 4. Overall status aggregation
    total_opps = len(opp_data_list)
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

    total_duration_ms = round((time.perf_counter() - orch_start) * 1000, 2)
    global_trace.append(f"overall_status:{overall_status}")
    global_trace.append(f"duration_ms:{total_duration_ms}")

    return LeadIntelligenceOrchestrationResponse(
        lead_id=lead_id_val,
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
        duration_ms=total_duration_ms,
    )
