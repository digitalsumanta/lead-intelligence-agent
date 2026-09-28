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
  intelligence_status: "available" | "unavailable";
  intelligence_score: number | null;
  latest_enrichment_at: string | null;
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
