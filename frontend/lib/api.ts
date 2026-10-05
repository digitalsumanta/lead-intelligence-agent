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

export type SeoAuditRun = {
  id: string; lead_id: string; status: "QUEUED" | "RUNNING" | "COMPLETED" | "FAILED";
  audit_version: string; root_url: string; normalized_root_url: string;
  pages_crawled: number; pages_discovered: number; findings_count: number;
  metadata: Record<string, unknown>; error_code: string | null; error_message: string | null;
  started_at: string | null; completed_at: string | null; created_at: string;
};
export type SeoFinding = {
  id: string; observation_id: string | null; page_url: string | null; finding_code: string;
  category: string; severity: "INFO" | "LOW" | "MEDIUM" | "HIGH";
  title: string; description: string; evidence: Record<string, unknown>; created_at: string;
};
export type SeoObservation = { id: string; observation_type: string; page_url: string | null; observed_value: Record<string, unknown>; created_at: string };
export type LatestSeo = { audit: SeoAuditRun | null; findings: SeoFinding[] };
export type SeoRunList = { items: SeoAuditRun[]; page: number; page_size: number };
export type SeoRunDetail = { run: SeoAuditRun; observations: SeoObservation[]; findings: SeoFinding[] };

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
export type SalesActionTask = SalesTask & {
  lead_sales_status: SalesStatus;
  lead_priority: SalesPriority;
  lead_assigned_to: string | null;
  next_follow_up_at: string | null;
  current_stage_value: string | null;
  current_stage_value_field: "estimated_value" | "proposal_value" | "negotiation_value" | "won_value" | null;
  currency: string;
};
export type OutcomeSuggestion = {
  activity_id: string; lead_id: string; lead_company_name: string;
  activity_type: SalesActivityType; outcome: SalesActivityOutcome; occurred_at: string;
  suggested_task_type: SalesTaskType; lead_sales_status: SalesStatus; lead_priority: SalesPriority;
  lead_assigned_to: string | null; next_follow_up_at: string | null;
  current_stage_value: string | null;
  current_stage_value_field: "estimated_value" | "proposal_value" | "negotiation_value" | "won_value" | null;
  currency: string;
};
export type SalesTaskCreate = Pick<SalesTask, "task_type" | "title" | "due_at" | "priority"> & Partial<Pick<SalesTask, "assigned_to" | "description" | "related_activity_id" | "related_outreach_artifact_id">> & { seo_opportunity_id?: string };
export type SalesTaskSummary = { today: SalesActionTask[]; overdue: SalesActionTask[]; upcoming: SalesActionTask[]; outcome_suggestions: OutcomeSuggestion[]; seo_opportunities: SeoSalesOpportunity[] };
export type SeoSalesOpportunityEvidence = {
  finding_id: string; finding_code: string; title: string; page_url: string | null;
  severity: "INFO" | "LOW" | "MEDIUM" | "HIGH"; observed_values: Record<string, unknown>;
};
export type SeoSalesOpportunity = {
  opportunity_id: string; lead_id: string; seo_run_id: string;
  type: "TECHNICAL_SEO" | "ON_PAGE_SEO" | "INDEXABILITY" | "PERFORMANCE" | "STRUCTURED_DATA" | "LINKS" | "IMAGES" | "CRAWLABILITY";
  scope: "SITE"; title: string; summary: string; severity: "INFO" | "LOW" | "MEDIUM" | "HIGH";
  affected_page_count: number; affected_pages: string[]; finding_count: number;
  evidence: SeoSalesOpportunityEvidence[]; recommended_service_ids: string[];
  suggested_task_type: SalesTaskType; suggested_action: string;
  current_sales_status: SalesStatus; lead_priority: SalesPriority; assigned_to: string | null;
  next_follow_up_at: string | null; current_stage_value: string | null;
  current_stage_value_field: string | null; currency: string;
  current_task_state: "NO_TASK" | SalesTaskStatus; related_task_id: string | null;
};
export type LeadSeoOpportunities = { items: SeoSalesOpportunity[] };

