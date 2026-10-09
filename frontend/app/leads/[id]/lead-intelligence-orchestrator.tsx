"use client";

import { useState } from "react";
import {
  orchestrateLeadIntelligence,
  ApiError,
  LeadIntelligenceOrchestrationResponse,
  OrchestrationOpportunityResult,
} from "@/lib/api";

function statusBadgeClass(status: string) {
  switch (status) {
    case "TASK_CREATED":
    case "COMPLETED":
      return "bg-emerald-50 text-emerald-700 border border-emerald-200";
    case "TASK_REUSED":
      return "bg-teal/10 text-teal border border-teal/20";
    case "PARTIAL":
      return "bg-amber-50 text-amber-800 border border-amber-200";
    case "NO_CONTACT":
    case "NO_RECOMMENDATION":
    case "NO_OPPORTUNITY":
      return "bg-slate-100 text-slate-700 border border-slate-200";
    case "FAILED":
      return "bg-rose-50 text-rose-800 border border-rose-200";
    default:
      return "bg-slate-100 text-slate-600 border border-slate-200";
  }
}

function priorityBadgeClass(priority: string) {
  switch (priority) {
    case "CRITICAL":
    case "URGENT":
      return "bg-rose-50 text-rose-700 border border-rose-200";
    case "HIGH":
      return "bg-amber-50 text-amber-800 border border-amber-200";
    case "MEDIUM":
      return "bg-sky-50 text-sky-800 border border-sky-200";
    default:
      return "bg-slate-100 text-slate-600 border border-slate-200";
  }
}

function formatLabel(value: string | null | undefined) {
  if (!value) return "—";
  return value.replaceAll("_", " ");
}

