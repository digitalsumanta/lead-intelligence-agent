const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
const TOKEN = process.env.NEXT_PUBLIC_API_TOKEN || "";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (TOKEN) headers.set("Authorization", `Bearer ${TOKEN}`);
  const response = await fetch(`${API}${path}`, {
    ...init,
    headers,
    cache: "no-store",
  });

  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    let code: string | undefined;
    try {
      const body = await response.json();
      code = body?.error?.code;
      message = body?.error?.message || message;
    } catch {
      // Keep a generic error when the server response is not the API envelope.
    }
    throw new ApiError(message, response.status, code);
  }
  return response.json() as Promise<T>;
}

export type DashboardStats = {
  total_leads: number;
  imported: number;
  processing: number;
  completed: number;
  failed: number;
  average_lead_score: number | null;
};

export type LeadListItem = {
  id: string;
  company_name: string;
  website: string | null;
  industry: string | null;
  city: string | null;
  state: string | null;
  country: string | null;
  status: string;
  sales_status: SalesStatus;
  priority: SalesPriority;
  assigned_to: string | null;
  next_follow_up_at: string | null;
  lost_reason: string | null;
  lead_score: number | null;
  recommended_services: string[] | null;
  intelligence_status: "available" | "unavailable";
  intelligence_score: number | null;
  opportunity_count: number;
  recommendation_count: number;
  personalization_available: boolean;
  latest_enrichment_at: string | null;
  created_at: string;
  updated_at: string;
};

export type LeadDetail = {
  id: string;
  company_name: string;
  website: string | null;
  phone: string | null;
  email: string | null;
  address: string | null;
  city: string | null;
  state: string | null;
  country: string | null;
  industry: string | null;
  status: string;
  sales_status: SalesStatus;
  priority: SalesPriority;
  assigned_to: string | null;
  next_follow_up_at: string | null;
  lost_reason: string | null;
  intelligence_status: "available" | "unavailable";
  intelligence_score: number | null;
  latest_enrichment_at: string | null;
};

export type SalesStatus = "NEW" | "QUALIFIED" | "CONTACTED" | "ENGAGED" | "PROPOSAL" | "NEGOTIATION" | "WON" | "LOST";
export type SalesPriority = "LOW" | "MEDIUM" | "HIGH" | "URGENT";
export type SalesActivityType = "NOTE" | "CALL" | "EMAIL" | "WHATSAPP" | "MEETING" | "STATUS_CHANGE" | "FOLLOW_UP" | "PROPOSAL" | "OTHER";
export type SalesActivityOutcome = "NO_RESPONSE" | "CONNECTED" | "INTERESTED" | "NOT_INTERESTED" | "CALLBACK_REQUESTED" | "MEETING_BOOKED" | "PROPOSAL_REQUESTED" | "PROPOSAL_SENT" | "NEGOTIATION_STARTED" | "WON" | "LOST" | "INVALID_CONTACT";
export type SalesActivity = { id: string; lead_id: string; activity_type: SalesActivityType; occurred_at: string; note: string; outcome: SalesActivityOutcome | null; suggested_task_type: SalesTaskType | null; outreach_execution_id: string | null };
export type SalesWorkflow = { sales_status: SalesStatus; priority: SalesPriority; assigned_to: string | null; next_follow_up_at: string | null; lost_reason: string | null; recent_activities: SalesActivity[] };

export type SalesTaskType = "FOLLOW_UP" | "CALL" | "EMAIL" | "WHATSAPP" | "MEETING" | "PROPOSAL" | "REVIEW_OUTREACH" | "OTHER";
export type SalesTaskStatus = "OPEN" | "IN_PROGRESS" | "COMPLETED" | "CANCELLED";
export type SalesTask = {
  id: string; lead_id: string; lead_company_name: string | null; assigned_to: string | null;
  task_type: SalesTaskType; title: string; description: string | null; due_at: string;
  priority: SalesPriority; status: SalesTaskStatus; related_activity_id: string | null;
  related_outreach_artifact_id: string | null; created_at: string; updated_at: string;
  completed_at: string | null; is_due: boolean; is_overdue: boolean;
};
export type SalesTaskCreate = Pick<SalesTask, "task_type" | "title" | "due_at" | "priority"> & Partial<Pick<SalesTask, "assigned_to" | "description" | "related_activity_id" | "related_outreach_artifact_id">>;
export type SalesTaskSummary = { today: SalesTask[]; overdue: SalesTask[]; upcoming: SalesTask[] };

export type CommercialRecord = {
  lead_id: string; sales_status: SalesStatus; estimated_value: string | null;
  proposal_value: string | null; negotiation_value: string | null; won_value: string | null;
  expected_collection: string | null; actual_collected: string; outstanding_collection: string; currency: string;
};
export type SalesCollection = {
  id: string; lead_id: string; lead_company_name: string | null; amount: string; currency: string;
  collection_date: string; payment_reference: string | null; notes: string | null;
  created_at: string; updated_at: string;
};
export type RevenueTarget = {
  id: string; label: string; target_amount: string; currency: string;
  period_start: string; period_end: string; active: boolean; created_at: string; updated_at: string;
};
export type RevenueSummary = {
  currency: string; pipeline_value: string; proposal_value: string; negotiation_value: string;
  won_value: string; expected_collection: string; actual_collected: string; outstanding_collection: string;
  target_id: string | null; target_label: string | null; target_amount: string | null;
  target_collected: string | null; target_remaining: string | null; achievement_percentage: string | null;
};
export type RevenuePipelineStage = { sales_status: SalesStatus; lead_count: number; current_opportunity_value: string; won_value: string; collected_value: string };
export type RevenueConversionStep = { from_status: SalesStatus; to_status: SalesStatus; source_count: number; converted_count: number; conversion_percentage: string | null };
export type RevenueConversion = {
  steps: RevenueConversionStep[]; tracked_lead_count: number; overall_conversion_percentage: string | null;
  won_deal_count: number; lost_deal_count: number; open_opportunity_count: number;
  average_won_deal_value: string | null; average_proposal_value: string | null; history_limitation: string;
};
export type RevenueSources = { available: boolean; reason: string; sources: Array<Record<string, unknown>> };