export type AISalesClaimKind = "VERIFIED_FACT" | "SUPPORTED_INFERENCE" | "SUGGESTED_LANGUAGE" | "UNCERTAINTY";
export type AISalesActionType = "REVIEW_SEO_OPPORTUNITY" | "CALL_PROSPECT" | "FOLLOW_UP" | "REVIEW_OUTREACH" | "PREPARE_PROPOSAL" | "INVESTIGATE_MISSING_INFORMATION" | "REVIEW_COMMERCIAL_CONTEXT";
export type AISalesClaim = { kind: AISalesClaimKind; text: string | null; evidence_refs: string[] };
export type AISalesOpportunity = {
  title: string; rationale: AISalesClaim; source_refs: string[];
  action_type: AISalesActionType; service_ids: string[];
};
export type AISalesRecommendedAction = {
  action_type: AISalesActionType; rationale: AISalesClaim; source_refs: string[];
  task_type: SalesTaskType | null; service_ids: string[];
};
export type AISalesIntelligenceResponse = {
  contract_version: "ai-sales-intelligence-v1"; prompt_version: "ai-sales-intelligence-prompt-v1";
  summary: AISalesClaim[]; insights: AISalesClaim[]; opportunities: AISalesOpportunity[];
  recommended_actions: AISalesRecommendedAction[]; uncertainties: AISalesClaim[]; evidence_refs: string[];
};
export type AISalesEvidenceCatalogEntry = { id: string; source_type: string; label: string; display_value: string };
export type AISalesIntelligenceArtifact = {
  id: string; lead_id: string; context_version: string; contract_version: string; prompt_version: string;
  provider: string; model: string; status: "validated" | "failed"; source_fingerprint: string;
  is_stale: boolean; is_current: boolean; generated_at: string; created_at: string;
  generated: AISalesIntelligenceResponse | null; evidence_catalog: AISalesEvidenceCatalogEntry[];
  evidence_refs: string[]; usage_metadata: Record<string, unknown>;
  error_code: string | null; error_message: string | null;
};
export type AISalesIntelligenceHistory = { items: AISalesIntelligenceArtifact[] };

