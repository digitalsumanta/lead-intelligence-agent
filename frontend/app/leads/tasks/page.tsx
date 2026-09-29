"use client";

import Link from "next/link";
import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError, LeadListItem, OutcomeSuggestion, SalesActionTask, SalesPriority, SalesTask, SalesTaskStatus, SalesTaskSummary, SalesTaskType, SeoSalesOpportunity } from "@/lib/api";
import AISalesActionPlanner from "@/app/leads/[id]/ai-sales-action-planner";

const TASK_TYPES: SalesTaskType[] = ["FOLLOW_UP", "CALL", "EMAIL", "WHATSAPP", "MEETING", "PROPOSAL", "REVIEW_OUTREACH", "OTHER"];
const PRIORITIES: SalesPriority[] = ["LOW", "MEDIUM", "HIGH", "URGENT"];
const STATUSES: SalesTaskStatus[] = ["OPEN", "IN_PROGRESS", "COMPLETED", "CANCELLED"];

function displayDate(value: string) {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "—" : parsed.toLocaleString();
}

function localDateTimeAfterDay() {
  const date = new Date(Date.now() + 24 * 60 * 60 * 1000);
  date.setMinutes(date.getMinutes() - date.getTimezoneOffset());
  return date.toISOString().slice(0, 16);
}

function commercialValue(value: string | null, currency: string, field: string | null) {
  if (value === null || !field) return "Commercial value not provided";
  const label = field.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
  return `${label}: ${value} ${currency}`;
}

