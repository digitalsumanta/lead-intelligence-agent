"""Database entities for leads, imports, and durable processing jobs."""
import enum
import uuid
from datetime import datetime, timezone
from sqlalchemy import CheckConstraint, Column, DateTime, Enum, ForeignKey, ForeignKeyConstraint, Index, Integer, String, Text, JSON, Float, Boolean, Uuid, Table, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def now_utc():
    """Return current UTC time."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


opportunity_evidence_links = Table(
    "opportunity_evidence_links", Base.metadata,
    Column("opportunity_id", Uuid(as_uuid=True), ForeignKey("opportunities.id", ondelete="CASCADE"), primary_key=True),
    Column("evidence_id", Uuid(as_uuid=True), ForeignKey("evidence.id", ondelete="CASCADE"), primary_key=True),
    Index("ix_opportunity_evidence_evidence", "evidence_id"),
)
recommendation_evidence_links = Table(
    "recommendation_evidence_links", Base.metadata,
    Column("recommendation_id", Uuid(as_uuid=True), ForeignKey("service_recommendations.id", ondelete="CASCADE"), primary_key=True),
    Column("evidence_id", Uuid(as_uuid=True), ForeignKey("evidence.id", ondelete="CASCADE"), primary_key=True),
    Index("ix_recommendation_evidence_evidence", "evidence_id"),
)
recommendation_opportunity_links = Table(
    "recommendation_opportunity_links", Base.metadata,
    Column("recommendation_id", Uuid(as_uuid=True), ForeignKey("service_recommendations.id", ondelete="CASCADE"), primary_key=True),
    Column("opportunity_id", Uuid(as_uuid=True), ForeignKey("opportunities.id", ondelete="CASCADE"), primary_key=True),
    Index("ix_recommendation_opportunity_opportunity", "opportunity_id"),
)
score_evidence_links = Table(
    "score_evidence_links", Base.metadata,
    Column("score_id", Uuid(as_uuid=True), ForeignKey("lead_scores.id", ondelete="CASCADE"), primary_key=True),
    Column("evidence_id", Uuid(as_uuid=True), ForeignKey("evidence.id", ondelete="CASCADE"), primary_key=True),
    Index("ix_score_evidence_evidence", "evidence_id"),
)
personalization_evidence_links = Table(
    "personalization_evidence_links", Base.metadata,
    Column("personalization_id", Uuid(as_uuid=True), ForeignKey("personalizations.id", ondelete="CASCADE"), primary_key=True),
    Column("evidence_id", Uuid(as_uuid=True), ForeignKey("evidence.id", ondelete="CASCADE"), primary_key=True),
    Index("ix_personalization_evidence_evidence", "evidence_id"),
)


class LeadStatus(str, enum.Enum):
    IMPORTED="IMPORTED"; VALIDATING="VALIDATING"; QUEUED="QUEUED"; PROCESSING="PROCESSING"; COMPLETED="COMPLETED"; FAILED="FAILED"; RETRYING="RETRYING"


class JobStatus(str, enum.Enum):
    QUEUED="QUEUED"; PROCESSING="PROCESSING"; COMPLETED="COMPLETED"; FAILED="FAILED"; RETRYING="RETRYING"


class SalesStatus(str, enum.Enum):
    NEW="NEW"; QUALIFIED="QUALIFIED"; CONTACTED="CONTACTED"; ENGAGED="ENGAGED"; PROPOSAL="PROPOSAL"; NEGOTIATION="NEGOTIATION"; WON="WON"; LOST="LOST"


class SalesPriority(str, enum.Enum):
    LOW="LOW"; MEDIUM="MEDIUM"; HIGH="HIGH"; URGENT="URGENT"


class SalesActivityType(str, enum.Enum):
    NOTE="NOTE"; CALL="CALL"; EMAIL="EMAIL"; WHATSAPP="WHATSAPP"; MEETING="MEETING"; STATUS_CHANGE="STATUS_CHANGE"; FOLLOW_UP="FOLLOW_UP"; PROPOSAL="PROPOSAL"; OTHER="OTHER"


class Lead(Base):
    __tablename__ = "leads"
    __table_args__ = (Index("ix_leads_email", "email"), Index("ix_leads_company_city", "company_name", "city"), Index("ix_leads_status_created", "status", "created_at"), Index("ix_leads_sales_status", "sales_status"), Index("ix_leads_priority", "priority"), Index("ix_leads_next_follow_up_at", "next_follow_up_at"))
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
    sales_status: Mapped[SalesStatus] = mapped_column(Enum(SalesStatus, name="salesstatus"), default=SalesStatus.NEW, server_default="NEW", nullable=False)
    priority: Mapped[SalesPriority] = mapped_column(Enum(SalesPriority, name="salespriority"), default=SalesPriority.MEDIUM, server_default="MEDIUM", nullable=False)
    assigned_to: Mapped[str | None] = mapped_column(String(120))
    next_follow_up_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lost_reason: Mapped[str | None] = mapped_column(String(1000))
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
    enrichment_runs: Mapped[list["EnrichmentRun"]] = relationship(back_populates="lead", cascade="all, delete-orphan")
    evidence: Mapped[list["Evidence"]] = relationship(back_populates="lead", cascade="all, delete-orphan")
    opportunities: Mapped[list["Opportunity"]] = relationship(back_populates="lead", cascade="all, delete-orphan")
    service_recommendation_records: Mapped[list["ServiceRecommendation"]] = relationship(back_populates="lead", cascade="all, delete-orphan")
    versioned_scores: Mapped[list["LeadScore"]] = relationship(back_populates="lead", cascade="all, delete-orphan")
    sales_activities: Mapped[list["SalesActivity"]] = relationship(back_populates="lead", cascade="all, delete-orphan")


class SalesActivity(Base):
    __tablename__ = "sales_activities"
    __table_args__ = (Index("ix_sales_activities_lead_occurred", "lead_id", "occurred_at"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lead_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False)
    activity_type: Mapped[SalesActivityType] = mapped_column(Enum(SalesActivityType, name="salesactivitytype"), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_utc)
    note: Mapped[str] = mapped_column(Text, nullable=False)
    lead: Mapped[Lead] = relationship(back_populates="sales_activities")


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
    __table_args__ = (Index("ix_provider_usage_provider_operation_created", "provider", "operation", "created_at"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    model: Mapped[str] = mapped_column(String(160), nullable=False)
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("lead_processing_jobs.id"))
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    operation: Mapped[str | None] = mapped_column(String(120))
    key_fingerprint: Mapped[str | None] = mapped_column(String(64))
    outcome: Mapped[str | None] = mapped_column(String(30))
    http_status: Mapped[int | None] = mapped_column(Integer)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    error_category: Mapped[str | None] = mapped_column(String(40))
    rate_limit_limit: Mapped[int | None] = mapped_column(Integer)
    rate_limit_remaining: Mapped[int | None] = mapped_column(Integer)
    rate_limit_reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    quota_units: Mapped[float | None] = mapped_column(Float)
    cost_units: Mapped[float | None] = mapped_column(Float)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class ApiKey(Base):
    __tablename__ = "api_keys"
    __table_args__ = (Index("ix_api_keys_provider_operation_available", "provider", "operation_scope", "enabled", "status", "cooldown_until", "quota_exhausted_until", "lease_expires_at"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"), nullable=False)
    operation_scope: Mapped[str | None] = mapped_column(String(120))
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    quota_exhausted_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProviderOperationState(Base):
    """Provider-wide per-operation cooldown/quota state when keys share quota."""

    __tablename__ = "provider_operation_state"

    provider: Mapped[str] = mapped_column(String(40), primary_key=True)
    operation: Mapped[str] = mapped_column(String(120), primary_key=True)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    quota_exhausted_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_utc, onupdate=now_utc)


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


class EnrichmentRun(Base):
    """Persist one versioned enrichment lifecycle for a lead."""

    __tablename__ = "enrichment_runs"
    __table_args__ = (
        Index("ix_enrichment_runs_lead_created", "lead_id", "created_at"),
        Index("ix_enrichment_runs_processing_job", "processing_job_id"),
        Index("uq_enrichment_runs_id_lead", "id", "lead_id", unique=True),
        Index(
            "uq_enrichment_run_active_job",
            "processing_job_id",
            unique=True,
            postgresql_where=text("status IN ('queued','running')"),
            sqlite_where=text("status IN ('queued','running')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lead_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False)
    processing_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("lead_processing_jobs.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="queued")
    requested_stages: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    completed_stages: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pipeline_version: Mapped[str] = mapped_column(String(100), nullable=False)
    failure_summary: Mapped[dict | None] = mapped_column(JSON)
    stage_states: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict, server_default=text("'{}'"))
    active_stage: Mapped[str | None] = mapped_column(String(40))
    stage_lease_token: Mapped[str | None] = mapped_column(String(36))
    stage_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)

    lead: Mapped[Lead] = relationship(back_populates="enrichment_runs")
    evidence: Mapped[list["Evidence"]] = relationship(back_populates="enrichment_run", cascade="all, delete-orphan")
    opportunities: Mapped[list["Opportunity"]] = relationship(back_populates="enrichment_run", cascade="all, delete-orphan")
    service_recommendations: Mapped[list["ServiceRecommendation"]] = relationship(back_populates="enrichment_run", cascade="all, delete-orphan")
    scores: Mapped[list["LeadScore"]] = relationship(back_populates="enrichment_run", cascade="all, delete-orphan")


class Evidence(Base):
    """Append-only source observation tied to its lead and enrichment run."""

    __tablename__ = "evidence"
    __table_args__ = (
        CheckConstraint("confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="ck_evidence_confidence_range"),
        Index("ix_evidence_lead", "lead_id"),
        Index("ix_evidence_enrichment_run", "enrichment_run_id"),
        Index("ix_evidence_type", "evidence_type"),
        Index("ix_evidence_provider_source", "provider_kind", "provider_name", "source_type"),
        Index("ix_evidence_observed_at", "observed_at"),
        Index("ix_evidence_fetched_at", "fetched_at"),
        Index("ix_evidence_provider_reference", "provider_name", "external_provider_reference"),
        Index("uq_evidence_run_content_hash", "enrichment_run_id", "content_hash", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lead_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False)
    enrichment_run_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("enrichment_runs.id", ondelete="CASCADE"), nullable=False)
    evidence_type: Mapped[str] = mapped_column(String(160), nullable=False)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False)
    provider_name: Mapped[str] = mapped_column(String(100), nullable=False)
    provider_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    provider_implementation: Mapped[str | None] = mapped_column(String(100))
    source_url: Mapped[str | None] = mapped_column(String(2048))
    external_provider_reference: Mapped[str | None] = mapped_column(String(512))
    observed_value: Mapped[object] = mapped_column(JSON, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confidence: Mapped[float | None] = mapped_column(Float)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_utc)

    lead: Mapped[Lead] = relationship(back_populates="evidence")
    enrichment_run: Mapped[EnrichmentRun] = relationship(back_populates="evidence")


class ServiceCatalogEntry(Base):
    """Configurable service catalog consumed by deterministic rules."""

    __tablename__ = "service_catalog"

    service_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(String(1000), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    version: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_utc, onupdate=now_utc)


class Opportunity(Base):
    """Deterministic or AI-origin opportunity retained per enrichment run."""

    __tablename__ = "opportunities"
    __table_args__ = (
        CheckConstraint("severity IN ('low','medium','high','critical')", name="ck_opportunity_severity"),
        CheckConstraint("status IN ('open','acknowledged','resolved','dismissed')", name="ck_opportunity_status"),
        CheckConstraint("origin IN ('deterministic','ai')", name="ck_opportunity_origin"),
        Index("ix_opportunities_lead_created", "lead_id", "created_at"),
        Index("ix_opportunities_run_category", "enrichment_run_id", "category"),
        Index("uq_opportunities_run_identity", "enrichment_run_id", "identity", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lead_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False)
    enrichment_run_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("enrichment_runs.id", ondelete="CASCADE"), nullable=False)
    identity: Mapped[str] = mapped_column(String(180), nullable=False)
    category: Mapped[str] = mapped_column(String(80), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    origin: Mapped[str] = mapped_column(String(20), nullable=False, default="deterministic")
    title: Mapped[str] = mapped_column(String(250), nullable=False)
    description: Mapped[str] = mapped_column(String(2000), nullable=False)
    evidence_references: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    rule_version: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_utc, onupdate=now_utc)

    lead: Mapped[Lead] = relationship(back_populates="opportunities")
    enrichment_run: Mapped[EnrichmentRun] = relationship(back_populates="opportunities")
    evidence_records: Mapped[list[Evidence]] = relationship(secondary=opportunity_evidence_links, lazy="selectin")


class ServiceRecommendation(Base):
    """Versioned deterministic service recommendation for one run and service."""

    __tablename__ = "service_recommendations"
    __table_args__ = (
        CheckConstraint("rank >= 1", name="ck_service_recommendation_rank"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_service_recommendation_confidence"),
        Index("ix_service_recommendations_lead_rank", "lead_id", "rank"),
        Index("uq_service_recommendations_run_service_rule", "enrichment_run_id", "service_id", "rule_version", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lead_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False)
    enrichment_run_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("enrichment_runs.id", ondelete="CASCADE"), nullable=False)
    service_id: Mapped[str] = mapped_column(String(80), ForeignKey("service_catalog.service_id"), nullable=False)
    service_name: Mapped[str] = mapped_column(String(160), nullable=False)
    service_description: Mapped[str] = mapped_column(String(1000), nullable=False)
    service_version: Mapped[str] = mapped_column(String(80), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    rationale: Mapped[str] = mapped_column(String(2000), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    opportunity_references: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    evidence_references: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    rule_version: Mapped[str] = mapped_column(String(100), nullable=False)
    ranking_score: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_utc)

    lead: Mapped[Lead] = relationship(back_populates="service_recommendation_records")
    enrichment_run: Mapped[EnrichmentRun] = relationship(back_populates="service_recommendations")
    service: Mapped[ServiceCatalogEntry] = relationship()
    evidence_records: Mapped[list[Evidence]] = relationship(secondary=recommendation_evidence_links, lazy="selectin")
    opportunity_records: Mapped[list[Opportunity]] = relationship(secondary=recommendation_opportunity_links, lazy="selectin")


class LeadScore(Base):
    """Immutable, versioned deterministic score tied to its evidence run."""

    __tablename__ = "lead_scores"
    __table_args__ = (
        CheckConstraint("score IS NULL OR (score >= 0 AND score <= 100)", name="ck_lead_score_range"),
        Index("ix_lead_scores_lead_calculated", "lead_id", "calculated_at"),
        Index("uq_lead_scores_run_version", "enrichment_run_id", "score_version", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lead_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False)
    enrichment_run_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("enrichment_runs.id", ondelete="CASCADE"), nullable=False)
    score: Mapped[int | None] = mapped_column(Integer)
    score_version: Mapped[str] = mapped_column(String(100), nullable=False)
    components: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    evidence_references: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_utc)

    lead: Mapped[Lead] = relationship(back_populates="versioned_scores")
    enrichment_run: Mapped[EnrichmentRun] = relationship(back_populates="scores")
    evidence_records: Mapped[list[Evidence]] = relationship(secondary=score_evidence_links, lazy="selectin")


class Personalization(Base):
    """Run-scoped validated or failed AI personalization artifact."""

    __tablename__ = "personalizations"
    __table_args__ = (
        Index("uq_personalizations_run_contract", "enrichment_run_id", "contract_version", unique=True),
        ForeignKeyConstraint(
            ["enrichment_run_id", "lead_id"], ["enrichment_runs.id", "enrichment_runs.lead_id"],
            name="fk_personalizations_run_lead",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lead_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False)
    enrichment_run_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    validation_result: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    generated: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_utc)
    evidence_records: Mapped[list[Evidence]] = relationship(secondary=personalization_evidence_links, lazy="selectin")