export type CopilotIntent = "NEXT_STEP" | "WHY_FOLLOW_UP" | "MAIN_OPPORTUNITIES" | "MISSING_INFORMATION" | "PREVIOUS_OUTREACH" | "CURRENT_SEO_OPPORTUNITIES" | "REVIEW_BEFORE_CONTACT" | "UNSUPPORTED";
export type CopilotReviewActionType = "REVIEW_LEAD" | "REVIEW_OPPORTUNITY" | "REVIEW_SEO" | "REVIEW_OUTREACH" | "REVIEW_COMMERCIAL_CONTEXT" | "VERIFY_MISSING_INFORMATION";
export type CopilotReviewAction = { action: CopilotReviewActionType; evidence_refs: string[]; service_ids: string[] };
export type CopilotResponse = {
  contract_version: "ai-sales-copilot-v1"; prompt_version: "ai-sales-copilot-prompt-v1";
  intent: CopilotIntent; answer: string; key_facts: string[]; relevant_opportunities: string[];
  recommended_review_actions: CopilotReviewAction[]; missing_information: string[]; evidence_refs: string[];
};
export type CopilotInteraction = {
  id: string; lead_id: string; question: string; context_version: string;
  contract_version: string; prompt_version: string; provider: string; model: string;
  intent: CopilotIntent; status: "validated" | "failed" | "unsupported"; source_fingerprint: string; is_stale: boolean;
  response: CopilotResponse | null; evidence_catalog: AISalesEvidenceCatalogEntry[];
  usage_metadata: Record<string, unknown>; error_code: string | null; error_message: string | null;
  created_at: string;
};
export type CopilotHistory = { items: CopilotInteraction[] };
export type PlannerActionType = "FOLLOW_UP" | "CALL_PROSPECT" | "REVIEW_SEO_OPPORTUNITY" | "REVIEW_OUTREACH" | "PREPARE_PROPOSAL" | "INVESTIGATE_MISSING_INFORMATION" | "REVIEW_COMMERCIAL_CONTEXT";
export type PlannerAction = { action_type: PlannerActionType; priority: SalesPriority; title: string; rationale: string; evidence_ids: string[]; opportunity_ids: string[]; service_ids: string[]; preparation_items: string[] };
export type ActionPlan = {
  id: string; lead_id: string; contract_version: "ai-sales-action-planner-v1"; prompt_version: "ai-sales-action-planner-prompt-v1";
  context_version: "ai-sales-context-v1"; provider: string; model: string; status: "validated" | "failed";
  source_fingerprint: string; is_stale: boolean; is_current: boolean; action_plan: { contract_version: "ai-sales-action-planner-v1"; prompt_version: "ai-sales-action-planner-prompt-v1"; actions: PlannerAction[] } | null;
  evidence_catalog: AISalesEvidenceCatalogEntry[]; evidence_refs: string[]; usage_metadata: Record<string, unknown>;
  error_code: string | null; error_message: string | null; created_at: string; updated_at: string;
};
export type ActionPlanHistory = { items: ActionPlan[] };
export type AgentPolicyDecision = "ALLOW" | "DENY" | "REQUIRE_HUMAN_APPROVAL";
export type AgentPolicyEvaluation = {
  evaluation_id: string; lead_id: string; action_type: string; requested_channel: string | null;
  decision: AgentPolicyDecision; reason: string; evidence_ids: string[]; opportunity_ids: string[];
  service_ids: string[]; policy_version: "agent-policy-v1"; autonomy_level: "HUMAN_ONLY" | "ASSISTED" | "SUPERVISED" | "AUTONOMOUS";
  source_fingerprint: string | null; request_fingerprint: string; requested_by: string;
  rule_results: { rule: string; passed: boolean; result: string; reason: string }[]; evaluated_at: string;
};
export type AgentPolicyHistory = { items: AgentPolicyEvaluation[] };
export type ProposalItem = { item_type: string; text: string; evidence_refs: string[] };
export type ProposalService = { service_id: string; evidence_refs: string[] };
export type ProposalOpportunityEvidence = { opportunity_id: string; label: string; evidence_refs: string[] };
export type ProposalCommercialReference = { evidence_ref: string; label: string; display_value: string };
export type ProposalBrief = {
  lead_id: string; proposal_type: "SEO_REVIEW" | "WEBSITE_IMPROVEMENT" | "COMMERCIAL_REVIEW" | "GENERAL_REVIEW";
  executive_summary: string; identified_needs: ProposalItem[]; recommended_services: ProposalService[];
  opportunity_evidence: ProposalOpportunityEvidence[]; proposed_scope: ProposalItem[];
  deliverables: string[]; assumptions: string[]; exclusions: string[]; timeline_guidance: string;
  commercial_context: ProposalCommercialReference[]; missing_information: ProposalItem[];
  evidence_refs: string[]; review_notes: string[];
};
export type ProposalIntelligence = {
  id: string; lead_id: string; contract_version: "ai-proposal-intelligence-v1";
  prompt_version: "ai-proposal-intelligence-prompt-v1"; context_version: "ai-sales-context-v1";
  provider: string; model: string; status: "validated" | "failed"; source_fingerprint: string;
  is_stale: boolean; is_current: boolean; proposal_brief: ProposalBrief | null;
  evidence_catalog: AISalesEvidenceCatalogEntry[]; evidence_refs: string[];
  usage_metadata: Record<string, unknown>; error_code: string | null; error_message: string | null;
  created_at: string; updated_at: string;
};
export type ProposalIntelligenceHistory = { items: ProposalIntelligence[] };