export default function TasksPage() {
  const [summary, setSummary] = useState<SalesTaskSummary | null>(null);
  const [filteredTasks, setFilteredTasks] = useState<SalesActionTask[] | null>(null);
  const [leads, setLeads] = useState<LeadListItem[]>([]);
  const [status, setStatus] = useState("");
  const [taskType, setTaskType] = useState("");
  const [priority, setPriority] = useState("");
  const [assignedTo, setAssignedTo] = useState("");
  const [leadId, setLeadId] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [suggestionToCreate, setSuggestionToCreate] = useState<OutcomeSuggestion | null>(null);
  const [suggestionDueAt, setSuggestionDueAt] = useState("");
  const [suggestionPriority, setSuggestionPriority] = useState<SalesPriority>("MEDIUM");
  const [suggestionAssignee, setSuggestionAssignee] = useState("");
  const [seoOpportunityToCreate, setSeoOpportunityToCreate] = useState<SeoSalesOpportunity | null>(null);
  const [seoDueAt, setSeoDueAt] = useState("");
  const [seoPriority, setSeoPriority] = useState<SalesPriority>("MEDIUM");
  const [seoAssignee, setSeoAssignee] = useState("");
  const filtered = Boolean(status || taskType || priority || assignedTo.trim() || leadId);
  const queryString = useMemo(() => {
    const params = new URLSearchParams();
    if (status) params.set("status", status);
    if (taskType) params.set("task_type", taskType);
    if (priority) params.set("priority", priority);
    if (assignedTo.trim()) params.set("assigned_to", assignedTo.trim());
    if (leadId) params.set("lead_id", leadId);
    return params.toString();
  }, [status, taskType, priority, assignedTo, leadId]);

  const load = useCallback(async () => {
    setLoading(true); setError("");
    try {
      if (filtered) {
        const [rows, data] = await Promise.all([
          api<SalesActionTask[]>(`/api/tasks?${queryString}`),
          api<SalesTaskSummary>("/api/tasks/summary"),
        ]);
        setFilteredTasks(rows); setSummary(data);
      } else {
        const data = await api<SalesTaskSummary>("/api/tasks/summary");
        setSummary(data); setFilteredTasks(null);
      }
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : "Tasks could not be loaded.");
    } finally { setLoading(false); }
  }, [filtered, queryString]);

  useEffect(() => {
    void api<LeadListItem[]>("/api/leads?page=1&page_size=100").then(setLeads).catch(() => setLeads([]));
  }, []);
  useEffect(() => { void load(); }, [load]);

  async function updateTask(task: SalesTask, action: "complete" | "cancel") {
    setBusy(true); setError("");
    try {
      await api(`/api/tasks/${task.id}/${action}`, { method: "POST" });
      await load();
    } catch (cause) { setError(cause instanceof ApiError ? cause.message : "Task could not be updated."); }
    finally { setBusy(false); }
  }

  function beginSuggestionTask(suggestion: OutcomeSuggestion) {
    setSuggestionToCreate(suggestion);
    setSuggestionDueAt(localDateTimeAfterDay());
    setSuggestionPriority(suggestion.lead_priority);
    setSuggestionAssignee(suggestion.lead_assigned_to || "");
  }

  async function createSuggestedTask(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!suggestionToCreate || !suggestionDueAt) return;
    setBusy(true); setError("");
    try {
      await api<SalesTask>(`/api/leads/${suggestionToCreate.lead_id}/tasks`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          task_type: suggestionToCreate.suggested_task_type,
          title: `${suggestionToCreate.suggested_task_type.replaceAll("_", " ")} after ${suggestionToCreate.outcome.replaceAll("_", " ")}`,
          due_at: new Date(suggestionDueAt).toISOString(),
          priority: suggestionPriority,
          assigned_to: suggestionAssignee.trim() || null,
          related_activity_id: suggestionToCreate.activity_id,
        }),
      });
      setSuggestionToCreate(null);
      await load();
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : "Task could not be created.");
    } finally { setBusy(false); }
  }

  function beginSeoOpportunityTask(opportunity: SeoSalesOpportunity) {
    setSeoOpportunityToCreate(opportunity);
    setSeoDueAt(localDateTimeAfterDay());
    setSeoPriority(opportunity.lead_priority);
    setSeoAssignee(opportunity.assigned_to || "");
  }

  async function createSeoOpportunityTask(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!seoOpportunityToCreate || !seoDueAt) return;
    setBusy(true); setError("");
    try {
      await api<SalesTask>(`/api/leads/${encodeURIComponent(seoOpportunityToCreate.lead_id)}/tasks`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          task_type: seoOpportunityToCreate.suggested_task_type,
          title: `Review SEO opportunity: ${seoOpportunityToCreate.title}`,
          description: `${seoOpportunityToCreate.summary} SEO audit ${seoOpportunityToCreate.seo_run_id}; findings ${seoOpportunityToCreate.evidence.map((item) => item.finding_id).join(", ")}.`,
          due_at: new Date(seoDueAt).toISOString(),
          priority: seoPriority,
          assigned_to: seoAssignee.trim() || null,
          seo_opportunity_id: seoOpportunityToCreate.opportunity_id,
        }),
      });
      setSeoOpportunityToCreate(null);
      await load();
    } catch (cause) {
      await load();
      setError(cause instanceof ApiError ? cause.message : "SEO opportunity task could not be created.");
    } finally { setBusy(false); }
  }

  let today: SalesActionTask[] = summary?.today || [];
  let overdue: SalesActionTask[] = summary?.overdue || [];
  let upcoming: SalesActionTask[] = summary?.upcoming || [];
  let completed: SalesActionTask[] = [];
  let cancelled: SalesActionTask[] = [];
  if (filteredTasks) {
    const now = Date.now();
    completed = filteredTasks.filter((task) => task.status === "COMPLETED");
    cancelled = filteredTasks.filter((task) => task.status === "CANCELLED");
    overdue = filteredTasks.filter((task) => task.is_overdue);
    today = filteredTasks.filter((task) => task.status !== "COMPLETED" && task.status !== "CANCELLED" && !task.is_overdue && new Date(task.due_at).getTime() < new Date().setHours(24, 0, 0, 0));
    upcoming = filteredTasks.filter((task) => task.status !== "COMPLETED" && task.status !== "CANCELLED" && new Date(task.due_at).getTime() >= now && new Date(task.due_at).getTime() <= now + 7 * 24 * 60 * 60 * 1000 && !today.some((item) => item.id === task.id));
  }

  function taskRow(task: SalesActionTask) {
    const actionable = task.status === "OPEN" || task.status === "IN_PROGRESS";
    return <article key={task.id} className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm"><div className="flex flex-wrap items-start justify-between gap-3"><div><Link href={`/leads/${task.lead_id}`} className="text-xs font-semibold text-teal hover:underline">{task.lead_company_name || "Open lead"}</Link><h3 className="mt-1 font-bold text-ink">{task.title}</h3><p className="mt-1 text-xs text-slate-500">{task.task_type.replaceAll("_", " ")} · {task.status.replaceAll("_", " ")} · Task priority {task.priority}</p></div><span className={`rounded-full px-2.5 py-1 text-xs font-semibold ${task.is_overdue ? "bg-rose-50 text-rose-800" : "bg-slate-100 text-slate-600"}`}>{task.is_overdue ? "Overdue" : displayDate(task.due_at)}</span></div><p className="mt-3 text-xs text-slate-500">Assigned to {task.assigned_to || "Unassigned"}{task.related_outreach_artifact_id ? " · Outreach-linked" : ""}</p><div className="mt-3 rounded-lg bg-slate-50 p-3 text-xs text-slate-600"><p>Lead status: {task.lead_sales_status} · Lead priority: {task.lead_priority}</p><p className="mt-1">{commercialValue(task.current_stage_value, task.currency, task.current_stage_value_field)}</p><p className="mt-1">Next follow-up: {task.next_follow_up_at ? displayDate(task.next_follow_up_at) : "Not set"}</p></div>{task.description && <p className="mt-2 text-sm text-slate-600">{task.description}</p>}{actionable && <div className="mt-3 flex gap-2"><button disabled={busy} onClick={() => void updateTask(task, "complete")} className="rounded-lg bg-emerald-700 px-3 py-2 text-xs font-semibold text-white disabled:opacity-50">Complete</button><button disabled={busy} onClick={() => void updateTask(task, "cancel")} className="rounded-lg border border-slate-300 px-3 py-2 text-xs font-semibold disabled:opacity-50">Cancel</button></div>}</article>;
  }

  const groups = [
    ["Today's tasks", today], ["Overdue", overdue], ["Upcoming next 7 days", upcoming], ["Completed", completed], ["Cancelled", cancelled],
  ] as const;

  const outcomeSuggestions = summary?.outcome_suggestions || [];
  const seoOpportunities = summary?.seo_opportunities || [];

  return <main className="mx-auto min-h-screen max-w-6xl px-4 py-6 sm:px-8 lg:py-10"><header className="flex flex-wrap items-end justify-between gap-4 border-b border-slate-200 pb-6"><div><Link href="/leads" className="text-xs font-bold uppercase tracking-[.2em] text-teal">Lead Intelligence Agent</Link><h1 className="mt-2 text-3xl font-bold tracking-tight text-ink">Sales Action Center</h1><p className="mt-1 text-slate-500">Human-managed tasks and outcome suggestions. Tasks are created only after an explicit action; nothing is sent automatically.</p></div><Link href="/leads" className="rounded-lg border border-slate-300 px-4 py-2 text-sm font-semibold">All leads</Link></header>
    <section className="mt-6 rounded-xl border border-slate-200 bg-white p-5 shadow-sm"><h2 className="font-bold text-ink">Filter tasks</h2><div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-5"><label className="text-xs font-semibold text-slate-600">Status<select className="mt-1 block w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm" value={status} onChange={(event) => setStatus(event.target.value)}><option value="">Any status</option>{STATUSES.map((value) => <option key={value}>{value}</option>)}</select></label><label className="text-xs font-semibold text-slate-600">Task type<select className="mt-1 block w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm" value={taskType} onChange={(event) => setTaskType(event.target.value)}><option value="">Any type</option>{TASK_TYPES.map((value) => <option key={value}>{value}</option>)}</select></label><label className="text-xs font-semibold text-slate-600">Priority<select className="mt-1 block w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm" value={priority} onChange={(event) => setPriority(event.target.value)}><option value="">Any priority</option>{PRIORITIES.map((value) => <option key={value}>{value}</option>)}</select></label><label className="text-xs font-semibold text-slate-600">Assigned to<input className="mt-1 block w-full rounded-lg border border-slate-300 px-3 py-2 text-sm" value={assignedTo} onChange={(event) => setAssignedTo(event.target.value)} placeholder="Owner label" /></label><label className="text-xs font-semibold text-slate-600">Lead<select className="mt-1 block w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm" value={leadId} onChange={(event) => setLeadId(event.target.value)}><option value="">All leads</option>{leads.map((lead) => <option key={lead.id} value={lead.id}>{lead.company_name}</option>)}</select></label></div></section>
    <section className="mt-5 rounded-xl border border-violet-200 bg-white p-4"><div><h2 className="font-bold text-ink">AI Recommended Actions</h2><p className="mt-1 text-sm text-slate-500">Choose one lead in the filter above to review its current evidence-grounded plan. Tasks require explicit confirmation.</p></div>{leadId ? <AISalesActionPlanner key={leadId} leadId={leadId} onTaskCreated={() => void load()} /> : <p className="mt-3 rounded-lg bg-slate-50 p-3 text-sm text-slate-500">Select one lead to load its recommendations.</p>}</section>
    {error && <p role="alert" className="mt-5 rounded-lg border border-rose-200 bg-rose-50 p-3 text-sm text-rose-800">{error}</p>}
    {loading ? <p className="mt-8 text-center text-sm text-slate-500">Loading sales action center…</p> : <div className="mt-6 space-y-6">{groups.map(([name, tasks]) => <section key={name}><div className="mb-3 flex items-center justify-between"><h2 className="text-lg font-bold text-ink">{name}</h2><span className="rounded-full bg-slate-100 px-2.5 py-1 text-xs font-semibold text-slate-600">{tasks.length}</span></div>{tasks.length ? <div className="grid gap-3 md:grid-cols-2">{tasks.map(taskRow)}</div> : <p className="rounded-xl border border-dashed border-slate-300 bg-white p-5 text-sm text-slate-500">No tasks in this section.</p>}</section>)}
      <section><div className="mb-3 flex items-center justify-between"><div><h2 className="text-lg font-bold text-ink">Outcome Suggestions</h2><p className="mt-1 text-sm text-slate-500">Deterministic suggestions from recorded outcomes without a linked task. Review each before creating a task.</p></div><span className="rounded-full bg-slate-100 px-2.5 py-1 text-xs font-semibold text-slate-600">{outcomeSuggestions.length}</span></div>{outcomeSuggestions.length ? <div className="grid gap-3 md:grid-cols-2">{outcomeSuggestions.map((suggestion) => <article key={suggestion.activity_id} className="rounded-xl border border-teal/20 bg-white p-4 shadow-sm"><div className="flex items-start justify-between gap-3"><div><Link href={`/leads/${suggestion.lead_id}`} className="text-xs font-semibold text-teal hover:underline">{suggestion.lead_company_name}</Link><h3 className="mt-1 font-bold text-ink">{suggestion.suggested_task_type.replaceAll("_", " ")}</h3><p className="mt-1 text-sm text-slate-600">Recorded {suggestion.outcome.replaceAll("_", " ")} · {suggestion.activity_type.replaceAll("_", " ")}</p><p className="mt-1 text-xs text-slate-400">{displayDate(suggestion.occurred_at)}</p></div><span className="rounded-full bg-slate-100 px-2 py-1 text-xs">Suggested</span></div><div className="mt-3 rounded-lg bg-slate-50 p-3 text-xs text-slate-600"><p>Lead status: {suggestion.lead_sales_status} · Lead priority: {suggestion.lead_priority}</p><p className="mt-1">{commercialValue(suggestion.current_stage_value, suggestion.currency, suggestion.current_stage_value_field)}</p><p className="mt-1">Next follow-up: {suggestion.next_follow_up_at ? displayDate(suggestion.next_follow_up_at) : "Not set"}</p></div>{suggestionToCreate?.activity_id === suggestion.activity_id ? <form onSubmit={(event) => void createSuggestedTask(event)} className="mt-4 grid gap-3 rounded-lg border border-slate-200 p-3 sm:grid-cols-2"><label className="text-xs font-semibold text-slate-600">Due at<input required type="datetime-local" value={suggestionDueAt} onChange={(event) => setSuggestionDueAt(event.target.value)} className="mt-1 block w-full rounded-lg border border-slate-300 px-2 py-2 text-sm" /></label><label className="text-xs font-semibold text-slate-600">Task priority<select value={suggestionPriority} onChange={(event) => setSuggestionPriority(event.target.value as SalesPriority)} className="mt-1 block w-full rounded-lg border border-slate-300 bg-white px-2 py-2 text-sm">{PRIORITIES.map((value) => <option key={value}>{value}</option>)}</select></label><label className="text-xs font-semibold text-slate-600 sm:col-span-2">Assigned to<input value={suggestionAssignee} onChange={(event) => setSuggestionAssignee(event.target.value)} className="mt-1 block w-full rounded-lg border border-slate-300 px-2 py-2 text-sm" placeholder="Unassigned" /></label><div className="flex gap-2 sm:col-span-2"><button disabled={busy || !suggestionDueAt} className="rounded-lg bg-ink px-3 py-2 text-xs font-semibold text-white disabled:opacity-50">Create task</button><button type="button" disabled={busy} onClick={() => setSuggestionToCreate(null)} className="rounded-lg border border-slate-300 px-3 py-2 text-xs">Cancel</button></div></form> : <button disabled={busy} onClick={() => beginSuggestionTask(suggestion)} className="mt-4 rounded-lg border border-teal/40 px-3 py-2 text-xs font-semibold text-teal disabled:opacity-50">Review and create task</button>}</article>)}</div> : <p className="rounded-xl border border-dashed border-slate-300 bg-white p-5 text-sm text-slate-500">No unhandled outcome suggestions.</p>}</section>
      <section><div className="mb-3 flex items-center justify-between"><div><h2 className="text-lg font-bold text-ink">SEO Opportunities</h2><p className="mt-1 text-sm text-slate-500">Deterministic groups from each lead’s latest completed SEO audit. No tasks or outreach are created unless you submit the task form.</p></div><span className="rounded-full bg-slate-100 px-2.5 py-1 text-xs font-semibold text-slate-600">{seoOpportunities.length}</span></div>{seoOpportunities.length ? <div className="grid gap-3 md:grid-cols-2">{seoOpportunities.map((opportunity) => <article key={opportunity.opportunity_id} className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm"><div className="flex items-start justify-between gap-3"><div><Link href={`/leads/${opportunity.lead_id}`} className="text-xs font-semibold text-teal hover:underline">{leads.find((lead) => lead.id === opportunity.lead_id)?.company_name || "Open lead"}</Link><h3 className="mt-1 font-bold text-ink">{opportunity.title}</h3></div><span className="rounded-full bg-amber-50 px-2.5 py-1 text-xs font-semibold text-amber-800">{opportunity.severity}</span></div><p className="mt-2 text-sm text-slate-600">{opportunity.summary}</p><p className="mt-2 text-xs text-slate-500">{opportunity.finding_count} findings · {opportunity.affected_page_count} affected pages · audit {opportunity.seo_run_id.slice(0, 8)}</p><p className="mt-2 text-xs text-slate-600">Lead status: {opportunity.current_sales_status} · Priority: {opportunity.lead_priority}</p><p className="mt-1 text-xs text-slate-600">{commercialValue(opportunity.current_stage_value, opportunity.currency, opportunity.current_stage_value_field)} · Next follow-up: {opportunity.next_follow_up_at ? displayDate(opportunity.next_follow_up_at) : "Not set"}</p>{opportunity.recommended_service_ids.length > 0 && <p className="mt-2 text-xs text-slate-600">Existing service mapping: {opportunity.recommended_service_ids.join(", ")}</p>}<ul className="mt-3 space-y-1">{opportunity.evidence.slice(0, 3).map((item) => <li key={item.finding_id} className="text-xs text-slate-600">{item.finding_code}: {item.title}{item.page_url ? <span className="block break-all text-slate-400">{item.page_url}</span> : null}</li>)}</ul><div className="mt-3 rounded-lg bg-slate-50 p-3 text-xs text-slate-600"><p>Suggested action: {opportunity.suggested_action}</p><p className="mt-1">Related task: {opportunity.current_task_state.replaceAll("_", " ")}{opportunity.related_task_id ? ` · ${opportunity.related_task_id.slice(0, 8)}` : ""}</p></div>{opportunity.current_task_state === "NO_TASK" ? seoOpportunityToCreate?.opportunity_id === opportunity.opportunity_id ? <form onSubmit={(event) => void createSeoOpportunityTask(event)} className="mt-4 grid gap-3 rounded-lg border border-slate-200 p-3 sm:grid-cols-2"><label className="text-xs font-semibold text-slate-600">Due at<input required type="datetime-local" value={seoDueAt} onChange={(event) => setSeoDueAt(event.target.value)} className="mt-1 block w-full rounded-lg border border-slate-300 px-2 py-2 text-sm" /></label><label className="text-xs font-semibold text-slate-600">Task priority<select value={seoPriority} onChange={(event) => setSeoPriority(event.target.value as SalesPriority)} className="mt-1 block w-full rounded-lg border border-slate-300 bg-white px-2 py-2 text-sm">{PRIORITIES.map((value) => <option key={value}>{value}</option>)}</select></label><label className="text-xs font-semibold text-slate-600 sm:col-span-2">Assigned to<input value={seoAssignee} onChange={(event) => setSeoAssignee(event.target.value)} className="mt-1 block w-full rounded-lg border border-slate-300 px-2 py-2 text-sm" placeholder="Unassigned" /></label><div className="flex gap-2 sm:col-span-2"><button disabled={busy || !seoDueAt} className="rounded-lg bg-ink px-3 py-2 text-xs font-semibold text-white disabled:opacity-50">Create task</button><button type="button" disabled={busy} onClick={() => setSeoOpportunityToCreate(null)} className="rounded-lg border border-slate-300 px-3 py-2 text-xs">Cancel</button></div></form> : <button disabled={busy} onClick={() => beginSeoOpportunityTask(opportunity)} className="mt-4 rounded-lg border border-teal/40 px-3 py-2 text-xs font-semibold text-teal disabled:opacity-50">Review and create task</button> : <p className="mt-4 text-xs font-semibold text-slate-500">This opportunity already has a linked {opportunity.current_task_state.toLowerCase().replaceAll("_", " ")} task.</p>}</article>)}</div> : <p className="rounded-xl border border-dashed border-slate-300 bg-white p-5 text-sm text-slate-500">No actionable opportunities from completed SEO audits.</p>}</section>
    </div>}
  </main>;
}