export default function LeadIntelligenceOrchestrator({
  leadId,
  onTasksChanged,
}: {
  leadId: string;
  onTasksChanged?: () => void;
}) {
  const [data, setData] = useState<LeadIntelligenceOrchestrationResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [lastRunAt, setLastRunAt] = useState<Date | null>(null);

  async function handleOrchestrate() {
    if (busy) return;
    setBusy(true);
    setError("");

    try {
      const response = await orchestrateLeadIntelligence(leadId);
      setData(response);
      setLastRunAt(new Date());

      // Task refresh rule: refresh if tasks were created or reused
      if (response.summary && (response.summary.tasks_created > 0 || response.summary.tasks_reused > 0)) {
        onTasksChanged?.();
      }
    } catch (cause) {
      setError(
        cause instanceof ApiError
          ? cause.message
          : "Intelligence orchestration request failed. Check API connectivity and try again."
      );
    } finally {
      setBusy(false);
    }
  }

  const summary = data?.summary;

  return (
    <section id="intelligence-orchestrator" className="mt-5 rounded-xl border border-teal/30 bg-white p-5 shadow-sm">
      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <span className="text-[10px] font-bold uppercase tracking-[.18em] text-teal">Deterministic Pipeline</span>
            {data && (
              <span className={`rounded-full px-2.5 py-0.5 text-xs font-semibold ${statusBadgeClass(data.status)}`}>
                {formatLabel(data.status)}
              </span>
            )}
          </div>
          <h2 className="mt-1 text-xl font-bold text-ink">Intelligence Orchestrator</h2>
          <p className="mt-1 text-sm text-slate-500">
            Coordinates contact matching, recommendation, task synchronization, and execution feedback without manual intervention.
          </p>
        </div>
        <div className="flex flex-col items-end gap-1">
          <button
            type="button"
            disabled={busy}
            onClick={() => void handleOrchestrate()}
            className="rounded-lg bg-ink px-4 py-2 text-sm font-semibold text-white shadow-sm hover:bg-slate-800 disabled:opacity-50"
          >
            {busy ? "Orchestrating intelligence…" : data ? "Re-run Orchestration" : "Run Intelligence Orchestrator"}
          </button>
          {lastRunAt && (
            <span className="text-[11px] text-slate-400">
              Last executed: {lastRunAt.toLocaleTimeString()}
            </span>
          )}
        </div>
      </div>

      {/* Error alert */}
      {error && (
        <div role="alert" className="mt-4 rounded-lg border border-rose-200 bg-rose-50 p-4 text-sm text-rose-900">
          <p className="font-semibold">Orchestration Error</p>
          <p className="mt-1">{error}</p>
        </div>
      )}

      {/* Idle state */}
      {!data && !busy && !error && (
        <div className="mt-4 rounded-lg border border-dashed border-slate-200 bg-slate-50/60 p-6 text-center">
          <p className="text-sm font-medium text-slate-700">Intelligence Orchestration is idle</p>
          <p className="mt-1 text-xs text-slate-500">
            Click &quot;Run Intelligence Orchestrator&quot; to evaluate sales opportunities, match company contacts, choose recommended sales actions, and synchronize human tasks.
          </p>
        </div>
      )}

      {/* Loading state */}
      {busy && !data && (
        <div className="mt-4 rounded-lg border border-teal/20 bg-teal/5 p-6 text-center">
          <div className="inline-block h-6 w-6 animate-spin rounded-full border-2 border-teal border-t-transparent" />
          <p className="mt-2 text-sm font-semibold text-teal">Executing deterministic orchestration workflow…</p>
          <p className="mt-1 text-xs text-slate-500">Matching contacts, scoring recommendations, and synchronizing sales tasks.</p>
        </div>
      )}

      {/* Results content */}
      {data && (
        <div className="mt-5 space-y-6">
          {/* Summary metrics bar */}
          {summary && (
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
              <div className="rounded-lg bg-slate-50 p-3">
                <p className="text-xs text-slate-500">Opportunities</p>
                <p className="mt-1 text-lg font-bold text-ink">{summary.total_opportunities}</p>
              </div>
              <div className="rounded-lg bg-emerald-50/60 p-3">
                <p className="text-xs text-emerald-700">Tasks Created</p>
                <p className="mt-1 text-lg font-bold text-emerald-800">{summary.tasks_created}</p>
              </div>
              <div className="rounded-lg bg-teal/10 p-3">
                <p className="text-xs text-teal">Tasks Reused</p>
                <p className="mt-1 text-lg font-bold text-teal">{summary.tasks_reused}</p>
              </div>
              <div className="rounded-lg bg-slate-50 p-3">
                <p className="text-xs text-slate-500">No Contacts</p>
                <p className="mt-1 text-lg font-bold text-slate-700">{summary.no_contact_count}</p>
              </div>
              <div className="rounded-lg bg-slate-50 p-3">
                <p className="text-xs text-slate-500">No Action / Rec</p>
                <p className="mt-1 text-lg font-bold text-slate-700">{summary.no_recommendation_count}</p>
              </div>
              <div className="rounded-lg bg-rose-50/60 p-3">
                <p className="text-xs text-rose-700">Failed Opps</p>
                <p className="mt-1 text-lg font-bold text-rose-800">{summary.failed_count}</p>
              </div>
            </div>
          )}

          {/* No opportunities state */}
          {data.opportunities.length === 0 && (
            <div className="rounded-lg border border-slate-200 bg-slate-50 p-4 text-sm text-slate-600">
              No sales opportunities exist for this lead. Create or derive opportunities before running intelligence matching.
            </div>
          )}

          {/* Opportunity cards */}
          {data.opportunities.length > 0 && (
            <div className="space-y-4">
              <h3 className="text-sm font-bold uppercase tracking-wider text-slate-500">
                Opportunity Orchestration Results ({data.opportunities.length})
              </h3>

              {data.opportunities.map((opp) => (
                <OpportunityOrchestrationCard key={opp.opportunity_id} opp={opp} />
              ))}
            </div>
          )}

          {/* Decision trace & diagnostics */}
          <details className="rounded-lg border border-slate-200 bg-slate-50 p-3">
            <summary className="cursor-pointer text-xs font-bold uppercase tracking-wide text-slate-600">
              Deterministic Decision Trace ({data.decision_trace.length} step tokens)
            </summary>
            <div className="mt-3 max-h-48 overflow-auto rounded bg-white p-3 font-mono text-xs text-slate-700">
              <ol className="list-decimal space-y-1 pl-4">
                {data.decision_trace.map((token, idx) => (
                  <li key={idx} className="break-all">
                    {token}
                  </li>
                ))}
              </ol>
            </div>
          </details>

          {/* Structured evidence catalog */}
          {data.evidence && data.evidence.length > 0 && (
            <details className="rounded-lg border border-slate-200 bg-slate-50 p-3">
              <summary className="cursor-pointer text-xs font-bold uppercase tracking-wide text-slate-600">
                Aggregated Evidence Graph ({data.evidence.length} entries)
              </summary>
              <pre className="mt-3 max-h-48 overflow-auto rounded bg-white p-3 font-mono text-xs text-slate-700">
                {JSON.stringify(data.evidence, null, 2)}
              </pre>
            </details>
          )}
        </div>
      )}
    </section>
  );
}