export type SalesSupervisorUrgency = "LOW" | "MEDIUM" | "HIGH";
export type SalesSupervisorAttention =
  | "FOLLOW_UP_DUE" | "FOLLOW_UP_OVERDUE" | "RECENT_OUTREACH"
  | "OUTREACH_RESPONSE_PENDING" | "PROPOSAL_REVIEW" | "SEO_OPPORTUNITY"
  | "MISSING_INFORMATION" | "COMMERCIAL_REVIEW" | "STALE_INTELLIGENCE"
  | "POLICY_BLOCKED" | "HUMAN_APPROVAL_REQUIRED" | "NO_ACTION_REQUIRED";
export type SalesSupervisorFinding = {
  finding_id: string; lead_id: string; attention_type: SalesSupervisorAttention;
  urgency: SalesSupervisorUrgency; title: string; summary: string; rationale: string;
  evidence_ids: string[]; action_type: PlannerActionType | null;
  action_priority: SalesSupervisorUrgency; opportunity_ids: string[]; service_ids: string[];
  missing_information: string[]; policy_decision: AgentPolicyDecision | null;
  autonomy_level: "HUMAN_ONLY" | "ASSISTED" | "SUPERVISED" | "AUTONOMOUS" | null;
  review_required: boolean;
};
export type SalesSupervisorResult = {
  artifact_version: "ai-sales-supervisor-v1";
  prompt_version: "ai-sales-supervisor-prompt-v1";
  context_version: "ai-sales-context-v1"; generated_at: string;
  context_fingerprint: string; findings: SalesSupervisorFinding[];
  pipeline_summary: {
    pipeline_total_leads: number; total_leads_reviewed: number; leads_needing_attention: number;
    overdue_followups: number; followups_due: number; proposal_reviews: number;
    seo_opportunity_reviews: number; missing_information_cases: number;
    policy_blocked_cases: number; human_approval_cases: number;
  };
  review_notes: string[]; evidence_refs: string[]; usage: Record<string, unknown>;
};
export type SalesSupervisorRun = {
  id: string; lead_id: string | null; scope: "PIPELINE" | "LEAD";
  status: "COMPLETED" | "FAILED"; artifact_version: "ai-sales-supervisor-v1";
  prompt_version: "ai-sales-supervisor-prompt-v1"; context_version: "ai-sales-context-v1";
  context_fingerprint: string; result: SalesSupervisorResult | null; evidence_refs: string[];
  usage: Record<string, unknown>; provider: string; model: string; error_code: string | null;
  error_message: string | null; is_current: boolean; is_stale: boolean;
  created_at: string; updated_at: string;
};
export type SalesSupervisorHistory = { items: SalesSupervisorRun[] };
export type TaskAgentExecution = {
  execution_id: string; lead_id: string; action_type: PlannerActionType;
  execution_status: "CREATED" | "ALREADY_EXISTS" | "DENIED" | "REQUIRES_HUMAN_APPROVAL" | "STALE_CONTEXT" | "INVALID_REQUEST" | "FAILED";
  policy_decision: AgentPolicyDecision | null; task_id: string | null; created: boolean;
  existing_task_id: string | null; context_fingerprint: string; evidence_ids: string[];
  source_artifact_id: string; source_action_id: string; policy_evaluation_id: string | null;
  reason: string; executed_at: string | null; created_at: string;
};
export type TaskAgentExecutionHistory = { items: TaskAgentExecution[] };
export type SupervisorTaskExecutionSummary = {
  total_findings: number; eligible: number; created: number; already_exists: number;
  denied: number; requires_human_approval: number; stale_context: number; failed: number;
  execution_ids: string[];
};

export type OutreachPreparationChannel = "EMAIL" | "WHATSAPP" | "CALL_SCRIPT";
export type OutreachPreparationStatus =
  | "READY_FOR_HUMAN_REVIEW" | "REQUIRES_HUMAN_APPROVAL" | "REJECTED"
  | "STALE_CONTEXT" | "INVALID_REQUEST" | "POLICY_DENIED";