export type OutreachStatus = "DRAFT" | "READY_FOR_REVIEW" | "APPROVED" | "REJECTED" | "STALE";
export type OutreachClaim = IntelligenceClaim;
export type OutreachText = { text: string; claims: OutreachClaim[] };
export type OutreachContent = {
  email: { subject: string; opening: OutreachText; body: OutreachText; call_to_action: OutreachText };
  whatsapp: { opening: OutreachText; message: OutreachText; call_to_action: OutreachText };
  call_script: { opening: OutreachText; key_problem: OutreachText; recommended_service_ids: string[]; discovery_questions: string[]; objections_and_talking_points: OutreachText; closing: OutreachText };
  follow_ups: Array<{ step: string; timing: string; purpose: string }>;
  recommended_service_ids: string[];
};
export type OutreachArtifact = {
  id: string;
  lead_id: string;
  enrichment_run_id: string;
  status: OutreachStatus;
  is_current: boolean;
  sales_status: SalesStatus;
  sales_context_version: string;
  contract_version: string;
  prompt_version: string;
  content: OutreachContent | null;
  validation_result: Record<string, unknown>;
  generation_error: string | null;
  generated_at: string;
  reviewed_at: string | null;
  reviewed_by: string | null;
  rejection_reason: string | null;
};
export type OutreachReadResponse = {
  current_context: { sales_status: SalesStatus; score: number | null; opportunity_count: number; recommended_services: string[]; personalization_available: boolean } | null;
  artifact: OutreachArtifact | null;
};
export type OutreachChannel = "EMAIL" | "WHATSAPP" | "MANUAL";
export type OutreachExecutionStatus = "QUEUED" | "SENDING" | "SENT" | "DELIVERED" | "FAILED" | "CANCELLED";
export type OutreachExecution = {
  id: string;
  lead_id: string;
  outreach_artifact_id: string;
  retry_of_execution_id: string | null;
  channel: OutreachChannel;
  manual_method: "EMAIL" | "WHATSAPP" | null;
  provider: string;
  status: OutreachExecutionStatus;
  attempt_number: number;
  recipient: string | null;
  subject: string | null;
  content_snapshot: { content: OutreachContent; selected_channel: string; subject: string | null; text: string };
  provider_message_id: string | null;
  error_code: string | null;
  error_message: string | null;
  requested_at: string;
  sent_at: string | null;
  delivered_at: string | null;
  failed_at: string | null;
  created_at: string;
  updated_at: string;
};

export type IntelligenceClaim = {
  type: "verified_fact" | "supported_inference" | "suggested_language";
  text: string;
  evidence_ids: string[];
  opportunity_ids: string[];
  service_ids: string[];
};

export type IntelligenceResponse = {
  api_version: "lead-intelligence-v1";
  lead: {
    id: string;
    company_name: string;
    website: string | null;
    industry: string | null;
    city: string | null;
    state: string | null;
    country: string | null;
  };
  enrichment_status: string;
  run: {
    id: string;
    status: string;
    pipeline_version: string;
    started_at: string | null;
    completed_at: string | null;
  };
  score: {
    value: number | null;
    version: string;
    breakdown: Record<string, unknown>;
    evidence_ids: string[];
    calculated_at: string;
  };
  opportunities: Array<{
    id: string;
    category: string;
    severity: string;
    status: string;
    title: string;
    description: string;
    evidence_ids: string[];
    rule_version: string;
  }>;
  recommendations: Array<{
    id: string;
    service_id: string;
    service_name: string;
    service_description: string;
    service_version: string;
    rank: number;
    rationale: string;
    confidence: number;
    opportunity_ids: string[];
    evidence_ids: string[];
    rule_version: string;
  }>;
  evidence: Array<{
    id: string;
    evidence_type: string;
    source_type: string;
    provider_name: string;
    source_url: string | null;
    observed_value: unknown;
    observed_at: string;
    fetched_at: string;
    confidence: number | null;
  }>;
  personalization: {
    id: string;
    contract_version: string;
    prompt_version: string;
    created_at: string;
    confidence_score: number;
    sales_opening: { text: string; claims: IntelligenceClaim[] };
    audit_summary: { text: string; claims: IntelligenceClaim[] };
    audit_video_script: { text: string; claims: IntelligenceClaim[] };
    recommended_package_language: { text: string; claims: IntelligenceClaim[] };
  } | null;
  personalization_available: boolean;
  processing: {
    job_id: string | null;
    status: string | null;
    checkpoint: string | null;
    attempt_count: number | null;
    max_attempts: number | null;
    started_at: string | null;
    completed_at: string | null;
    updated_at: string | null;
  };
  generated_at: string;
};
