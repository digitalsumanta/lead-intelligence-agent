"use client";

import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { api, ActionPlan, ApiError, PlannerAction, SalesTask, SalesTaskType } from "@/lib/api";

const TASK_TYPES: Record<PlannerAction["action_type"], SalesTaskType> = {
  FOLLOW_UP: "FOLLOW_UP", CALL_PROSPECT: "CALL", REVIEW_SEO_OPPORTUNITY: "OTHER",
  REVIEW_OUTREACH: "REVIEW_OUTREACH", PREPARE_PROPOSAL: "PROPOSAL",
  INVESTIGATE_MISSING_INFORMATION: "OTHER", REVIEW_COMMERCIAL_CONTEXT: "OTHER",
};

function nextDay() {
  const value = new Date(Date.now() + 24 * 60 * 60 * 1000);
  value.setMinutes(value.getMinutes() - value.getTimezoneOffset());
  return value.toISOString().slice(0, 16);
}

export default function AISalesActionPlanner({ leadId, onTaskCreated }: { leadId: string; onTaskCreated?: () => void }) {
  const [plan, setPlan] = useState<ActionPlan | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [taskIndex, setTaskIndex] = useState<number | null>(null);
  const [dueAt, setDueAt] = useState(nextDay());
  const [message, setMessage] = useState("");
  const evidence = useMemo(() => new Map((plan?.evidence_catalog || []).map((item) => [item.id, item])), [plan]);

  const load = useCallback(async () => {
    try { setPlan(await api<ActionPlan | null>(`/api/leads/${encodeURIComponent(leadId)}/ai-sales-action-plan`)); }
    catch (cause) { setError(cause instanceof ApiError ? cause.message : "Action plan could not be loaded."); }
  }, [leadId]);
  useEffect(() => { void load(); }, [load]);

  async function generate() {
    setBusy(true); setError(""); setMessage("");
    try { setPlan(await api<ActionPlan>(`/api/leads/${encodeURIComponent(leadId)}/ai-sales-action-plan`, { method: "POST" })); }
    catch (cause) { setError(cause instanceof ApiError ? cause.message : "Action plan could not be generated."); }
    finally { setBusy(false); }
  }

  async function createTask(event: FormEvent<HTMLFormElement>, action: PlannerAction, index: number) {
    event.preventDefault(); setBusy(true); setError(""); setMessage("");
    try {
      await api<SalesTask>(`/api/leads/${encodeURIComponent(leadId)}/tasks`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          task_type: TASK_TYPES[action.action_type], title: action.title,
          description: `${action.rationale} Evidence: ${action.evidence_ids.join(", ")}${action.opportunity_ids.length ? `; Opportunities: ${action.opportunity_ids.join(", ")}` : ""}${action.service_ids.length ? `; Services: ${action.service_ids.join(", ")}` : ""}${action.preparation_items.length ? `; Preparation: ${action.preparation_items.join("; ")}` : ""}`,
          due_at: new Date(dueAt).toISOString(), priority: action.priority,
        }),
      });
      setTaskIndex(null); setMessage("Task created through the existing SalesTask workflow."); onTaskCreated?.(); await load();
    } catch (cause) { setError(cause instanceof ApiError ? cause.message : "Task could not be created."); }
    finally { setBusy(false); }
    void index;
  }

  const actions = plan?.action_plan?.actions || [];
  return <section id="ai-sales-actions" className="mt-5 rounded-xl border border-violet-200 bg-white p-5 shadow-sm">
    <div className="flex flex-wrap items-start justify-between gap-3"><div><p className="text-[10px] font-bold uppercase tracking-[.18em] text-violet-700">Advisory only · human review required</p><h2 className="mt-1 font-bold text-ink">AI Recommended Actions</h2><p className="mt-1 text-sm text-slate-500">Actions cite current lead evidence. Nothing is created or executed until you choose Create Task.</p></div><button type="button" disabled={busy} onClick={() => void generate()} className="rounded-lg bg-ink px-4 py-2 text-sm font-semibold text-white disabled:opacity-50">{busy ? "Generating…" : plan ? "Refresh recommendations" : "Generate recommendations"}</button></div>
    {busy && (
      <div className="mt-4 flex items-center gap-3 rounded-lg border border-violet-200 bg-violet-50/60 p-4 text-sm text-violet-900">
        <svg className="h-5 w-5 animate-spin text-violet-600 shrink-0" viewBox="0 0 24 24" fill="none">
          <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
          <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
        </svg>
        <div>
          <p className="font-semibold">Generating recommendations via local AI model…</p>
          <p className="text-xs text-violet-700">CPU inference typically takes 2–4 minutes. Grounding against current lead evidence.</p>
        </div>
      </div>
    )}
    {error && <p role="alert" className="mt-4 rounded-lg border border-rose-200 bg-rose-50 p-3 text-sm text-rose-800">{error}</p>}{message && <p role="status" className="mt-4 text-sm text-emerald-700">{message}</p>}
    {plan?.is_stale && <p className="mt-4 rounded-lg bg-amber-50 p-3 text-sm text-amber-800">This saved plan uses older context. Refresh it before acting.</p>}
    {plan?.status === "failed" && <p role="alert" className="mt-4 text-sm text-rose-700">{plan.error_message || "No validated action plan is available."}</p>}
    {plan?.status === "validated" && !actions.length && <p className="mt-4 rounded-lg bg-slate-50 p-4 text-sm text-slate-600">No recommended actions were returned for the current context.</p>}
    {actions.length > 0 && <div className="mt-4 space-y-3">{actions.map((action, index) => <article key={`${action.action_type}-${index}`} className="rounded-lg border border-slate-200 p-4"><div className="flex flex-wrap items-start justify-between gap-2"><div><h3 className="font-semibold text-ink">{action.title}</h3><p className="mt-1 text-xs text-slate-500">{action.action_type.replaceAll("_", " ")} · Existing lead priority: {action.priority}</p></div><span className="rounded-full bg-violet-50 px-2 py-1 text-xs font-semibold text-violet-800">Recommended</span></div><p className="mt-3 text-sm text-slate-700">{action.rationale}</p>{action.preparation_items.length > 0 && <ul className="mt-3 list-disc space-y-1 pl-5 text-xs text-slate-600">{action.preparation_items.map((item) => <li key={item}>{item}</li>)}</ul>}
      <div className="mt-3 flex flex-wrap gap-2">{action.evidence_ids.map((id) => <span key={id} title={evidence.get(id)?.label || "Evidence reference"} className="max-w-full break-all rounded bg-slate-100 px-2 py-1 font-mono text-[10px] text-slate-600">{evidence.get(id)?.label || id}</span>)}{action.opportunity_ids.map((id) => <span key={id} className="max-w-full break-all rounded bg-amber-50 px-2 py-1 font-mono text-[10px] text-amber-800">Opportunity: {evidence.get(id)?.label || id}</span>)}{action.service_ids.map((id) => <span key={id} className="rounded bg-teal/10 px-2 py-1 text-[10px] text-teal">Service: {id}</span>)}</div>
      <div className="mt-4 flex flex-wrap gap-2"><a href={`/leads/${encodeURIComponent(leadId)}#evidence`} className="rounded-lg border border-slate-300 px-3 py-2 text-xs font-semibold text-teal">Review Evidence</a>{taskIndex === index ? <form onSubmit={(event) => void createTask(event, action, index)} className="flex flex-wrap items-end gap-2"><label className="text-[11px] font-semibold text-slate-600">Due at<input required type="datetime-local" value={dueAt} onChange={(event) => setDueAt(event.target.value)} className="mt-1 block rounded border border-slate-300 px-2 py-1.5 text-xs" /></label><button disabled={busy} className="rounded-lg bg-ink px-3 py-2 text-xs font-semibold text-white disabled:opacity-50">Confirm task creation</button><button type="button" onClick={() => setTaskIndex(null)} className="rounded-lg border border-slate-300 px-3 py-2 text-xs">Cancel</button></form> : <button type="button" disabled={busy || plan?.is_stale || plan?.status !== "validated"} onClick={() => { setDueAt(nextDay()); setTaskIndex(index); }} className="rounded-lg border border-violet-300 px-3 py-2 text-xs font-semibold text-violet-800 disabled:opacity-50">Create Task</button>}</div>
      </article>)}</div>}
    {plan && <p className="mt-4 text-[10px] text-slate-400">{plan.contract_version} · {plan.prompt_version} · {plan.provider} / {plan.model} · {new Date(plan.created_at).toLocaleString()}</p>}
  </section>;
}
