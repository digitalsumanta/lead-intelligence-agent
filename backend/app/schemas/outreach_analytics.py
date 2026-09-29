"""Strict response contracts for aggregate outreach analytics."""
from datetime import date
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class OutreachAnalyticsChannel(str, Enum):
    EMAIL = "EMAIL"
    WHATSAPP = "WHATSAPP"
    MANUAL = "MANUAL"


class OutreachAnalyticsStatus(str, Enum):
    QUEUED = "QUEUED"
    SENDING = "SENDING"
    SENT = "SENT"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class OutreachAnalyticsOutcome(str, Enum):
    NO_RESPONSE = "NO_RESPONSE"
    CONNECTED = "CONNECTED"
    INTERESTED = "INTERESTED"
    NOT_INTERESTED = "NOT_INTERESTED"
    CALLBACK_REQUESTED = "CALLBACK_REQUESTED"
    MEETING_BOOKED = "MEETING_BOOKED"
    PROPOSAL_REQUESTED = "PROPOSAL_REQUESTED"
    PROPOSAL_SENT = "PROPOSAL_SENT"
    NEGOTIATION_STARTED = "NEGOTIATION_STARTED"
    WON = "WON"
    LOST = "LOST"
    INVALID_CONTACT = "INVALID_CONTACT"


class StrictAnalyticsModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class OutreachAnalyticsCohort(StrictAnalyticsModel):
    from_date: date | None = Field(alias="from")
    to: date | None
    channel: OutreachAnalyticsChannel | None
    date_field: str = "sent_at"
    timezone: str = "UTC"


class OutreachCountByChannel(StrictAnalyticsModel):
    channel: OutreachAnalyticsChannel
    count: int = Field(ge=0)


class OutreachCountByStatus(StrictAnalyticsModel):
    status: OutreachAnalyticsStatus
    count: int = Field(ge=0)


class OutreachOutcomeCount(StrictAnalyticsModel):
    outcome: OutreachAnalyticsOutcome
    activity_count: int = Field(ge=0)
    executions_with_outcome: int = Field(ge=0)
    leads_represented: int = Field(ge=0)


class OutreachOutcomeCoverage(StrictAnalyticsModel):
    eligible_sent_executions: int = Field(ge=0)
    executions_with_outcomes: int = Field(ge=0)
    executions_without_outcomes: int = Field(ge=0)


class OutreachAnalyticsResponse(StrictAnalyticsModel):
    cohort: OutreachAnalyticsCohort
    attempts_total: int = Field(ge=0)
    attempts_by_channel: list[OutreachCountByChannel]
    attempts_by_status: list[OutreachCountByStatus]
    recorded_outcomes_total: int = Field(ge=0)
    outcomes_by_type: list[OutreachOutcomeCount]
    executions_with_recorded_outcomes: int = Field(ge=0)
    leads_represented: int = Field(ge=0)
    leads_with_recorded_outcomes: int = Field(ge=0)
    outcome_coverage: OutreachOutcomeCoverage
    limitations: list[str]