export type OutreachPreparationRequest = {
  lead_id: string;
  source_type: "AI_SALES_ACTION_PLAN" | "AI_SALES_SUPERVISOR";
  source_id: string;
  source_action_id: string;
  action_type: PlannerActionType;
  channel: OutreachPreparationChannel;
};
export type OutreachPreparation = {
  id: string; lead_id: string; source_type: "AI_SALES_ACTION_PLAN" | "AI_SALES_SUPERVISOR";
  source_id: string; source_action_id: string; action_type: PlannerActionType;
  channel: OutreachPreparationChannel; purpose: string; subject: string | null;
  body: string | null; call_script: Record<string, unknown> | null;
  evidence_ids: string[]; opportunity_ids: string[]; service_ids: string[];
  context_fingerprint: string; context_version: string; preparation_version: string; prompt_version: string;
  provider: string | null; model: string | null; status: OutreachPreparationStatus;
  review_required: boolean; policy_decision: AgentPolicyDecision | null;
  policy_evaluation_id: string | null; validation_result: Record<string, unknown>; created_at: string;
};

export type OutreachApprovalStatus = "APPROVED" | "STALE";
export type OutreachApproval = {
  id: string; preparation_id: string; lead_id: string; status: OutreachApprovalStatus;
  approved_by_user_id: string; channel: "EMAIL"; recipient: string;
  context_fingerprint: string; preparation_version: string; content_hash: string;
  approval_version: string; policy_evaluation_id: string;
  policy_decision: "ALLOW" | "REQUIRE_HUMAN_APPROVAL"; approved_at: string;
};
export type AgentOutreachExecutionStatus = "EXECUTING" | "SENT" | "FAILED" | "UNCERTAIN" | "STALE" | "BLOCKED";
export type AgentOutreachExecution = {
  id: string; approval_id: string; preparation_id: string; lead_id: string;
  channel: "EMAIL"; recipient: string; subject: string; content_hash: string;
  context_fingerprint: string; execution_version: string; policy_evaluation_id: string;
  policy_decision: "ALLOW" | "DENY" | "REQUIRE_HUMAN_APPROVAL"; provider: string;
  provider_message_id: string | null; provider_status: string | null; status: AgentOutreachExecutionStatus;
  failure_code: string | null; failure_reason: string | null; idempotency_key: string;
  started_at: string; completed_at: string | null; created_at: string;
};
export type AgentOutreachExecutionHistory = { items: AgentOutreachExecution[] };
export type EmailDeliveryEventType = "SENT" | "DELIVERED" | "BOUNCED" | "FAILED" | "COMPLAINED" | "UNSUBSCRIBED";
export type EmailDeliveryEvent = {
  id: string; execution_id: string; event_type: EmailDeliveryEventType; recipient_email: string;
  provider: string | null;
  provider_message_id: string | null; provider_event_id: string | null; provider_status: string | null;
  occurred_at: string; recorded_at: string; source: "PROVIDER" | "INTERNAL";
};
export type EmailDeliveryEventHistory = { items: EmailDeliveryEvent[] };
export type DeliveryReconciliation = {
  execution_id: string; provider: string; provider_message_id: string | null;
  current_state: "UNSUBSCRIBED" | "COMPLAINED" | "BOUNCED" | "FAILED" | "DELIVERED" | "SENT" | "UNKNOWN";
  terminal_state: "UNSUBSCRIBED" | "COMPLAINED" | "BOUNCED" | "FAILED" | null;
  has_terminal_event: boolean; event_count: number; has_conflict: boolean;
  first_event_at: string | null; last_event_at: string | null;
  has_out_of_order_events: boolean;
  conflict_types: ("MULTIPLE_TERMINAL_STATES" | "DELIVERED_AFTER_TERMINAL" | "SENT_AFTER_TERMINAL" | "EVENT_TIMESTAMP_OUT_OF_ORDER" | "UNSUPPORTED_EVENT_TYPE")[];
  unsupported_event_count: number;
};