function OpportunityOrchestrationCard({ opp }: { opp: OrchestrationOpportunityResult }) {
  const contact = opp.recommended_contact;
  const rec = opp.recommendation;
  const task = opp.sales_task;
  const feedback = opp.feedback;

  return (
    <article className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm transition hover:border-slate-300">
      {/* Opportunity Header */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-100 pb-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className={`rounded-full px-2 py-0.5 text-xs font-semibold ${priorityBadgeClass(opp.priority)}`}>
            {opp.priority}
          </span>
          <h4 className="font-bold text-ink">{opp.opportunity_title}</h4>
          <span className="text-xs text-slate-400">({opp.opportunity_type})</span>
        </div>
        <div className="flex items-center gap-2">
          <span className="rounded border border-slate-200 bg-slate-50 px-2 py-0.5 text-xs font-semibold text-slate-700">
            Score: {opp.score}
          </span>
          <span className={`rounded-full px-2.5 py-0.5 text-xs font-semibold ${statusBadgeClass(opp.status)}`}>
            {formatLabel(opp.status)}
          </span>
        </div>
      </div>

      {/* Card Body Grid */}
      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        {/* Left Column: Target Contact & Recommendation */}
        <div className="space-y-3">
          {/* Target Contact */}
          <div className="rounded-lg border border-slate-100 bg-slate-50/50 p-3">
            <p className="text-[11px] font-bold uppercase tracking-wider text-slate-400">Target Contact</p>
            {contact ? (
              <div className="mt-2 text-sm">
                <p className="font-semibold text-ink">
                  {contact.name || contact.job_title || "Unnamed Contact"}
                </p>
                <p className="text-xs text-slate-500">
                  {[contact.job_title, contact.department].filter(Boolean).join(" · ") || "No title recorded"}
                </p>
                <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-600">
                  {contact.email && <span>Email: <strong className="font-mono">{contact.email}</strong></span>}
                  {contact.phone && <span>Phone: <strong className="font-mono">{contact.phone}</strong></span>}
                  <span>Authority: <strong>{contact.decision_maker_score ?? "—"}/100</strong></span>
                  <span>Influence: <strong>{formatLabel(contact.buying_influence)}</strong></span>
                </div>
              </div>
            ) : (
              <p className="mt-2 text-xs text-slate-500">
                {opp.matched_contacts_count > 0
                  ? `${opp.matched_contacts_count} candidate contact(s) matched, but none met recommendation thresholds.`
                  : "No company contacts matched in database."}
              </p>
            )}
          </div>

          {/* Recommendation */}
          <div className="rounded-lg border border-slate-100 bg-slate-50/50 p-3">
            <div className="flex items-center justify-between">
              <p className="text-[11px] font-bold uppercase tracking-wider text-slate-400">Recommended Action</p>
              {rec?.recommendation_score !== undefined && (
                <span className="text-xs font-semibold text-teal">
                  Fit Score: {rec.recommendation_score}/100
                </span>
              )}
            </div>
            {rec && rec.recommended_action && rec.recommended_action !== "NO_ACTION" ? (
              <div className="mt-2 text-sm">
                <span className="inline-block rounded-md bg-teal/10 px-2.5 py-1 text-xs font-bold text-teal">
                  {formatLabel(rec.recommended_action)}
                </span>
                {rec.reasons && Array.isArray(rec.reasons) && rec.reasons.length > 0 && (
                  <ul className="mt-2 list-disc space-y-1 pl-4 text-xs text-slate-600">
                    {rec.reasons.map((reason: string, i: number) => (
                      <li key={i}>{reason}</li>
                    ))}
                  </ul>
                )}
              </div>
            ) : (
              <p className="mt-2 text-xs text-slate-500">
                No actionable sales outreach recommendation generated for this opportunity.
              </p>
            )}
          </div>
        </div>

        {/* Right Column: Task Bridge & Feedback */}
        <div className="space-y-3">
          {/* Sales Task Bridge */}
          <div className="rounded-lg border border-slate-100 bg-slate-50/50 p-3">
            <p className="text-[11px] font-bold uppercase tracking-wider text-slate-400">Sales Task Bridge</p>
            {task && task.task_id ? (
              <div className="mt-2 text-sm">
                <div className="flex items-center gap-2">
                  <span
                    className={`rounded-full px-2 py-0.5 text-xs font-semibold ${
                      task.created
                        ? "bg-emerald-50 text-emerald-700 border border-emerald-200"
                        : "bg-teal/10 text-teal border border-teal/20"
                    }`}
                  >
                    {task.created ? "Task Created" : "Task Reused"}
                  </span>
                  <span className="text-xs font-mono text-slate-500">ID: {task.task_id.slice(0, 8)}</span>
                </div>
                <div className="mt-2 flex flex-wrap gap-x-4 text-xs text-slate-600">
                  <span>Type: <strong>{formatLabel(task.task_type)}</strong></span>
                  <span>Priority: <strong>{task.priority || "MEDIUM"}</strong></span>
                </div>
                <p className="mt-2 text-[11px] text-slate-400">
                  Human-managed review task. External outreach is never sent automatically.
                </p>
              </div>
            ) : (
              <p className="mt-2 text-xs text-slate-500">No active sales task synchronized.</p>
            )}
          </div>

          {/* Sales Execution Feedback */}
          <div className="rounded-lg border border-slate-100 bg-slate-50/50 p-3">
            <p className="text-[11px] font-bold uppercase tracking-wider text-slate-400">Sales Execution Feedback</p>
            {feedback ? (
              <div className="mt-2 text-sm">
                <div className="flex items-center gap-3">
                  <div>
                    <span className="text-xs text-slate-500">Total Executions: </span>
                    <strong className="text-ink">{feedback.total_executions}</strong>
                  </div>
                  {feedback.total_executions > 0 && (
                    <div>
                      <span className="text-xs text-slate-500">Positive Rate: </span>
                      <strong className="text-emerald-700">{feedback.positive_rate}%</strong>
                    </div>
                  )}
                </div>
                {feedback.total_executions > 0 ? (
                  <div className="mt-2 flex gap-2 text-xs">
                    <span className="rounded bg-emerald-50 px-2 py-0.5 text-emerald-800">
                      Positive: {feedback.positive_count}
                    </span>
                    <span className="rounded bg-slate-100 px-2 py-0.5 text-slate-700">
                      Neutral: {feedback.neutral_count}
                    </span>
                    <span className="rounded bg-rose-50 px-2 py-0.5 text-rose-800">
                      Negative: {feedback.negative_count}
                    </span>
                  </div>
                ) : (
                  <p className="mt-1 text-xs text-slate-400">No completed sales activity records for this opportunity yet.</p>
                )}
              </div>
            ) : (
              <p className="mt-2 text-xs text-slate-500">No feedback metrics collected.</p>
            )}
          </div>
        </div>
      </div>

      {/* Opportunity Error Banner */}
      {opp.error && (
        <div role="alert" className="mt-3 rounded-md border border-rose-200 bg-rose-50 p-2.5 text-xs text-rose-900">
          <strong>Opportunity Error:</strong> {opp.error}
        </div>
      )}
    </article>
  );
}
