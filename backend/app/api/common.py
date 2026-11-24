"""Response helpers shared by several route modules."""
from app.schemas.sales import SalesActivityOut
from app.services.sales_tasks import (
    suggested_task_type,
)


def sales_activity_payload(activity):
    suggestion = suggested_task_type(activity.outcome) if activity.outcome else None
    return SalesActivityOut.model_validate({"id": str(activity.id), "lead_id": str(activity.lead_id), "activity_type": activity.activity_type, "occurred_at": activity.occurred_at, "note": activity.note, "outcome": activity.outcome, "suggested_task_type": suggestion.value if suggestion else None, "old_sales_status": activity.old_sales_status, "new_sales_status": activity.new_sales_status, "outreach_execution_id": str(activity.outreach_execution_id) if activity.outreach_execution_id else None})


def job_payload(job):
    """Serialize job fields safely."""
    safe_error = "Processing failed; details are available in server logs" if job.last_error else None
    return {"id": str(job.id), "lead_id": str(job.lead_id), "status": job.status.value, "attempt_count": job.attempt_count, "max_attempts": job.max_attempts, "last_error": safe_error, "started_at": job.started_at, "completed_at": job.completed_at, "next_retry_at": job.next_retry_at, "checkpoint": job.checkpoint, "provider": job.provider, "model": job.model, "created_at": job.created_at, "updated_at": job.updated_at}