export function getOutreachApproval(preparationId: string): Promise<OutreachApproval | null> {
  return api<OutreachApproval | null>(`/api/agent/outreach/preparations/${encodeURIComponent(preparationId)}/approval`);
}

export function approveOutreachPreparation(preparationId: string): Promise<OutreachApproval> {
  return api<OutreachApproval>(`/api/agent/outreach/preparations/${encodeURIComponent(preparationId)}/approve`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({}),
  });
}

export function getPreparationExecutions(preparationId: string): Promise<AgentOutreachExecutionHistory> {
  return api<AgentOutreachExecutionHistory>(`/api/agent/outreach/preparations/${encodeURIComponent(preparationId)}/executions`);
}

export function getEmailDeliveryEvents(executionId: string): Promise<EmailDeliveryEventHistory> {
  return api<EmailDeliveryEventHistory>(`/api/outreach-executions/${encodeURIComponent(executionId)}/delivery-events`);
}

export function getDeliveryReconciliation(executionId: string): Promise<DeliveryReconciliation> {
  return api<DeliveryReconciliation>(`/api/outreach-executions/${encodeURIComponent(executionId)}/delivery-status`);
}

export function executeApprovedOutreach(approvalId: string): Promise<AgentOutreachExecution> {
  return api<AgentOutreachExecution>(`/api/agent/outreach/approvals/${encodeURIComponent(approvalId)}/execute`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({}),
  });
}

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

export type OutreachAnalyticsChannel = OutreachChannel;
export type OutreachAnalyticsStatus = OutreachExecutionStatus;
export type OutreachAnalyticsOutcome = SalesActivityOutcome;
export type OutreachAnalytics = {
  cohort: { from: string | null; to: string | null; channel: OutreachAnalyticsChannel | null; date_field: "sent_at"; timezone: "UTC" };
  attempts_total: number;
  attempts_by_channel: Array<{ channel: OutreachAnalyticsChannel; count: number }>;
  attempts_by_status: Array<{ status: OutreachAnalyticsStatus; count: number }>;
  recorded_outcomes_total: number;
  outcomes_by_type: Array<{ outcome: OutreachAnalyticsOutcome; activity_count: number; executions_with_outcome: number; leads_represented: number }>;
  executions_with_recorded_outcomes: number;
  leads_represented: number;
  leads_with_recorded_outcomes: number;
  outcome_coverage: { eligible_sent_executions: number; executions_with_outcomes: number; executions_without_outcomes: number };
  limitations: string[];
};

export type OutreachAnalyticsFilters = {
  from?: string;
  to?: string;
  channel?: OutreachAnalyticsChannel | "";
};

export function getOutreachAnalytics(filters: OutreachAnalyticsFilters = {}): Promise<OutreachAnalytics> {
  const query = new URLSearchParams();
  if (filters.from) query.set("from", filters.from);
  if (filters.to) query.set("to", filters.to);
  if (filters.channel) query.set("channel", filters.channel);
  const suffix = query.size ? `?${query.toString()}` : "";
  return api<OutreachAnalytics>(`/api/outreach/analytics${suffix}`);
}

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

export type DeliveryAnalyticsSummary = {
  scope: "global" | "date_range" | "provider" | "execution";
  start: string | null;
  end: string | null;
  provider: string | null;
  execution_id: string | null;
  sent_count: number;
  delivered_count: number;
  bounced_count: number;
  failed_count: number;
  complained_count: number;
  unsubscribed_count: number;
  delivery_rate: number | null;
  bounce_rate: number | null;
  failure_rate: number | null;
  complaint_rate: number | null;
  unsubscribe_rate: number | null;
  unknown_execution_count: number;
  sent_execution_count: number;
  delivered_execution_count: number;
  bounced_execution_count: number;
  failed_execution_count: number;
  complained_execution_count: number;
  unsubscribed_execution_count: number;
  conflicted_execution_count: number;
  out_of_order_execution_count: number;
  unsupported_event_count: number;
};

