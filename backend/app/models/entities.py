"""Database entities for leads, imports, and durable processing jobs."""
import enum
import uuid
from datetime import datetime, timezone
from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, String, Text, JSON, Float, Boolean, Uuid, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now_utc():
    """Return current UTC time."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class LeadStatus(str, enum.Enum):
    IMPORTED="IMPORTED"; VALIDATING="VALIDATING"; QUEUED="QUEUED"; PROCESSING="PROCESSING"; COMPLETED="COMPLETED"; FAILED="FAILED"; RETRYING="RETRYING"


class JobStatus(str, enum.Enum):
    QUEUED="QUEUED"; PROCESSING="PROCESSING"; COMPLETED="COMPLETED"; FAILED="FAILED"; RETRYING="RETRYING"


class Lead(Base):
    __tablename__ = "leads"
    __table_args__ = (Index("ix_leads_email", "email"), Index("ix_leads_company_city", "company_name", "city"), Index("ix_leads_status_created", "status", "created_at"))
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_name: Mapped[str] = mapped_column(String(250), nullable=False)
    website: Mapped[str | None] = mapped_column(String(1000))
    phone: Mapped[str | None] = mapped_column(String(80))
    email: Mapped[str | None] = mapped_column(String(320))
    address: Mapped[str | None] = mapped_column(String(500))
    city: Mapped[str | None] = mapped_column(String(120))
    state: Mapped[str | None] = mapped_column(String(120))
    country: Mapped[str | None] = mapped_column(String(120))
    industry: Mapped[str | None] = mapped_column(String(160))
    status: Mapped[LeadStatus] = mapped_column(Enum(LeadStatus), default=LeadStatus.IMPORTED, nullable=False)
    lead_score: Mapped[int | None] = mapped_column(Integer)
    score_factors: Mapped[dict | None] = mapped_column(JSON)
    ai_summary: Mapped[str | None] = mapped_column(Text)
    pain_points: Mapped[list | None] = mapped_column(JSON)
    keyword_opportunities: Mapped[list | None] = mapped_column(JSON)
    competitors: Mapped[list | None] = mapped_column(JSON)
    recommended_services: Mapped[list | None] = mapped_column(JSON)
    decision_maker: Mapped[str | None] = mapped_column(String(250))
    linkedin_url: Mapped[str | None] = mapped_column(String(1000))
    website_analysis: Mapped[dict | None] = mapped_column(JSON)
    ai_analysis: Mapped[dict | None] = mapped_column(JSON)
    sales_opening: Mapped[str | None] = mapped_column(Text)
    audit_video_script: Mapped[str | None] = mapped_column(Text)
    proposed_package: Mapped[str | None] = mapped_column(Text)
    confidence_score: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)


class ImportJob(Base):
    __tablename__ = "lead_import_jobs"
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    filename: Mapped[str] = mapped_column(String(255), default="upload.csv")
    total_rows: Mapped[int] = mapped_column(Integer, default=0)
    accepted_rows: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_rows: Mapped[int] = mapped_column(Integer, default=0)
    invalid_rows: Mapped[int] = mapped_column(Integer, default=0)
    invalid_details: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class LeadBatch(Base):
    __tablename__ = "lead_batches"
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    status: Mapped[str] = mapped_column(String(30), default="QUEUED")
    requested_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class ProcessingJob(Base):
    __tablename__ = "lead_processing_jobs"
    __table_args__ = (Index("ix_jobs_status_retry", "status", "next_retry_at"), Index("ix_jobs_lead_created", "lead_id", "created_at"), Index("uq_active_job_lead", "lead_id", unique=True, postgresql_where=text("status IN ('QUEUED','PROCESSING','RETRYING')"), sqlite_where=text("status IN ('QUEUED','PROCESSING','RETRYING')")))
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lead_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False)
    batch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("lead_batches.id"))
    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus), default=JobStatus.QUEUED, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=4)
    last_error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checkpoint: Mapped[str] = mapped_column(String(40), default="CREATED")
    provider: Mapped[str] = mapped_column(String(40), default="ollama")
    model: Mapped[str] = mapped_column(String(160), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)


class LeadEvent(Base):
    __tablename__ = "lead_events"
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lead_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"))
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("lead_processing_jobs.id", ondelete="SET NULL"))
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class ProviderUsage(Base):
    __tablename__ = "provider_usage"
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    model: Mapped[str] = mapped_column(String(160), nullable=False)
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("lead_processing_jobs.id"))
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE")
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentRun(Base):
    __tablename__ = "agent_runs"
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("lead_processing_jobs.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(30), default="RUNNING")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    worker_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_utc)