export type DeliveryAnalyticsProviderSummary = {
  provider: string;
  sent_count: number;
  delivered_count: number;
  bounced_count: number;
  failed_count: number;
  complained_count: number;
  unsubscribed_count: number;
  delivery_rate: number | null;
  bounce_rate: number | null;
  failure_rate: number | null;
  complaint_rate: number | null;
  unsubscribe_rate: number | null;
};

export type DeliveryAnalyticsTimeSeriesPoint = {
  period_start: string;
  sent_count: number;
  delivered_count: number;
  bounced_count: number;
  failed_count: number;
  complained_count: number;
  unsubscribed_count: number;
};

export type ExecutionDeliveryAnalytics = {
  execution_id: string;
  provider: string;
  provider_message_id: string | null;
  recipient: string | null;
  current_reconciled_state: "UNSUBSCRIBED" | "COMPLAINED" | "BOUNCED" | "FAILED" | "DELIVERED" | "SENT" | "UNKNOWN";
  event_count: number;
  first_event_at: string | null;
  last_event_at: string | null;
  has_conflict: boolean;
  conflict_types: string[];
  has_out_of_order_events: boolean;
  unsupported_event_count: number;
};

export function getDeliveryAnalyticsSummary(scope: string = "global", params?: Record<string, string>): Promise<DeliveryAnalyticsSummary> {
  const query = new URLSearchParams({ scope, ...params });
  return api<DeliveryAnalyticsSummary>(`/api/email-delivery-analytics/summary?${query.toString()}`);
}

export function getDeliveryAnalyticsProviders(): Promise<DeliveryAnalyticsProviderSummary[]> {
  return api<DeliveryAnalyticsProviderSummary[]>(`/api/email-delivery-analytics/providers`);
}

export function getDeliveryAnalyticsTimeSeries(granularity: string = "day", params?: Record<string, string>): Promise<DeliveryAnalyticsTimeSeriesPoint[]> {
  const query = new URLSearchParams({ granularity, ...params });
  return api<DeliveryAnalyticsTimeSeriesPoint[]>(`/api/email-delivery-analytics/timeseries?${query.toString()}`);
}

export function getExecutionDeliveryAnalytics(executionId: string): Promise<ExecutionDeliveryAnalytics> {
  return api<ExecutionDeliveryAnalytics>(`/api/outreach-executions/${encodeURIComponent(executionId)}/delivery-analytics`);
}

export type IntelligenceCompanyProfile = {
  name: string | null; legal_name: string | null; website: string | null;
  industry: string | null; industry_source: string | null;
  company_size: string | null; company_size_source: string | null;
  location: string | null; country: string | null; description: string | null;
  products_services: string[];
};
export type IntelligenceContactProfile = {
  name: string | null; job_title: string | null; email: string | null; phone: string | null; location: string | null;
};
export type IntelligenceDigitalPresence = {
  website: string | null; linkedin_url: string | null; facebook_url: string | null;
  instagram_url: string | null; youtube_url: string | null; other_urls: string[];
};
export type IntelligencePainPoint = {
  id: string; title: string; description: string | null; source_type: string;
  source_reference: string | null; confidence: string; created_at: string; updated_at: string;
};
export type IntelligenceOpportunity = {
  id: string; title: string; description: string | null; opportunity_type: string | null;
  score: number | null; priority: string | null; recommended_action: string | null; evidence: any | null;
  source_type: string; source_reference: string | null; confidence: string; created_at: string; updated_at: string;
  related_task_id?: string | null; related_task_status?: string | null;
};
export type SalesOpportunitiesResponse = { opportunities: IntelligenceOpportunity[] };
export type IntelligenceBusinessContext = {
  pain_points: IntelligencePainPoint[]; opportunities: IntelligenceOpportunity[];
  known_services: string[]; known_products: string[];
};
export type IntelligenceQuality = {
  completeness_score: number; last_researched_at: string | null;
  field_count: number; populated_field_count: number;
};
export type LeadIntelligenceProfileSchema = {
  lead_id: string; company: IntelligenceCompanyProfile; contact: IntelligenceContactProfile;
  digital_presence: IntelligenceDigitalPresence; business_context: IntelligenceBusinessContext;
  intelligence_quality: IntelligenceQuality;
};

export type CompanyEnrichmentProfileSchema = {
  id: string;
  lead_id: string;
  legal_name: string | null;
  display_name: string | null;
  domain: string | null;
  website: string | null;
  email_domain: string | null;
  linkedin_url: string | null;
  phone: string | null;
  location: string | null;
  city: string | null;
  state: string | null;
  country: string | null;
  description: string | null;
  industry: string | null;
  normalized_industry: string | null;
  company_size: string | null;
  normalized_company_size: string | null;
  source: string;
  confidence: string;
  completeness_percent: number;
  created_at: string;
  updated_at: string;
};

export type WebsiteIntelligenceProfileSchema = {
  id: string;
  company_id: string;
  website: string | null;
  normalized_website: string | null;
  domain: string | null;
  normalized_domain: string | null;
  website_present: boolean;
  domain_present: boolean;
  technology_signals: Record<string, any> | null;
  content_signals: Record<string, any> | null;
  source_type: string;
  confidence: string;
  completeness: number;
  created_at: string;
  updated_at: string;
};

export type DigitalMarketingIntelligenceProfileSchema = {
  id: string;
  company_id: string;
  seo_signals: Record<string, any> | null;
  paid_advertising_signals: Record<string, any> | null;
  social_media_signals: Record<string, any> | null;
  content_marketing_signals: Record<string, any> | null;
  marketing_maturity: string;
  marketing_gaps: string[] | null;
  marketing_opportunities: string[] | null;
  source_type: string;
  confidence: string;
  completeness: number;
  created_at: string;
  updated_at: string;
};

export type BuyingSignalSchema = {
  id: string;
  company_id: string;
  signal_type: string;
  signal_category: string;
  confidence: string;
  strength: string | null;
  source_type: string;
  detected_at: string;
  evidence: Record<string, any>;
  status: string;
  created_at: string;
  updated_at: string;
};

export type BuyingSignalsResponse = {
  signals: BuyingSignalSchema[];
};

export type SeniorityLevel = "UNKNOWN" | "INDIVIDUAL_CONTRIBUTOR" | "MANAGER" | "LEADERSHIP" | "EXECUTIVE";
export type FunctionalArea = "UNKNOWN" | "EXECUTIVE" | "MARKETING" | "DIGITAL_MARKETING" | "SEO" | "SALES" | "IT" | "TECHNOLOGY" | "FINANCE" | "HR" | "OPERATIONS" | "PROCUREMENT";
export type BuyingInfluenceLevel = "UNKNOWN" | "LOW" | "MEDIUM" | "HIGH" | "DECISION_MAKER";
export type SourceType = "LEAD_RECORD" | "COMPANY_RECORD" | "CONTACT_RECORD" | "USER_PROVIDED" | "SYSTEM_DERIVED";
export type ConfidenceLevel = "UNKNOWN" | "LOW" | "MEDIUM" | "HIGH";

export type ContactIntelligenceProfile = {
  id: string;
  contact_id: string;
  department: string | null;
  functional_area: FunctionalArea;
  seniority_level: SeniorityLevel;
  decision_maker_score: number;
  buying_influence: BuyingInfluenceLevel;
  confidence: ConfidenceLevel;
  source_type: SourceType;
  evidence: Record<string, any> | null;
  evaluated_at: string;
  created_at: string;
  updated_at: string;
};
